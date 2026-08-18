"""Every place two artifacts must agree, but nothing binds them, gets a binder here.

Shiki has many pairs of artifacts that must say the same thing: a hardening in a
schema and the validator that runs it; a check-name literal in Python and the
workflow job that publishes that check; a vocabulary declared in a constant and
the JSON Schema enum that mirrors it. Where something *binds* such a pair, a break
fails immediately and names the fix — ``validate_shiki`` raises ``type enum must
match ...`` the moment ``ledger.schema.json`` and ``LEDGER_TYPES`` diverge. Where
nothing binds them, a break is silent and expensive: the CCA transport schema
declared checklist items as ``{type: object, additionalProperties: true}`` while
the repository schema required named fields, nothing compared them, and six shape
violations reached production over 2026-08-03/04 at one-to-three CCA re-runs each.

This module is one binder per pair for fourteen agreements that hold on the tree
today. Each ``*_holds`` test pins the live agreement; each ``*_divergence_*`` test
feeds the same comparison a mutated copy of one side to prove the binder actually
fails when the agreement breaks (not merely that it passes today). Every failure
message names BOTH sites and the fix, in the style of
``tests/test_cca_verdict_consistency.py``.

TARGET VALIDITY. ``scripts/test_shiki_init.sh`` installs Shiki into a fresh target
and runs that target's ``python3 -m unittest discover -s tests``. So every
assertion here reads only artifacts that ship (``scripts/shiki_installer.py``
``TEMPLATE_PATHS``) and tolerates an empty ``.shiki`` mirror: config/schema/doc
files under ``.shiki`` and the shipped scripts, workflows, and docs are present in
a target; per-record mirror files (tasks, memories, ...) may be absent.

The relation is SET-EQUALITY unless a pair's correct relation is genuinely
containment or subset (a literal must be a real job name; a referenced id must be
defined; a prose enumeration must not invent a value) — those say so in the
message. Set-equality is deliberate: a needle/containment check passes while a
site silently *gains* an entry the other lacks, which is the failure mode this
module exists to remove.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (puts scripts/ on sys.path)

import enforce_cca_verdict
import mergegate_check
import shiki_config
import shiki_contracts
import shiki_guardian
import shiki_jsonschema
import shiki_loop
import shiki_manifest
import shiki_memory
import shiki_runtime_adapters
import shiki_schema
import validate_shiki
from shiki_installer import TEMPLATE_PATHS

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCHEMAS_DIR = REPO_ROOT / ".shiki" / "schemas"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DOCS_AGENTS = REPO_ROOT / "docs" / "agents"

MUTANT = "__paired_invariants_mutant__"

# The workflow files that ship (TEMPLATE_PATHS names each one explicitly).
SHIPPED_WORKFLOWS = sorted(
    path.split("/")[-1]
    for path in TEMPLATE_PATHS
    if path.startswith(".github/workflows/") and path.endswith(".yml")
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path):
    return json.loads(read_text(path))


def workflow_job_names() -> set[str]:
    """Every job-level ``name:`` value across the shipped workflows.

    In these workflows a job name is a ``name:`` at exactly four leading spaces
    (a job property under ``jobs.<id>:``); the workflow name is at column 0 and
    step names are ``- name:`` at six-plus spaces, so the four-space anchor
    selects jobs and only jobs. A GitHub required status check is published under
    its job's display name, so these are the strings a check-name literal must
    match.
    """
    names: set[str] = set()
    for filename in SHIPPED_WORKFLOWS:
        path = WORKFLOWS_DIR / filename
        if not path.is_file():
            continue
        for line in read_text(path).splitlines():
            match = re.match(r"^    name:\s*(\S.*?)\s*$", line)
            if match:
                names.add(match.group(1))
    return names


def schema_keywords_used(schema, into: set[str]) -> None:
    """Collect every JSON Schema *keyword* used anywhere in ``schema``.

    Schema-aware: the keys of a schema object are keywords, but it recurses into
    sub-schemas only through ``properties`` values, ``items``, and a schema-valued
    ``additionalProperties`` — never treating a property *name* (``verdict``,
    ``summary``) or an ``enum`` value as if it were a keyword.
    """
    if not isinstance(schema, dict):
        return
    into.update(schema.keys())
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for sub in properties.values():
            schema_keywords_used(sub, into)
    items = schema.get("items")
    if isinstance(items, dict):
        schema_keywords_used(items, into)
    elif isinstance(items, list):
        for sub in items:
            schema_keywords_used(sub, into)
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        schema_keywords_used(additional, into)


def shiki_schema_implemented_keywords() -> set[str]:
    """The JSON Schema keywords ``shiki_schema.validate_instance`` actually reads.

    Derived from the module source: every ``schema.get("<kw>")`` and
    ``"<kw>" in schema``. A keyword the validator does not read constrains
    nothing at runtime, so a schema it validates must not rely on one.
    """
    source = read_text(SCRIPTS_DIR / "shiki_schema.py")
    getters = set(re.findall(r'schema\.get\(\s*["\'](\$?\w+)["\']', source))
    contains = set(re.findall(r'["\'](\$?\w+)["\']\s+in\s+schema', source))
    return getters | contains


def enumerated_backtick_tokens(text: str, anchor: str) -> set[str]:
    """Lowercase backtick tokens enumerated in the list/table right after ``anchor``.

    From the first line containing ``anchor``, scan the following block and take
    the first `` `token` `` of each enumeration line (``-``/``*`` bullet or ``|``
    table row), stopping at the first blank line after the enumeration begins.
    This reads the verdict enumeration a doc presents and nothing else (it does
    not wander into an unrelated backtick list elsewhere in the file).
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if anchor in line), None)
    if start is None:
        return set()
    tokens: set[str] = set()
    started = False
    for line in lines[start + 1 : start + 1 + 20]:
        stripped = line.strip()
        if re.match(r"^[-*|]", stripped):
            started = True
            match = re.search(r"`([a-z_]+)`", line)
            if match:
                tokens.add(match.group(1))
        elif started:
            break
    return tokens


class PairInvariant(unittest.TestCase):
    """Shared set-equality / containment assertions that name both sites."""

    def assert_all_equal(self, named: "dict[str, set[str]]", *, relation: str, fix: str) -> None:
        items = [(name, set(value)) for name, value in named.items()]
        self.assertTrue(items, "no sites to compare")
        ref_name, ref = items[0]
        disagreements = []
        for name, value in items[1:]:
            if value != ref:
                only_here = sorted(value - ref)
                only_ref = sorted(ref - value)
                disagreements.append(
                    f"{name} vs {ref_name}: only in {name}={only_here}, only in {ref_name}={only_ref}"
                )
        if disagreements:
            self.fail(
                f"{relation}\nSites: {', '.join(name for name, _ in items)}\n"
                + "\n".join(disagreements)
                + f"\nFix: {fix}"
            )

    def assert_subset(
        self, subset, superset, *, subset_name: str, superset_name: str, relation: str, fix: str
    ) -> None:
        extras = sorted(set(subset) - set(superset))
        if extras:
            self.fail(
                f"{relation}\n{subset_name} has entries absent from {superset_name}: {extras}\n"
                f"Fix: {fix}"
            )


# ---------------------------------------------------------------------------
# Pair 1 — schemas validated by shiki_schema.validate_instance use only the
# keywords that validator implements (else a hardening constrains nothing).
# ---------------------------------------------------------------------------
class Pair01SchemaKeywords(PairInvariant):
    # `enforce_cca_verdict.validate_verdict` passes the cca-verdict and
    # repair-packet schemas to `shiki_schema.validate_instance`
    # (enforce_cca_verdict.py:317-329). Metadata keys carry no constraint and are
    # allowed alongside the implemented constraint keywords.
    METADATA = {"$schema", "$id", "title", "description"}
    REACHED = ("cca-verdict", "repair-packet")

    def allowed(self) -> set[str]:
        return shiki_schema_implemented_keywords() | self.METADATA

    def test_declared_subset_is_consistent_with_implemented(self):
        # The implemented subset must be a subset of what shiki_jsonschema
        # DECLARES supported (SUPPORTED_KEYWORDS); otherwise the two validators
        # disagree about the bounded subset itself.
        self.assert_subset(
            shiki_schema_implemented_keywords(),
            shiki_jsonschema.SUPPORTED_KEYWORDS,
            subset_name="scripts/shiki_schema.py implemented keywords",
            superset_name="shiki_jsonschema.SUPPORTED_KEYWORDS",
            relation="subset: every keyword shiki_schema.validate_instance implements must be declared supported",
            fix="add the keyword to shiki_jsonschema.SUPPORTED_KEYWORDS or stop reading it in shiki_schema.validate_instance",
        )

    def test_holds(self):
        allowed = self.allowed()
        for name in self.REACHED:
            used: set[str] = set()
            schema_keywords_used(load_json(SCHEMAS_DIR / f"{name}.schema.json"), used)
            self.assert_subset(
                used,
                allowed,
                subset_name=f".shiki/schemas/{name}.schema.json keywords",
                superset_name="keywords shiki_schema.validate_instance implements",
                relation="subset: a schema shiki_schema.validate_instance runs must use only implemented keywords",
                fix="implement the keyword in scripts/shiki_schema.py validate_instance, or remove it from the schema (it constrains nothing today)",
            )

    def test_divergence_is_detected(self):
        # A schema hardened with an unimplemented keyword (e.g. `oneOf`) must be
        # caught: shiki_schema.validate_instance would ignore it.
        used: set[str] = set()
        schema_keywords_used({"type": "object", "oneOf": [{"type": "string"}]}, used)
        with self.assertRaises(AssertionError):
            self.assert_subset(
                used,
                self.allowed(),
                subset_name="mutated schema keywords",
                superset_name="implemented keywords",
                relation="subset",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 2 — every check-name literal in Python is a real workflow job name.
# ---------------------------------------------------------------------------
class Pair02CheckNameLiterals(PairInvariant):
    def literals(self) -> set[str]:
        evidence_jobs = set(
            re.findall(r'"job"\s*:\s*"([^"]+)"', read_text(SCRIPTS_DIR / "shiki_evidence.py"))
        )
        return (
            set(mergegate_check.SELF_CHECKS)
            | set(mergegate_check.VERDICT_CHECKS)
            | {shiki_loop.CCA_VERDICT_CHECK}
            | evidence_jobs
        )

    def test_holds(self):
        self.assert_subset(
            self.literals(),
            workflow_job_names(),
            subset_name="check-name literals (mergegate_check.SELF_CHECKS/VERDICT_CHECKS, shiki_loop.CCA_VERDICT_CHECK, shiki_evidence job)",
            superset_name="shipped workflow job names",
            relation="containment: each check-name literal must be a real workflow job display name",
            fix="rename the literal to match the workflow job `name:`, or add/rename the workflow job — a check that never runs blocks MergeGate forever",
        )

    def test_divergence_is_detected(self):
        with self.assertRaises(AssertionError):
            self.assert_subset(
                self.literals() | {MUTANT},
                workflow_job_names(),
                subset_name="check-name literals",
                superset_name="shipped workflow job names",
                relation="containment",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 3 — mergegate's record-directory set equals the manifest's record dirs.
# ---------------------------------------------------------------------------
class Pair03MirrorDirs(PairInvariant):
    # `_GENERIC_MIRROR_DIRS` are the generic-rule dirs; the six with dedicated
    # rules (its own comment names tasks/goals/locks/ledger/repairs/plans) are
    # excluded from it. Their union is every `.shiki` record directory mergegate
    # scope-protects. The manifest is authoritative: those directories are its
    # per-record classes — `mirror` for all but `ledger`, which is
    # `append-only-evidence`.
    DEDICATED = {"tasks", "goals", "locks", "ledger", "repairs", "plans"}
    RECORD_CLASSES = {"mirror", "append-only-evidence"}

    def mergegate_dirs(self) -> set[str]:
        return set(mergegate_check._GENERIC_MIRROR_DIRS) | self.DEDICATED

    def manifest_dirs(self) -> set[str]:
        manifest = shiki_manifest.load_manifest(REPO_ROOT)
        result: set[str] = set()
        for path, meta in shiki_manifest.manifest_directories(manifest).items():
            if not isinstance(path, str) or not isinstance(meta, dict):
                continue
            if meta.get("state_class") not in self.RECORD_CLASSES or meta.get("tracked") is not True:
                continue
            if path.startswith(".shiki/"):
                result.add(path[len(".shiki/") :].strip("/"))
        return result

    def test_holds(self):
        self.assert_all_equal(
            {
                "mergegate_check._GENERIC_MIRROR_DIRS ∪ dedicated dirs": self.mergegate_dirs(),
                ".shiki/manifest.json record dirs (state_class mirror/append-only-evidence)": self.manifest_dirs(),
            },
            relation="set-equality: every .shiki record directory the manifest declares must be covered by exactly one mergegate rule (generic or dedicated), and vice versa; ledger is append-only-evidence, the rest mirror",
            fix="add the directory to mergegate_check._GENERIC_MIRROR_DIRS (or give it a dedicated rule) and to .shiki/manifest.json, so no record dir is left unscoped",
        )

    def test_divergence_is_detected(self):
        with self.assertRaises(AssertionError):
            self.assert_all_equal(
                {
                    "mergegate": self.mergegate_dirs() | {MUTANT},
                    "manifest": self.manifest_dirs(),
                },
                relation="set-equality",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 4 — every PR-body producer emits the headings the metadata check requires.
# ---------------------------------------------------------------------------
class Pair04PrBodyHeadings(PairInvariant):
    def required_headings(self) -> list[str]:
        # Read the gate's own list so this test tracks it
        # (mergegate_check.py: `for heading in [...]`).
        source = read_text(SCRIPTS_DIR / "mergegate_check.py")
        match = re.search(r"for heading in \[([^\]]*)\]:", source)
        self.assertIsNotNone(match, "could not locate the MergeGate required-heading list")
        headings = re.findall(r'"([^"]+)"', match.group(1))
        self.assertTrue(headings, "MergeGate required-heading list parsed empty")
        return headings

    def sample_task(self) -> dict:
        return {
            "id": "T-0001",
            "goal_id": "G-0001",
            "scope": "Sample scope for the PR body producer.",
            "non_goals": ["nothing"],
            "acceptance_checks": ["a check"],
            "locks": ["path:x"],
            "risk_level": "low",
            "ledger_evidence": ["L-1"],
        }

    def producer_bodies(self) -> "dict[str, str]":
        task = self.sample_task()
        from shiki_github import github_pr_body

        bodies = {
            "shiki_github.github_pr_body": github_pr_body(task),
            "shiki_loop._closeout_pr_body": shiki_loop._closeout_pr_body(task, "G-0001", completes_goal=True),
        }
        # File templates. shiki-task.md ships (TEMPLATE_PATHS); the top-level
        # pull_request_template.md does not, so tolerate its absence in a target.
        for label, relative in (
            (".github/PULL_REQUEST_TEMPLATE/shiki-task.md", ".github/PULL_REQUEST_TEMPLATE/shiki-task.md"),
            (".github/pull_request_template.md", ".github/pull_request_template.md"),
        ):
            path = REPO_ROOT / relative
            if path.is_file():
                bodies[label] = read_text(path)
        return bodies

    def unsatisfied(self, body: str, headings: list[str]) -> list[str]:
        # Exactly the gate's own relation: a heading is satisfied when it appears
        # (case-insensitive) or as a markdown heading.
        return [
            heading
            for heading in headings
            if heading.lower() not in body.lower() and not mergegate_check.has_heading(body, heading)
        ]

    def test_holds(self):
        headings = self.required_headings()
        for label, body in self.producer_bodies().items():
            missing = self.unsatisfied(body, headings)
            self.assertEqual(
                missing,
                [],
                f"containment: PR-body producer {label} must satisfy every heading the "
                f"MergeGate metadata check requires (mergegate_check.py `for heading in [...]`); "
                f"missing {missing}. Fix: add the heading to {label} or the producer emits a PR "
                f"body the metadata check blocks.",
            )

    def test_divergence_is_detected(self):
        headings = self.required_headings()
        broken = "## Scope\n## Acceptance\n## Evidence\n"  # no MergeGate heading
        self.assertNotEqual(self.unsatisfied(broken, headings), [])


# ---------------------------------------------------------------------------
# Pair 5 — CCA verdict values agree across schema/enforcer/loop, and prose
# enumerations and the workflow transport enum invent nothing new.
# ---------------------------------------------------------------------------
class Pair05CcaVerdictValues(PairInvariant):
    PROSE_SITES = (
        ("CLAUDE.md", REPO_ROOT / "CLAUDE.md", "Allowed CCA verdicts:"),
        ("AGENTS.md", REPO_ROOT / "AGENTS.md", "CCA verdict rules:"),
        ("docs/agents/completion-check-agent.md", DOCS_AGENTS / "completion-check-agent.md", "## Verdicts"),
        ("docs/agents/implementation-policy.md", DOCS_AGENTS / "implementation-policy.md", "CCA emits one of:"),
    )

    def canonical(self) -> set[str]:
        schema = load_json(SCHEMAS_DIR / "cca-verdict.schema.json")
        return set(schema["properties"]["verdict"]["enum"])

    def code_sites(self) -> "dict[str, set[str]]":
        return {
            ".shiki/schemas/cca-verdict.schema.json enum": self.canonical(),
            "enforce_cca_verdict.VALID_VERDICTS": set(enforce_cca_verdict.VALID_VERDICTS),
            "shiki_loop.CCA_VERDICT_VALUES": set(shiki_loop.CCA_VERDICT_VALUES),
        }

    def workflow_enum(self) -> set[str]:
        text = read_text(WORKFLOWS_DIR / "shiki-cca-completion.yml")
        match = re.search(r'"verdict"\s*:\s*\{\s*"enum"\s*:\s*\[([^\]]*)\]', text)
        self.assertIsNotNone(match, "CCA workflow transport verdict enum not found")
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    def test_code_sites_are_set_equal(self):
        self.assert_all_equal(
            self.code_sites(),
            relation="set-equality: the CCA verdict vocabulary must be identical across the schema, the enforcer, and the loop (shiki_loop.CCA_VERDICT_VALUES claims to mirror with no check)",
            fix="update .shiki/schemas/cca-verdict.schema.json enum, enforce_cca_verdict.VALID_VERDICTS, and shiki_loop.CCA_VERDICT_VALUES together",
        )

    def test_workflow_and_prose_are_contained(self):
        canonical = self.canonical()
        self.assert_subset(
            self.workflow_enum(),
            canonical,
            subset_name="shiki-cca-completion.yml transport verdict enum",
            superset_name="canonical CCA verdicts",
            relation="containment: the workflow transport schema must not offer a verdict outside the canonical set",
            fix="align the transport `verdict` enum in shiki-cca-completion.yml with cca-verdict.schema.json",
        )
        for label, path, anchor in self.PROSE_SITES:
            tokens = enumerated_backtick_tokens(read_text(path), anchor)
            self.assertTrue(
                tokens,
                f"expected to find a verdict enumeration under {anchor!r} in {label}",
            )
            self.assert_subset(
                tokens,
                canonical,
                subset_name=f"{label} verdict enumeration (under {anchor!r})",
                superset_name="canonical CCA verdicts",
                relation="containment: a prose verdict enumeration must not invent a verdict absent from the schema",
                fix=f"correct the enumeration in {label} to match cca-verdict.schema.json",
            )

    def test_divergence_is_detected(self):
        # A code site that gains a value the others lack.
        sites = self.code_sites()
        sites["enforce_cca_verdict.VALID_VERDICTS"] = set(enforce_cca_verdict.VALID_VERDICTS) | {"retry"}
        with self.assertRaises(AssertionError):
            self.assert_all_equal(sites, relation="set-equality", fix="n/a")
        # A prose enumeration that invents a verdict.
        text = "Allowed CCA verdicts:\n\n- `complete`\n- `retry`\n"
        tokens = enumerated_backtick_tokens(text, "Allowed CCA verdicts:")
        with self.assertRaises(AssertionError):
            self.assert_subset(
                tokens,
                self.canonical(),
                subset_name="mutated prose",
                superset_name="canonical",
                relation="containment",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 6 — risk vocabulary agrees across every constant, choice list, and schema.
# ---------------------------------------------------------------------------
class Pair06RiskVocabulary(PairInvariant):
    def cli_choice_sets(self) -> "list[set[str]]":
        source = read_text(SCRIPTS_DIR / "shiki_cli.py")
        blocks = re.findall(r'--risk-level"[^\n]*?choices=\[([^\]]*)\]', source)
        self.assertTrue(blocks, "no --risk-level choices found in shiki_cli.py")
        return [set(re.findall(r'"([^"]+)"', block)) for block in blocks]

    def sites(self) -> "dict[str, set[str]]":
        task_schema = load_json(SCHEMAS_DIR / "task.schema.json")
        named = {
            "validate_shiki.RISK_LEVELS": set(validate_shiki.RISK_LEVELS),
            "shiki_guardian.KNOWN_RISK_LEVELS": set(shiki_guardian.KNOWN_RISK_LEVELS),
            "mergegate_check._RISK_ORDER": set(mergegate_check._RISK_ORDER),
            "task.schema.json risk_level enum": set(task_schema["properties"]["risk_level"]["enum"]),
        }
        for index, choices in enumerate(self.cli_choice_sets()):
            named[f"shiki_cli.py --risk-level choices #{index + 1}"] = choices
        return named

    def test_holds(self):
        self.assert_all_equal(
            self.sites(),
            relation="set-equality: the risk vocabulary must be identical across validate_shiki, shiki_guardian, mergegate_check, the shiki_cli --risk-level choices, and task.schema.json",
            fix="change every risk-level site together — a value one site accepts and another rejects makes a task unrepresentable",
        )

    def test_divergence_is_detected(self):
        sites = self.sites()
        sites["shiki_guardian.KNOWN_RISK_LEVELS"] = set(shiki_guardian.KNOWN_RISK_LEVELS) | {MUTANT}
        with self.assertRaises(AssertionError):
            self.assert_all_equal(sites, relation="set-equality", fix="n/a")


# ---------------------------------------------------------------------------
# Pair 7 — code-review verdict vocabulary agrees across transport enum,
# constant, prompt prose, and the bare literals the loop branches on.
# ---------------------------------------------------------------------------
class Pair07CodeReviewVerdicts(PairInvariant):
    def sites(self) -> "dict[str, set[str]]":
        transport = json.loads(shiki_runtime_adapters.CODE_REVIEW_VERDICT_SCHEMA)
        loop_source = read_text(SCRIPTS_DIR / "shiki_loop.py")
        bare = set(re.findall(r'"verdict"\s*:\s*"([a-z_]+)"', loop_source)) | set(
            re.findall(r'get\("verdict"\)\s*==\s*"([a-z_]+)"', loop_source)
        )
        return {
            "shiki_runtime_adapters.CODE_REVIEW_VERDICT_SCHEMA enum": set(transport["properties"]["verdict"]["enum"]),
            "shiki_runtime_adapters.CODE_REVIEW_VALID_VERDICTS": set(shiki_runtime_adapters.CODE_REVIEW_VALID_VERDICTS),
            "shiki_loop._CODE_REVIEW_PROMPT quoted verdicts": set(re.findall(r'"([a-z]+)"', shiki_loop._CODE_REVIEW_PROMPT)),
            "shiki_loop.py bare verdict literals": bare,
        }

    def test_holds(self):
        self.assert_all_equal(
            self.sites(),
            relation="set-equality: the code-review verdict vocabulary must be identical across the adapter transport enum, CODE_REVIEW_VALID_VERDICTS, the loop's review prompt, and the bare literals the loop gates on",
            fix="change the transport enum, CODE_REVIEW_VALID_VERDICTS, _CODE_REVIEW_PROMPT, and the shiki_loop verdict literals together",
        )

    def test_divergence_is_detected(self):
        sites = self.sites()
        sites["shiki_loop.py bare verdict literals"] = sites["shiki_loop.py bare verdict literals"] | {"blocked"}
        with self.assertRaises(AssertionError):
            self.assert_all_equal(sites, relation="set-equality", fix="n/a")


# ---------------------------------------------------------------------------
# Pair 8 — every DEFAULT_REQUIRED_CHECKS entry is a real workflow job name.
# ---------------------------------------------------------------------------
class Pair08DefaultRequiredChecks(PairInvariant):
    def test_holds(self):
        self.assert_subset(
            set(shiki_contracts.DEFAULT_REQUIRED_CHECKS),
            workflow_job_names(),
            subset_name="shiki_contracts.DEFAULT_REQUIRED_CHECKS",
            superset_name="shipped workflow job names",
            relation="containment: every fallback required check must be a real workflow job display name (validate_shiki binds only the config.yaml path, never this fallback)",
            fix="align shiki_contracts.DEFAULT_REQUIRED_CHECKS with the workflow job `name:` values — a required check with no job can never turn green",
        )

    def test_divergence_is_detected(self):
        with self.assertRaises(AssertionError):
            self.assert_subset(
                set(shiki_contracts.DEFAULT_REQUIRED_CHECKS) | {MUTANT},
                workflow_job_names(),
                subset_name="DEFAULT_REQUIRED_CHECKS",
                superset_name="job names",
                relation="containment",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 9 — memory vocabulary agrees between constants and schema; shipped
# memory records validate.
# ---------------------------------------------------------------------------
class Pair09MemoryVocabulary(PairInvariant):
    def schema(self) -> dict:
        return load_json(SCHEMAS_DIR / "memory-entry.schema.json")

    def test_statuses_and_areas_are_set_equal(self):
        schema = self.schema()
        self.assert_all_equal(
            {
                "shiki_memory.MEMORY_STATUSES": set(shiki_memory.MEMORY_STATUSES),
                "memory-entry.schema.json status enum": set(schema["properties"]["status"]["enum"]),
            },
            relation="set-equality: memory status vocabulary must match the schema enum",
            fix="update shiki_memory.MEMORY_STATUSES and memory-entry.schema.json status enum together",
        )
        self.assert_all_equal(
            {
                "shiki_memory.MEMORY_AREAS": set(shiki_memory.MEMORY_AREAS),
                "memory-entry.schema.json area enum": set(schema["properties"]["area"]["enum"]),
            },
            relation="set-equality: memory area vocabulary must match the schema enum",
            fix="update shiki_memory.MEMORY_AREAS and memory-entry.schema.json area enum together",
        )

    def test_shipped_memory_records_validate(self):
        # Tolerates an empty mirror: a target ships no memory records, so this is
        # a vacuous pass there and a real check in the platform.
        schema = self.schema()
        for record_path in sorted((REPO_ROOT / ".shiki" / "memories").glob("*.json")):
            with self.subTest(record=record_path.name):
                try:
                    shiki_schema.validate_instance(load_json(record_path), schema)
                except shiki_schema.SchemaValidationError as error:
                    self.fail(
                        f"shipped memory record {record_path} violates memory-entry.schema.json: {error}. "
                        f"Fix: correct the record or the schema."
                    )

    def test_divergence_is_detected(self):
        schema = self.schema()
        with self.assertRaises(AssertionError):
            self.assert_all_equal(
                {
                    "shiki_memory.MEMORY_AREAS": set(shiki_memory.MEMORY_AREAS) | {MUTANT},
                    "schema area enum": set(schema["properties"]["area"]["enum"]),
                },
                relation="set-equality",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 10 — workflow-file identity agrees across the contract map, the shipped
# paths, and the doctor's required-file list.
# ---------------------------------------------------------------------------
class Pair10WorkflowFiles(PairInvariant):
    def doctor_required_files(self) -> set[str]:
        source = read_text(SCRIPTS_DIR / "shiki_doctor.py")
        match = re.search(r"required_files\s*=\s*\[(.*?)\]", source, re.DOTALL)
        self.assertIsNotNone(match, "could not locate shiki_doctor required workflow file list")
        files = set(re.findall(r'"([^"]+\.yml)"', match.group(1)))
        self.assertTrue(files, "shiki_doctor required workflow file list parsed empty")
        return files

    def sites(self) -> "dict[str, set[str]]":
        return {
            "validate_shiki.WORKFLOW_CONTRACTS keys": set(validate_shiki.WORKFLOW_CONTRACTS),
            "shiki_installer.TEMPLATE_PATHS .github/workflows entries": set(SHIPPED_WORKFLOWS),
            "shiki_doctor.py required workflow files": self.doctor_required_files(),
        }

    def test_holds(self):
        self.assert_all_equal(
            self.sites(),
            relation="set-equality: the set of Shiki workflow files must be identical in validate_shiki.WORKFLOW_CONTRACTS, the TEMPLATE_PATHS shipped set, and shiki_doctor's required-file list",
            fix="add or remove the workflow in all three places — a workflow shipped but uncontracted (or contracted but unshipped) drifts silently",
        )

    def test_divergence_is_detected(self):
        sites = self.sites()
        sites["shiki_doctor.py required workflow files"] = self.doctor_required_files() | {"ghost.yml"}
        with self.assertRaises(AssertionError):
            self.assert_all_equal(sites, relation="set-equality", fix="n/a")


# ---------------------------------------------------------------------------
# Pair 11 — repair-packet required_skill enum is a subset of KNOWN_SKILLS.
# ---------------------------------------------------------------------------
class Pair11RepairSkillSubset(PairInvariant):
    def enum(self) -> set[str]:
        schema = load_json(SCHEMAS_DIR / "repair-packet.schema.json")
        return set(schema["properties"]["required_skill"]["enum"])

    def test_holds(self):
        # SUBSET, not equality: KNOWN_SKILLS is broader (planning skills a repair
        # packet never names); the audit measured the difference as empty.
        self.assert_subset(
            self.enum(),
            validate_shiki.KNOWN_SKILLS,
            subset_name="repair-packet.schema.json required_skill enum",
            superset_name="validate_shiki.KNOWN_SKILLS",
            relation="subset: every required_skill a repair packet may name must be a skill validate_shiki recognizes (KNOWN_SKILLS is intentionally broader)",
            fix="add the skill to validate_shiki.KNOWN_SKILLS, or remove it from the repair-packet required_skill enum",
        )

    def test_divergence_is_detected(self):
        with self.assertRaises(AssertionError):
            self.assert_subset(
                self.enum() | {"nonexistent-skill"},
                validate_shiki.KNOWN_SKILLS,
                subset_name="required_skill enum",
                superset_name="KNOWN_SKILLS",
                relation="subset",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 12 — every checklist id referenced outside checklists.md is defined in it.
# ---------------------------------------------------------------------------
class Pair12ChecklistIds(PairInvariant):
    CHECKLISTS = DOCS_AGENTS / "checklists.md"

    def defined_ids(self) -> set[str]:
        # Checklist ids are `FAMILY-NN` with a 2+-letter family and exactly two
        # digits (word-bounded). The two-digit bound keeps 4-digit ADR numbers
        # (`SADR-0015`) out.
        return set(re.findall(r"\b[A-Z]{2,5}-[0-9]{2}\b", read_text(self.CHECKLISTS)))

    def reference_files(self) -> "list[Path]":
        files = [
            REPO_ROOT / "AGENTS.md",
            REPO_ROOT / "CLAUDE.md",
            SCRIPTS_DIR / "enforce_cca_verdict.py",
        ]
        files += sorted(p for p in DOCS_AGENTS.glob("*.md") if p != self.CHECKLISTS)
        files += sorted(WORKFLOWS_DIR / name for name in SHIPPED_WORKFLOWS)
        return [p for p in files if p.is_file()]

    def referenced_ids(self) -> set[str]:
        # Restrict references to the KNOWN families so unrelated `FAMILY-NN`-shaped
        # tokens (e.g. `UTF-16`) never masquerade as checklist ids and false-fail.
        families = sorted({token.split("-")[0] for token in self.defined_ids()})
        pattern = re.compile(r"\b(?:" + "|".join(families) + r")-[0-9]{2}\b")
        referenced: set[str] = set()
        for path in self.reference_files():
            referenced.update(pattern.findall(read_text(path)))
        return referenced

    def test_holds(self):
        self.assert_subset(
            self.referenced_ids(),
            self.defined_ids(),
            subset_name="checklist ids referenced outside docs/agents/checklists.md",
            superset_name="ids defined in docs/agents/checklists.md",
            relation="containment: every checklist id referenced elsewhere must be defined in docs/agents/checklists.md",
            fix="define the id in docs/agents/checklists.md, or correct the reference — a reference to an undefined id is unresolvable",
        )

    def test_divergence_is_detected(self):
        with self.assertRaises(AssertionError):
            self.assert_subset(
                self.referenced_ids() | {"CCA-99"},
                self.defined_ids(),
                subset_name="referenced ids",
                superset_name="defined ids",
                relation="containment",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 13 — config.yaml guardian identity agrees with guardian-policy.json.
# ---------------------------------------------------------------------------
class Pair13GuardianIdentity(PairInvariant):
    def loaded(self):
        config_guardian = shiki_config.load_shiki_config(REPO_ROOT).get("guardian", {})
        policy = load_json(REPO_ROOT / ".shiki" / "guardian-policy.json")
        return config_guardian, policy

    def test_holds(self):
        config_guardian, policy = self.loaded()
        self.assert_all_equal(
            {
                ".shiki/config.yaml guardian.users": set(config_guardian.get("users", [])),
                ".shiki/guardian-policy.json approvers.users": set(policy["approvers"]["users"]),
            },
            relation="set-equality: the Guardian approver users must be identical in config.yaml and guardian-policy.json",
            fix="update guardian.users in .shiki/config.yaml and approvers.users in .shiki/guardian-policy.json together",
        )
        self.assert_all_equal(
            {
                ".shiki/config.yaml guardian.labels": set(config_guardian.get("labels", [])),
                ".shiki/guardian-policy.json guardian_label.label": {policy["approval_sources"]["guardian_label"]["label"]},
            },
            relation="set-equality: the Guardian approval label must be identical in config.yaml and guardian-policy.json",
            fix="update guardian.labels in .shiki/config.yaml and approval_sources.guardian_label.label in .shiki/guardian-policy.json together",
        )

    def test_divergence_is_detected(self):
        config_guardian, policy = self.loaded()
        with self.assertRaises(AssertionError):
            self.assert_all_equal(
                {
                    "config users": set(config_guardian.get("users", [])) | {MUTANT},
                    "policy users": set(policy["approvers"]["users"]),
                },
                relation="set-equality",
                fix="n/a",
            )


# ---------------------------------------------------------------------------
# Pair 14 — smoke/start required-key sets agree with their schemas.
# ---------------------------------------------------------------------------
class Pair14SmokeStartRequired(PairInvariant):
    def sites(self, kind: str):
        constant = validate_shiki.SMOKE_REQUIRED if kind == "smoke" else validate_shiki.START_REQUIRED
        schema = load_json(SCHEMAS_DIR / f"{kind}.schema.json")
        return {
            f"validate_shiki.{kind.upper()}_REQUIRED": set(constant),
            f"{kind}.schema.json required": set(schema["required"]),
        }

    def test_holds(self):
        for kind in ("smoke", "start"):
            self.assert_all_equal(
                self.sites(kind),
                relation=f"set-equality: {kind} required keys must match between validate_shiki and {kind}.schema.json",
                fix=f"update validate_shiki.{kind.upper()}_REQUIRED and {kind}.schema.json required together",
            )

    def test_divergence_is_detected(self):
        sites = self.sites("start")
        sites["validate_shiki.START_REQUIRED"] = set(validate_shiki.START_REQUIRED) | {MUTANT}
        with self.assertRaises(AssertionError):
            self.assert_all_equal(sites, relation="set-equality", fix="n/a")


class Pair15ForkAndMergeBaseWorkflowEvidence(PairInvariant):
    WORKFLOWS = (
        WORKFLOWS_DIR / "shiki-mergegate.yml",
        WORKFLOWS_DIR / "shiki-cca-completion.yml",
    )

    @staticmethod
    def _gh_pr_view_field_lists(text: str) -> list[set[str]]:
        fields: list[set[str]] = []
        for match in re.finditer(r'gh pr view\s+"\$[^\"]+"', text):
            command_tail = text[match.start() : match.start() + 500]
            json_match = re.search(r"--json\s+([^\s\\]+)", command_tail)
            if json_match:
                fields.append(set(json_match.group(1).split(",")))
        return fields

    def test_every_pr_view_json_field_list_requests_fork_identity(self):
        for workflow in self.WORKFLOWS:
            field_lists = self._gh_pr_view_field_lists(read_text(workflow))
            self.assertTrue(field_lists, f"{workflow.name}: no gh pr view --json field list found")
            for fields in field_lists:
                self.assertIn("isCrossRepository", fields, f"{workflow.name}: {sorted(fields)}")

    def test_both_exemption_sites_fail_closed_without_pr_short_circuit(self):
        mergegate = read_text(SCRIPTS_DIR / "mergegate_check.py")
        signal = read_text(SCRIPTS_DIR / "guardian_approval_signal.py")
        self.assertIn(
            'if bookkeeping_closeout and pr.get("isCrossRepository") is not False:',
            mergegate,
        )
        self.assertNotIn("bookkeeping_closeout and pr and pr.get", mergegate)
        self.assertIn(
            'if exemption and pr.get("isCrossRepository") is not False:',
            signal,
        )
        self.assertNotIn("exemption and pr and pr.get", signal)

    def test_closeout_keeps_mandatory_lock_release_and_no_relaxation(self):
        mergegate = read_text(SCRIPTS_DIR / "mergegate_check.py")
        self.assertIsNone(re.search(r"lockless|lock_terminal", mergegate))
        self.assertIn("if not task_changed or not lock_released:", mergegate)

    def test_workflows_archive_the_pinned_merge_base_without_swallowing_failure(self):
        for workflow in self.WORKFLOWS:
            text = read_text(workflow)
            self.assertIn('git archive "$merge_base" .shiki', text, workflow.name)
            self.assertNotIn('git archive "$merge_base" .shiki | tar -x -C .shiki/gha/base-shiki || true', text)
            self.assertIn("mergeBaseOid", text, workflow.name)


if __name__ == "__main__":
    unittest.main()
