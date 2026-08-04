"""ADR 0018 base-sync carry (guardian_comment_carried) reaches CI.

The verifier (`shiki_sync_proof.verify_pure_base_sync`) and the evaluator
(`shiki_guardian.evaluate_guardian_approval`) were correct and complete, but no
caller ever passed the flags, so the carry degraded to "does not apply" silently:
no error, feature invisible. This suite pins that every invocation that judges
Guardian approval across the two workflows now passes BOTH `--base-sync-carry` and
`--default-branch`, that each such step exports `DEFAULT_BRANCH` through `env:` and
references it as `"$DEFAULT_BRANCH"` (never a `${{ }}` expression in
`shiki-mergegate.yml`), and that dropping either flag from any one invocation fails
`validate_shiki` loudly.

The four judging invocations:

  1. signal          shiki-cca-completion.yml  cca        guardian_approval_signal.py
  2. guardian status shiki-cca-completion.yml  cca        shiki.py guardian status
  3. cca-mergegate   shiki-cca-completion.yml  mergegate  mergegate_check.py
  4. metadata        shiki-mergegate.yml       mergegate  mergegate_check.py

Two silent failure modes are also pinned here (both were real defects PR #206
removed): a base fetch that goes shallow (`--depth`) makes the git proof
unresolvable, and a second base fetch would do the same — either disables the carry
without any error.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import validate_shiki
from shiki_workflows import load_workflow_model, workflow_jobs

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CCA_WF = WORKFLOWS / "shiki-cca-completion.yml"
MERGEGATE_WF = WORKFLOWS / "shiki-mergegate.yml"
ORCHESTRATOR_WF = WORKFLOWS / "shiki-orchestrator.yml"

CARRY_FLAGS = ("--base-sync-carry", "--default-branch")
DEFAULT_BRANCH_ENV = "${{ github.event.repository.default_branch }}"

# Every judging invocation, keyed by the `python3 <command>` that runs it and the
# step whose run: body contains it. The step name is enough to scope a flag removal
# to exactly one invocation because each invocation lives in its own step.
JUDGING_INVOCATIONS = (
    ("signal", CCA_WF, "Prepare CCA context", "python3 scripts/guardian_approval_signal.py"),
    ("guardian-status", CCA_WF, "Post Guardian blockers comment", "python3 scripts/shiki.py guardian status"),
    ("cca-mergegate", CCA_WF, "Enforce MergeGate", "python3 scripts/mergegate_check.py"),
    ("metadata", MERGEGATE_WF, "MergeGate metadata check", "python3 scripts/mergegate_check.py"),
)


def _step_run_body(text: str, step_name: str) -> str:
    """The raw run: body region of the named step, from its `- name:` line to the
    next step at the same indent (or EOF)."""
    lines = text.split("\n")
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == f"- name: {step_name}"),
        None,
    )
    if start is None:
        raise AssertionError(f"step {step_name!r} not found")
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("- name:") and (len(lines[j]) - len(stripped)) == indent:
            end = j
            break
    return "\n".join(lines[start:end])


def _invocation_tail(run_body: str, command: str) -> str:
    """The invocation's argv, from the `python3 <command>` token to the end of the
    run body — the exact region validate_shiki reads flags from."""
    idx = run_body.find(command)
    if idx == -1:
        raise AssertionError(f"command {command!r} not found in run body")
    return run_body[idx:]


def _drop_flag_from_step(text: str, step_name: str, flag: str) -> str:
    """Remove the single `<flag> ...` continuation line from one step's run body.
    Each judging invocation is alone in its step, so exactly one such line exists;
    removing it drops the flag from that invocation and no other."""
    lines = text.split("\n")
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == f"- name: {step_name}"),
        None,
    )
    assert start is not None, f"step {step_name!r} not found"
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("- name:") and (len(lines[j]) - len(stripped)) == indent:
            end = j
            break
    out: list[str] = []
    removed = False
    for k, ln in enumerate(lines):
        if start <= k < end and not removed and ln.strip().split()[:1] == [flag]:
            removed = True
            continue
        out.append(ln)
    assert removed, f"flag {flag!r} not found in step {step_name!r}"
    return "\n".join(out)


def _stage(tmp: Path) -> Path:
    """A throwaway root holding every workflow validate_workflow_invocation_contracts
    reads: both required-flags workflows plus the orchestrator (expression-free)."""
    dest = tmp / ".github" / "workflows"
    dest.mkdir(parents=True, exist_ok=True)
    for wf in (CCA_WF, MERGEGATE_WF, ORCHESTRATOR_WF):
        shutil.copyfile(wf, dest / wf.name)
    return tmp


class EveryInvocationCarriesBothFlags(unittest.TestCase):
    def test_enumerates_exactly_four_and_all_carry_both_flags(self) -> None:
        # Enumerate every `python3 <judging command>` across both workflows and
        # assert both carry flags on each. The count anchors the enumeration: a new
        # judging invocation added without flags changes the total and fails here.
        seen: list[str] = []
        for label, wf, step_name, command in JUDGING_INVOCATIONS:
            text = wf.read_text(encoding="utf-8")
            self.assertEqual(text.count(command), 1, f"{label}: expected one {command!r}")
            tail = _invocation_tail(_step_run_body(text, step_name), command)
            for flag in CARRY_FLAGS:
                self.assertIn(flag, tail, f"{label} invocation must pass {flag}")
            seen.append(label)
        self.assertEqual(
            sorted(seen),
            sorted(l for l, *_ in JUDGING_INVOCATIONS),
            "every judging invocation must be enumerated",
        )
        # Cross-check the raw file: exactly four judging commands exist in total, so
        # no unenumerated invocation is hiding.
        total = 0
        for wf in (CCA_WF, MERGEGATE_WF):
            body = wf.read_text(encoding="utf-8")
            for command in {c for *_, c in JUDGING_INVOCATIONS}:
                total += body.count(command)
        self.assertEqual(total, 4, "exactly four judging invocations must exist")

    def test_each_step_exports_default_branch_and_references_it(self) -> None:
        # AC2: DEFAULT_BRANCH flows through env: (never re-derived), and the flag is
        # referenced as "$DEFAULT_BRANCH".
        for label, wf, step_name, command in JUDGING_INVOCATIONS:
            model = load_workflow_model(wf)
            step = self._find_step(model, step_name)
            env = step.get("env") or {}
            self.assertEqual(
                env.get("DEFAULT_BRANCH"),
                DEFAULT_BRANCH_ENV,
                f"{label} step must export DEFAULT_BRANCH from the repository default branch",
            )
            tail = _invocation_tail(_step_run_body(wf.read_text(encoding="utf-8"), step_name), command)
            self.assertIn('--default-branch "$DEFAULT_BRANCH"', tail, label)

    @staticmethod
    def _find_step(model: dict, step_name: str) -> dict:
        for job in workflow_jobs(model).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and step.get("name") == step_name:
                    return step
        raise AssertionError(f"step {step_name!r} not found")


class MergegateRunBodiesStayExpressionFree(unittest.TestCase):
    def test_metadata_run_body_uses_shell_var_not_expression(self) -> None:
        # shiki-mergegate.yml is in WORKFLOW_RUN_EXPRESSION_FREE: the carry must reach
        # the metadata check through "$DEFAULT_BRANCH", never a GitHub-expression
        # interpolation in the run body. Assert against the parsed run body — the
        # exact surface validate_shiki enforces — and against every run body in the
        # file so a future carry edit cannot smuggle an expression back in.
        model = load_workflow_model(MERGEGATE_WF)
        run_bodies = [
            step["run"]
            for job in workflow_jobs(model).values()
            if isinstance(job, dict)
            for step in (job.get("steps") or [])
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        ]
        for body in run_bodies:
            self.assertNotIn("${{", body, "shiki-mergegate.yml run bodies must be expression-free")
        metadata = next(b for b in run_bodies if "scripts/mergegate_check.py" in b)
        self.assertIn('--default-branch "$DEFAULT_BRANCH"', metadata)


class NoShallowOrSecondBaseFetch(unittest.TestCase):
    def test_no_base_fetch_carries_depth(self) -> None:
        # A shallow base makes the git proof unresolvable and silently disables the
        # carry (the exact defect PR #206 removed).
        for wf in (CCA_WF, MERGEGATE_WF):
            text = wf.read_text(encoding="utf-8")
            for line in text.split("\n"):
                if "git fetch" in line:
                    self.assertNotIn("--depth", line, f"{wf.name}: base fetch must not be shallow")

    def test_no_second_base_fetch_added(self) -> None:
        # The carry reuses each job's existing base fetch. The CCA workflow keeps
        # exactly two (one per job); the MergeGate metadata workflow keeps exactly
        # one. A second fetch would go shallow by default and defeat the proof.
        self.assertEqual(CCA_WF.read_text(encoding="utf-8").count("git fetch origin"), 2)
        self.assertEqual(MERGEGATE_WF.read_text(encoding="utf-8").count("git fetch origin"), 1)


class ValidateShikiGuardsEveryInvocation(unittest.TestCase):
    def test_repo_workflows_satisfy_contract(self) -> None:
        validate_shiki.validate_workflow_invocation_contracts(REPO_ROOT)

    def test_dropping_either_flag_from_any_invocation_fails(self) -> None:
        # AC4: one case per invocation per flag. Each of the four invocations, with
        # either carry flag removed, must fail validate_shiki.
        for label, wf, step_name, _command in JUDGING_INVOCATIONS:
            for flag in CARRY_FLAGS:
                with self.subTest(invocation=label, flag=flag):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = _stage(Path(tmp))
                        target = root / ".github" / "workflows" / wf.name
                        target.write_text(
                            _drop_flag_from_step(target.read_text(encoding="utf-8"), step_name, flag),
                            encoding="utf-8",
                        )
                        with self.assertRaises(validate_shiki.ValidationError):
                            validate_shiki.validate_workflow_invocation_contracts(root)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
