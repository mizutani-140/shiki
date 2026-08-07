"""Every target ADR and Shiki SADR citation resolves in its own namespace.

Background. ``scripts/mergegate_check.py`` justifies ``is_bookkeeping_closeout``
with "SADR-0017" nine times and ``scripts/guardian_approval_signal.py`` cites
SADR-0016/SADR-0017 seven times, yet for a stretch the numbering in ``docs/adr/`` stopped
at 0015: the authoring commits lived on a coordinator branch and no task ever
declared ``docs/adr/...`` in its locks, so nothing carried the records to the
default branch. The stated basis of an exemption that merged seven PRs with zero
Guardian approvals could not be read. ``grep -n 'docs/adr' scripts/validate_shiki.py``
returns nothing -- no validator required a cited ADR to exist, so the citations
and the directory drifted with no signal.

This is the same unbound-pair shape behind other silent failures: a reference
and its target with nothing asserting they agree. This test binds them. Target
``ADR NNNN`` citations resolve only to ``docs/adr/NNNN-*.md``; Shiki
``SADR-NNNN`` citations resolve only to ``docs/adr/SADR-NNNN-*.md``. The scan
covers current governance docs, scripts, workflows, prompts, packaging metadata,
and repo-local Codex guidance while deliberately excluding historical
``.shiki`` evidence. The converse is not required: a record may be uncited.

The suite is dependency free: standard-library ``unittest`` only.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# ``ADR 0016`` / ``SADR-0016`` -- a namespace plus four-digit citation. The
# trailing ``\b`` keeps a five-digit token from being read as a truncated one.
_CITATION = re.compile(r"\b(S?ADR)[ -](\d{4})\b")
# Target ADRs retain the numeric filename; Shiki platform decisions use SADR.
_DECISION_FILENAME = re.compile(r"^(?:(SADR)-)?(\d{4})-.*\.md$")


class Violation(NamedTuple):
    """A citation that does not resolve to exactly one decision record."""

    citing_file: str  # repo-relative path of the file that cites the ADR
    namespace: str  # "ADR" for target decisions; "SADR" for Shiki decisions
    number: str  # the cited four-digit number, e.g. "0016"
    line: int  # 1-based line of the citation
    reason: str  # "missing" (zero matches) or "ambiguous" (two or more)
    matches: Sequence[str]  # repo-relative paths matching docs/adr/<n>-*.md

    @property
    def expected_path(self) -> str:
        prefix = "SADR-" if self.namespace == "SADR" else ""
        return f"docs/adr/{prefix}{self.number}-*.md"

    def describe(self) -> str:
        """Name the citing file, the cited number, and the missing path."""
        if self.reason == "missing":
            return (
                f"{self.citing_file}:{self.line} cites {self.namespace} {self.number} "
                f"but no document matches {self.expected_path}"
            )
        joined = ", ".join(self.matches)
        return (
            f"{self.citing_file}:{self.line} cites {self.namespace} {self.number} which "
            f"resolves to {len(self.matches)} documents (expected exactly one "
            f"at {self.expected_path}): {joined}"
        )


def _adr_index(root: Path) -> dict[tuple[str, str], list[Path]]:
    """Map each decision namespace and number to the files that claim it."""
    index: dict[tuple[str, str], list[Path]] = {}
    adr_dir = root / "docs" / "adr"
    if adr_dir.is_dir():
        for path in sorted(adr_dir.glob("*.md")):
            match = _DECISION_FILENAME.match(path.name)
            if match:
                namespace = "SADR" if match.group(1) else "ADR"
                index.setdefault((namespace, match.group(2)), []).append(path)
    return index


def _citing_files(root: Path, include_docs: bool = True) -> list[Path]:
    """Current, shipped text surfaces scanned for decision citations."""
    files = sorted((root / "scripts").glob("*.py"))
    files += sorted((root / "scripts").glob("*.sh"))
    if include_docs:
        docs = root / "docs"
        if docs.is_dir():
            files += sorted(docs.rglob("*.md"))
        files += sorted(root.glob("*.md"))
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            files.append(pyproject)
        for relative in (".github", ".codex"):
            surface = root / relative
            if surface.is_dir():
                files += sorted(
                    path
                    for path in surface.rglob("*")
                    if path.is_file()
                    and path.suffix in {".md", ".py", ".sh", ".toml", ".yaml", ".yml"}
                )
    return sorted(set(files))


def find_adr_reference_violations(
    root: Path | str, include_docs: bool = True
) -> list[Violation]:
    """Return every ``ADR ####`` citation under ``root`` that fails to resolve.

    A citation resolves when exactly one record in the same namespace matches
    its number. Zero matches is a "missing" violation; two or more is
    "ambiguous". Records that are never cited produce nothing.
    """
    root = Path(root)
    index = _adr_index(root)
    violations: list[Violation] = []
    for path in _citing_files(root, include_docs=include_docs):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _CITATION.finditer(line):
                namespace, number = match.groups()
                found = index.get((namespace, number), [])
                if len(found) == 1:
                    continue
                reason = "missing" if not found else "ambiguous"
                rels = [p.relative_to(root).as_posix() for p in found]
                violations.append(
                    Violation(rel, namespace, number, lineno, reason, rels)
                )
    return violations


class RealRepoAdrReferences(unittest.TestCase):
    """Bind this repository's citations to its decision records."""

    def test_every_cited_adr_resolves(self) -> None:
        violations = find_adr_reference_violations(REPO_ROOT)
        detail = "\n".join(v.describe() for v in violations)
        self.assertEqual(
            violations,
            [],
            "Every ADR/SADR citation must resolve exactly once in its own "
            f"docs/adr namespace:\n{detail}",
        )

    def test_scan_actually_reads_scripts(self) -> None:
        # Guard against a silent pass caused by scanning nothing: the scan must
        # include scripts/*.py, the surface the acceptance check names.
        files = _citing_files(REPO_ROOT)
        self.assertTrue(
            any(f.match("scripts/*.py") for f in files),
            "the citation scan must cover scripts/*.py",
        )

    def test_platform_citations_do_not_use_unresolved_numeric_shorthand(self) -> None:
        shorthand = re.compile(r"SADR-\d{4}/\d{4}")
        offenders = []
        for path in _citing_files(REPO_ROOT):
            text = path.read_text(encoding="utf-8")
            if shorthand.search(text):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(
            offenders,
            [],
            "repeat the SADR prefix so every cited number is independently resolvable",
        )

    def test_carried_records_are_indexed(self) -> None:
        # The two records this Goal carried onto the default branch, and which
        # the scripts cite most, must be present.
        index = _adr_index(REPO_ROOT)
        self.assertIn(("SADR", "0016"), index)
        self.assertIn(("SADR", "0017"), index)

    def test_platform_tree_has_only_sadr_0001_through_0019(self) -> None:
        adr_dir = REPO_ROOT / "docs" / "adr"
        sadrs = sorted(adr_dir.glob("SADR-[0-9][0-9][0-9][0-9]-*.md"))
        self.assertEqual(
            {path.name.split("-", 2)[1] for path in sadrs},
            {f"{number:04d}" for number in range(1, 20)},
        )
        self.assertEqual(
            list(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")),
            [],
            "the Shiki platform tree must not retain legacy numeric SADR paths",
        )


class FixtureAdrReferences(unittest.TestCase):
    """Prove the check fails on a broken citation -- not merely passes today."""

    def _make_repo(self, adr_names: Sequence[str], script_body: str) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "docs" / "adr").mkdir(parents=True)
        for name in adr_names:
            (root / "docs" / "adr" / name).write_text("# stub\n", encoding="utf-8")
        (root / "scripts" / "sample.py").write_text(script_body, encoding="utf-8")
        return root

    def test_fails_when_citation_has_no_document(self) -> None:
        # A script cites ADR 9991, but no docs/adr/9991-*.md exists.
        root = self._make_repo(
            adr_names=["0001-real.md"],
            script_body='"""See ADR 9991 for the rule."""\n',
        )
        violations = find_adr_reference_violations(root, include_docs=False)
        self.assertEqual(len(violations), 1)
        found = violations[0]
        self.assertEqual(found.number, "9991")
        self.assertEqual(found.reason, "missing")
        message = found.describe()
        self.assertIn("scripts/sample.py", message)  # citing file
        self.assertIn("9991", message)  # cited number
        self.assertIn("docs/adr/9991-*.md", message)  # missing path

    def test_target_adr_does_not_satisfy_missing_platform_sadr(self) -> None:
        root = self._make_repo(
            adr_names=["0042-target-decision.md"],
            script_body='"""Target ADR 0042 exists, but platform SADR-0042 does not."""\n',
        )
        violations = find_adr_reference_violations(root, include_docs=False)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].namespace, "SADR")
        self.assertEqual(violations[0].number, "0042")
        self.assertEqual(violations[0].expected_path, "docs/adr/SADR-0042-*.md")

    def test_target_adr_and_platform_sadr_may_reuse_a_number(self) -> None:
        root = self._make_repo(
            adr_names=["0042-target-decision.md", "SADR-0042-platform-decision.md"],
            script_body='"""Target ADR 0042 and platform SADR-0042 are distinct."""\n',
        )
        self.assertEqual(
            find_adr_reference_violations(root, include_docs=False), []
        )

    def test_uncited_adr_is_allowed(self) -> None:
        # An ADR exists but nothing cites it -> not a violation. The converse
        # (every ADR must be cited) is explicitly NOT required.
        root = self._make_repo(
            adr_names=["0001-real.md", "0042-never-cited.md"],
            script_body='"""Only mentions ADR 0001 here."""\n',
        )
        self.assertEqual(
            find_adr_reference_violations(root, include_docs=False), []
        )

    def test_ambiguous_when_two_documents_share_a_number(self) -> None:
        # "Exactly one" -- two records claiming the same number is a violation.
        root = self._make_repo(
            adr_names=["0007-a.md", "0007-b.md"],
            script_body='"""Cites ADR 0007 once."""\n',
        )
        violations = find_adr_reference_violations(root, include_docs=False)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].reason, "ambiguous")

    def test_passes_when_citation_resolves(self) -> None:
        root = self._make_repo(
            adr_names=["0016-real.md"],
            script_body='"""Cites ADR 0016 which exists."""\n',
        )
        self.assertEqual(
            find_adr_reference_violations(root, include_docs=False), []
        )


if __name__ == "__main__":
    unittest.main()
