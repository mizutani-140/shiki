"""Static shipping-completeness check for the ``shiki init`` install surface.

Guards against one defect class: a *shipped* artifact (a Python module,
contract test, or workflow that ``shiki init`` copies into a target) depending
on a repository-relative artifact that is NOT shipped. When that happens a fresh
install passes ``shiki init``'s own validation yet is permanently red on the
required ``Validate Shiki mirror`` check -- and because ``init`` also applies
branch protection requiring that check, nothing in the target can ever merge.

For every shipped artifact this check resolves the repository-relative
dependencies it names -- Python imports of sibling ``scripts/*.py`` modules,
``scripts/...`` and ``tests/...`` paths referenced in shell and YAML, and the
directory a ``unittest discover -s <dir>`` names -- and asserts each is either
shipped (in ``TEMPLATE_PATHS`` or under a shipped directory) or on the explicit,
reasoned exception list below. This is the check that would have caught, in one
pass, the shipped ``tests`` directory, ``scripts/shiki_session_lease.py`` and
``scripts/shiki_contract_approval.py`` all being unshipped.
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from shiki_installer import TARGET_INSTALL_EXCLUDES, TEMPLATE_PATHS  # noqa: E402


# Repository-relative dependencies that a shipped artifact names but that are
# deliberately NOT shipped into a target. Every entry MUST carry a non-empty
# reason: an omission here is a recorded decision, not an oversight. An empty
# map is the healthy state -- it means every dependency of the shipping surface
# is itself shipped.
SHIPPING_EXCEPTIONS: dict[str, str] = {
    # "scripts/example.py": "why this dependency is intentionally not shipped",
}


_GLOB_CHARS = frozenset("*?[]")
# ``from X import`` / ``import X`` inside embedded Python (shell/YAML heredocs).
_TEXT_IMPORT_RE = re.compile(r"(?:^|\b)(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)")
# ``scripts/...`` or ``tests/...`` path literals in shell and YAML. Not preceded
# by a word char/dot/dash (so ``myscripts/x`` does not match) but a leading
# ``/`` is fine (so ``$ROOT/scripts/x.py`` still resolves to ``scripts/x.py``).
_PATH_RE = re.compile(r"(?<![\w.-])(?:scripts|tests)/[A-Za-z0-9_./-]+")
_DISCOVER_RE = re.compile(r"unittest\s+discover\b[^\n]*?-s\s+(\S+)")


def _exists_exact(root: Path, rel: str) -> bool:
    """Case-sensitive existence check.

    ``Path.exists`` is case-insensitive on macOS/Windows, which would let a
    reference like ``from Shiki state`` resolve to ``scripts/shiki.py``. Walking
    the real directory entries keeps resolution case-exact on every platform.
    """
    current = root
    for part in Path(rel).parts:
        try:
            names = {entry.name for entry in current.iterdir()}
        except (NotADirectoryError, FileNotFoundError, PermissionError):
            return False
        if part not in names:
            return False
        current = current / part
    return True


def _py_import_deps(root: Path, path: Path, text: str) -> set[str]:
    """Sibling ``scripts/*.py`` modules imported by a shipped Python module."""
    deps: set[str] = set()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # A shipped module that does not parse is a distinct failure surfaced by
        # ``py_compile``; it names no resolvable static dependencies here.
        return deps
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            candidate = f"scripts/{name.split('.')[0]}.py"
            if _exists_exact(root, candidate):
                deps.add(candidate)
    return deps


def _text_import_deps(root: Path, text: str) -> set[str]:
    """Sibling ``scripts/*.py`` modules imported by embedded Python heredocs."""
    deps: set[str] = set()
    for match in _TEXT_IMPORT_RE.finditer(text):
        candidate = f"scripts/{match.group(1)}.py"
        if _exists_exact(root, candidate):
            deps.add(candidate)
    return deps


def _path_reference_deps(root: Path, text: str) -> set[str]:
    """``scripts/...`` and ``tests/...`` file references in shell/YAML."""
    deps: set[str] = set()
    for match in _PATH_RE.finditer(text):
        raw = match.group(0).rstrip("/.,:;")
        if any(char in raw for char in _GLOB_CHARS):
            continue
        if _exists_exact(root, raw):
            deps.add(raw)
    return deps


def _discover_deps(root: Path, text: str) -> set[str]:
    """The directory a ``unittest discover -s <dir>`` names in shell/YAML."""
    deps: set[str] = set()
    for match in _DISCOVER_RE.finditer(text):
        target = match.group(1)
        if any(char in target for char in _GLOB_CHARS):
            continue
        if _exists_exact(root, target):
            deps.add(target)
    return deps


def classify(rel: str) -> str | None:
    """Classify a shipped path as a scannable ``py``/``sh``/``yml`` source."""
    if rel.startswith("scripts/") and rel.endswith(".py"):
        return "py"
    if rel.startswith("scripts/") and re.search(r"/test_shiki_[^/]*\.sh$", rel):
        return "sh"
    if rel.startswith(".github/workflows/") and rel.endswith(".yml"):
        return "yml"
    return None


def dependencies_of(root: Path, rel: str, kind: str) -> set[str]:
    """All repository-relative artifacts the shipped source ``rel`` depends on."""
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if kind == "py":
        return _py_import_deps(root, path, text)
    # Shell and YAML: heredoc imports plus path and discover-target references.
    deps = _text_import_deps(root, text)
    deps |= _path_reference_deps(root, text)
    deps |= _discover_deps(root, text)
    return deps


def is_shipped(rel: str, shipped: set[str]) -> bool:
    """True when ``rel`` is a shipped path or lives under a shipped directory."""
    if rel in shipped:
        return True
    parts = Path(rel).parts
    for index in range(1, len(parts)):
        if "/".join(parts[:index]) in shipped:
            return True
    return False


def find_unshipped_dependencies(
    root: Path, shipped: set[str], exceptions: dict[str, str]
) -> list[tuple[str, str]]:
    """Return ``(source, dependency)`` pairs where a shipped source depends on an
    unshipped, non-excepted repository artifact."""
    violations: list[tuple[str, str]] = []
    for rel in sorted(shipped):
        kind = classify(rel)
        if kind is None or not _exists_exact(root, rel):
            continue
        for dep in sorted(dependencies_of(root, rel, kind)):
            if dep == rel or is_shipped(dep, shipped) or dep in exceptions:
                continue
            violations.append((rel, dep))
    return violations


class InstallCompletenessTest(unittest.TestCase):
    def test_every_shipped_dependency_is_shipped_or_excepted(self) -> None:
        violations = find_unshipped_dependencies(
            ROOT, set(TEMPLATE_PATHS), SHIPPING_EXCEPTIONS
        )
        if violations:
            detail = "\n".join(
                f"  {source} depends on unshipped {dependency}"
                for source, dependency in violations
            )
            self.fail(
                "shipped artifacts depend on unshipped repository files; add each "
                "to TEMPLATE_PATHS, or to SHIPPING_EXCEPTIONS with a reason:\n"
                + detail
            )

    def test_every_exception_records_a_reason(self) -> None:
        for dependency, reason in SHIPPING_EXCEPTIONS.items():
            self.assertIsInstance(reason, str)
            self.assertTrue(
                reason.strip(), f"exception {dependency!r} must record a reason"
            )

    def test_every_target_install_exclude_records_a_reason(self) -> None:
        # Platform-only tests are kept out of a target install by an explicit,
        # reasoned constant so each omission is a deliberate recorded decision.
        for excluded, reason in TARGET_INSTALL_EXCLUDES.items():
            self.assertTrue(
                excluded.startswith("tests/"),
                f"target-install exclude {excluded!r} should be a tests/ path",
            )
            self.assertIsInstance(reason, str)
            self.assertTrue(
                reason.strip(), f"target-install exclude {excluded!r} must record a reason"
            )

    def test_check_detects_unshipped_python_import(self) -> None:
        # A shipped module importing a sibling that EXISTS but is not shipped
        # must be reported -- proof the check fails on the defect class, not
        # merely that today's surface happens to be complete.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "shipped_module.py").write_text(
                "import shiki_ghost_dependency\n", encoding="utf-8"
            )
            (root / "scripts" / "shiki_ghost_dependency.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            shipped = {"scripts/shipped_module.py"}

            violations = find_unshipped_dependencies(root, shipped, {})
            self.assertIn(
                ("scripts/shipped_module.py", "scripts/shiki_ghost_dependency.py"),
                violations,
            )

            # An explicit, reasoned exception suppresses the same violation.
            excepted = find_unshipped_dependencies(
                root, shipped, {"scripts/shiki_ghost_dependency.py": "fixture"}
            )
            self.assertEqual(excepted, [])

    def test_check_detects_unshipped_shell_path_reference(self) -> None:
        # A shipped contract test naming an unshipped ``scripts/...`` file must
        # be reported too -- covers the shell/YAML path-reference resolver.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "test_shiki_fixture.sh").write_text(
                "#!/usr/bin/env bash\npython3 scripts/shiki_ghost_helper.py\n",
                encoding="utf-8",
            )
            (root / "scripts" / "shiki_ghost_helper.py").write_text(
                "x = 1\n", encoding="utf-8"
            )
            shipped = {"scripts/test_shiki_fixture.sh"}

            violations = find_unshipped_dependencies(root, shipped, {})
            self.assertIn(
                ("scripts/test_shiki_fixture.sh", "scripts/shiki_ghost_helper.py"),
                violations,
            )


if __name__ == "__main__":
    unittest.main()
