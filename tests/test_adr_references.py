"""Every ``ADR ####`` citation must resolve to exactly one decision record.

Background. ``scripts/mergegate_check.py`` justifies ``is_bookkeeping_closeout``
with "ADR 0017" nine times and ``scripts/guardian_approval_signal.py`` cites
0016/0017 seven times, yet for a stretch the numbering in ``docs/adr/`` stopped
at 0015: the authoring commits lived on a coordinator branch and no task ever
declared ``docs/adr/...`` in its locks, so nothing carried the records to the
default branch. The stated basis of an exemption that merged seven PRs with zero
Guardian approvals could not be read. ``grep -n 'docs/adr' scripts/validate_shiki.py``
returns nothing -- no validator required a cited ADR to exist, so the citations
and the directory drifted with no signal.

This is the same unbound-pair shape behind other silent failures: a reference
and its target with nothing asserting they agree. This test binds them. It scans
every ``scripts/*.py`` (plus ``docs/**/*.md`` and the root ``*.md`` governance
files, which are cheap) for the ``ADR ####`` pattern and asserts each cited
number resolves to exactly one ``docs/adr/####-*.md``. The converse is
deliberately NOT required: an ADR may exist without being cited.

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

# "ADR 0016" -- a four-digit citation. The trailing \b keeps a five-digit token
# from being read as a truncated four-digit number.
_CITATION = re.compile(r"\bADR (\d{4})\b")
# "0016-locks-bound-product-paths.md" -- the leading four digits name the record.
_ADR_FILENAME = re.compile(r"^(\d{4})-.*\.md$")


class Violation(NamedTuple):
    """A citation that does not resolve to exactly one decision record."""

    citing_file: str  # repo-relative path of the file that cites the ADR
    number: str  # the cited four-digit number, e.g. "0016"
    line: int  # 1-based line of the citation
    reason: str  # "missing" (zero matches) or "ambiguous" (two or more)
    matches: Sequence[str]  # repo-relative paths matching docs/adr/<n>-*.md

    @property
    def expected_path(self) -> str:
        return f"docs/adr/{self.number}-*.md"

    def describe(self) -> str:
        """Name the citing file, the cited number, and the missing path."""
        if self.reason == "missing":
            return (
                f"{self.citing_file}:{self.line} cites ADR {self.number} "
                f"but no document matches {self.expected_path}"
            )
        joined = ", ".join(self.matches)
        return (
            f"{self.citing_file}:{self.line} cites ADR {self.number} which "
            f"resolves to {len(self.matches)} documents (expected exactly one "
            f"at {self.expected_path}): {joined}"
        )


def _adr_index(root: Path) -> dict[str, list[Path]]:
    """Map every four-digit ADR number to the record files that claim it."""
    index: dict[str, list[Path]] = {}
    adr_dir = root / "docs" / "adr"
    if adr_dir.is_dir():
        for path in sorted(adr_dir.glob("*.md")):
            match = _ADR_FILENAME.match(path.name)
            if match:
                index.setdefault(match.group(1), []).append(path)
    return index


def _citing_files(root: Path, include_docs: bool = True) -> list[Path]:
    """Files scanned for citations: scripts/*.py, and cheaply the markdown."""
    files = sorted((root / "scripts").glob("*.py"))
    if include_docs:
        docs = root / "docs"
        if docs.is_dir():
            files += sorted(docs.rglob("*.md"))
        files += sorted(root.glob("*.md"))
    return files


def find_adr_reference_violations(
    root: Path | str, include_docs: bool = True
) -> list[Violation]:
    """Return every ``ADR ####`` citation under ``root`` that fails to resolve.

    A citation resolves when exactly one ``docs/adr/####-*.md`` matches its
    number. Zero matches is a "missing" violation; two or more is "ambiguous".
    Records that are never cited produce nothing -- the converse is not
    required.
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
                number = match.group(1)
                found = index.get(number, [])
                if len(found) == 1:
                    continue
                reason = "missing" if not found else "ambiguous"
                rels = [p.relative_to(root).as_posix() for p in found]
                violations.append(Violation(rel, number, lineno, reason, rels))
    return violations


class RealRepoAdrReferences(unittest.TestCase):
    """Bind this repository's citations to its decision records."""

    def test_every_cited_adr_resolves(self) -> None:
        violations = find_adr_reference_violations(REPO_ROOT)
        detail = "\n".join(v.describe() for v in violations)
        self.assertEqual(
            violations,
            [],
            "Every 'ADR ####' citation must resolve to exactly one "
            f"docs/adr/####-*.md:\n{detail}",
        )

    def test_scan_actually_reads_scripts(self) -> None:
        # Guard against a silent pass caused by scanning nothing: the scan must
        # include scripts/*.py, the surface the acceptance check names.
        files = _citing_files(REPO_ROOT)
        self.assertTrue(
            any(f.match("scripts/*.py") for f in files),
            "the citation scan must cover scripts/*.py",
        )

    def test_carried_records_are_indexed(self) -> None:
        # The two records this Goal carried onto the default branch, and which
        # the scripts cite most, must be present.
        index = _adr_index(REPO_ROOT)
        self.assertIn("0016", index)
        self.assertIn("0017", index)


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
