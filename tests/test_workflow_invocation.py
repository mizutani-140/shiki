"""Workflows must invoke the scripts they claim to, with the flags those scripts
need — the contract asserts the *invocation*, not the mere presence of strings.

Three gaps this pins:

1. ``.github/workflows/shiki-mergegate.yml`` metadata check ran
   ``mergegate_check.py`` WITHOUT ``--base-shiki``/``--merged-prs``, so a
   ``post_merge_reconcile`` PR always blocked ("requires a base .shiki snapshot")
   even though the CCA workflow already builds and passes exactly those inputs.
   The metadata job now builds the base snapshot in "Prepare PR context" and
   passes both flags, mirroring the CCA workflow. A test drives
   ``mergegate_check.main()`` with the metadata job's exact argv against a valid
   ``post_merge_reconcile`` fixture and proves it is ``ready``; the same argv with
   ``--base-shiki`` removed still blocks (fail-closed behaviour unchanged).

2. ``.github/workflows/shiki-orchestrator.yml`` commit-evidence step guarded with
   ``git diff --quiet`` — which only sees TRACKED files — so newly created
   (untracked) evidence reported "no changes" and the step exited 0 without
   committing. It now stages ``.shiki`` first and guards with
   ``git diff --cached --quiet``, so new-file-only evidence is committed and
   pushed.

3. The same workflow spliced comment- and input-derived GitHub expressions
   (``${{ ... }}``) straight into ``run:`` bodies, an injection surface. Those
   values now flow through ``env:`` and are referenced as quoted shell variables,
   so a command-substitution payload in a comment reaches the plan-file argument
   as a literal string.

4. ``validate_shiki`` now asserts the invocation contract: the metadata job passes
   the required flags, and the hardened workflows carry no ``${{ }}`` expression
   inside any ``run:`` body. Removing a flag or reintroducing an expression fails
   validation.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import mergegate_check
import validate_shiki
from shiki_workflows import load_workflow_model, workflow_jobs

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
MERGEGATE_WF = WORKFLOWS / "shiki-mergegate.yml"
ORCHESTRATOR_WF = WORKFLOWS / "shiki-orchestrator.yml"
# ADR 0018 base-sync carry: shiki-cca-completion.yml now has a top-level entry in
# validate_shiki.WORKFLOW_INVOCATION_REQUIRED_FLAGS, so the staged root the contract
# check runs against must include it or the loop FileNotFoundErrors (see _stage).
CCA_WF = WORKFLOWS / "shiki-cca-completion.yml"


# --- shared workflow-text helpers ----------------------------------------


def _step_run_body(workflow_path: Path, step_name: str) -> str:
    """The ``run:`` script of the ``- name: <step_name>`` step, raw from the file
    (comments preserved), joined verbatim."""
    lines = workflow_path.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"),
        None,
    )
    if start is None:
        raise AssertionError(f"step {step_name!r} not found in {workflow_path.name}")
    # Collect the block until the next `- name:` at the same or shallower indent.
    block = lines[start + 1 :]
    end = next((i for i, line in enumerate(block) if line.strip().startswith("- name:")), len(block))
    step_lines = [lines[start], *block[:end]]
    # Keep only the run: body lines (everything after `run: |` up to the block's end).
    text = "\n".join(step_lines)
    run_idx = text.find("run:")
    return text[run_idx:] if run_idx != -1 else text


def _job_step_runs(model: dict, job_id: str) -> list[str]:
    job = workflow_jobs(model).get(job_id)
    steps = job.get("steps") if isinstance(job, dict) else None
    if not isinstance(steps, list):
        return []
    return [s["run"] for s in steps if isinstance(s, dict) and isinstance(s.get("run"), str)]


def _mergegate_invocation() -> str:
    """The ``python3 scripts/mergegate_check.py ...`` command in the metadata job,
    from the parsed model, sliced from the script token to the end of the step."""
    model = load_workflow_model(MERGEGATE_WF)
    body = next((r for r in _job_step_runs(model, "mergegate") if "scripts/mergegate_check.py" in r), None)
    if body is None:
        raise AssertionError("metadata job has no mergegate_check.py invocation")
    return body[body.index("scripts/mergegate_check.py") :]


# --- AC1 / AC2: metadata job argv against a post_merge_reconcile fixture ---

GOAL_ID = "G-20260729T065622761503Z-4479b0d3"
TASK_ID = "T-20260729T065622765968Z-234bf98b"
LEDGER_ID = "L-20260729T070000000000Z-1234abcd"
HEAD_SHA = "b" * 40
IMPL_PR = 4242          # the reconciled task's merged implementation PR
RECONCILE_PR = 4243     # this post_merge_reconcile PR

# Flag -> fixture value used to reconstruct the metadata job's argv. Path flags
# keep the workflow's own relative paths (resolved under the fixture cwd);
# env-var flags map to the fixture's concrete values.
_FLAG_VALUES = {
    "--changed-files-status": ".shiki/gha/changed-files-status.txt",
    "--expected-head-sha": HEAD_SHA,
    "--guardian-policy": ".shiki/guardian-policy.json",
    "--guardian-comments": ".shiki/gha/live-guardian-comments.json",
    "--guardian-events": ".shiki/gha/live-guardian-events.json",
    "--guardian-timeline": ".shiki/gha/live-guardian-timeline.json",
    "--base-shiki": ".shiki/gha/base-shiki/.shiki",
    "--merged-prs": str(IMPL_PR),
    # ADR 0015 Contract Approval carry: the metadata job now passes
    # --contract-approval (built earlier in the step). The fixture does not write
    # this file, so mergegate_check degrades to "no proof" (missing file -> None)
    # and the medium-risk reconcile PR is unaffected — but the flag must resolve a
    # value here or argv reconstruction KeyErrors the moment the workflow adds it.
    "--contract-approval": ".shiki/gha/contract-approval.json",
    # ADR 0018 base-sync carry: the metadata job now also passes --default-branch.
    # A concrete branch name resolves the argv; the carry is inert for this
    # medium-risk reconcile PR (the carry refuses risk != high before any git), so
    # the ready result is unchanged. --base-sync-carry is store_true (below).
    "--default-branch": "main",
}
_STORE_TRUE = {"--allow-missing-cca", "--base-sync-carry"}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# The immutable governance body shared byte-for-byte by base and head task. Only
# status / expected_pr / ledger_evidence may move during a post_merge reconcile.
_TASK_BODY = {
    "id": TASK_ID,
    "goal_id": GOAL_ID,
    "title": "Make workflows invoke the scripts they claim to",
    "scope": "Wire base snapshot + merged-prs into the metadata check.",
    "non_goals": [],
    "dependencies": [],
    "locks": ["path:.github/workflows/shiki-mergegate.yml"],
    "assigned_runtime": "claude-code",
    "risk_level": "medium",
    "acceptance_checks": ["metadata check passes a post_merge_reconcile PR"],
    "expected_branch": "shiki/t-20260729t065622765968z-234bf98b-make-wor",
}

PR_BODY = f"""Reconcile the merged task residue.

<!-- shiki:post_merge_reconcile -->

Task: {TASK_ID}

## Scope
Clear the residual lock and expected_pr for the merged task.

## Acceptance
Task lock released and expected_pr cleared.

## Evidence
Reconcile ledger appended.

## MergeGate
post_merge_reconcile mode.
"""


def _build_post_merge_fixture(target: Path) -> None:
    """A valid post_merge_reconcile fixture that a correctly-wired metadata check
    must return ``ready`` for. Copies the repo's environmental artifacts (manifest,
    config, guardian policy, workflows — for job-name/required-check validation)
    and writes the reconcile diff."""
    shiki = target / ".shiki"
    shiki.mkdir(parents=True, exist_ok=True)
    for rel in (".shiki/manifest.json", ".shiki/config.yaml", ".shiki/guardian-policy.json"):
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, dest)
    shutil.copytree(WORKFLOWS, target / ".github" / "workflows")

    # HEAD task: reconciled residue — status back to review, expected_pr cleared.
    head_task = {**_TASK_BODY, "status": "review", "expected_pr": None,
                 "ledger_evidence": [LEDGER_ID]}
    _write(shiki / "tasks" / f"{TASK_ID}.json", head_task)
    _write(shiki / "goals" / f"{GOAL_ID}.json",
           {"id": GOAL_ID, "status": "in-progress", "title": "g", "risk_level": "medium"})
    # Reconcile ledger (task-scoped lock event).
    _write(shiki / "ledger" / f"{LEDGER_ID}.json",
           {"id": LEDGER_ID, "goal_id": GOAL_ID, "task_id": TASK_ID, "type": "lock",
            "actor": "shiki-loop", "timestamp": "2026-07-29T07:00:00+00:00",
            "summary": "lock released post-merge", "evidence": [f".shiki/tasks/{TASK_ID}.json"]})

    # Base snapshot (origin/base): the merged impl PR left the task at review naming
    # the impl PR, with its lock active — the residue being reconciled.
    base = shiki / "gha" / "base-shiki" / ".shiki"
    _write(base / "tasks" / f"{TASK_ID}.json",
           {**_TASK_BODY, "status": "review", "expected_pr": IMPL_PR, "ledger_evidence": ["L-base00001"]})

    gha = shiki / "gha"
    gha.mkdir(parents=True, exist_ok=True)
    _write(gha / "pr.json",
           {"number": RECONCILE_PR, "body": PR_BODY, "headRefOid": HEAD_SHA,
            "labels": [{"name": "mergegate:post_merge_reconcile"}], "reviews": []})
    # The diff: task file modified, lock deleted (released), reconcile ledger added.
    (gha / "changed-files-status.txt").write_text(
        f"M\t.shiki/tasks/{TASK_ID}.json\n"
        f"D\t.shiki/locks/{TASK_ID}.json\n"
        f"A\t.shiki/ledger/{LEDGER_ID}.json\n",
        encoding="utf-8",
    )
    for name in ("live-guardian-comments", "live-guardian-events", "live-guardian-timeline"):
        (gha / f"{name}.json").write_text("[]\n", encoding="utf-8")


def _metadata_argv(*, include_base_shiki: bool = True) -> list[str]:
    """Reconstruct the metadata job's argv from the workflow's own invocation, so a
    flag added or dropped in the workflow changes what this test runs."""
    invocation = _mergegate_invocation()
    flags = re.findall(r"--[a-z][a-z-]+", invocation)
    if "--base-shiki" not in flags or "--merged-prs" not in flags:
        raise AssertionError(f"metadata invocation missing merge-proof flags: {flags}")
    argv: list[str] = []
    for flag in flags:
        if flag == "--base-shiki" and not include_base_shiki:
            continue
        argv.append(flag)
        if flag not in _STORE_TRUE:
            argv.append(_FLAG_VALUES[flag])
    return argv


def _run_main(argv: list[str], cwd: Path) -> tuple[int, dict]:
    saved_cwd = os.getcwd()
    saved_argv = sys.argv
    os.chdir(cwd)
    sys.argv = ["mergegate_check.py", *argv]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mergegate_check.main()
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)
    result = json.loads((cwd / ".shiki" / "gha" / "mergegate-result.json").read_text())
    return rc, result


class MetadataArgvPostMergeReconcile(unittest.TestCase):
    def test_metadata_argv_returns_ready(self) -> None:
        # AC1: driving main() with the metadata job's exact argv against a valid
        # post_merge_reconcile fixture returns ready (it blocked before the wiring).
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _build_post_merge_fixture(target)
            rc, result = _run_main(_metadata_argv(), target)
        self.assertEqual(result["blocking_reasons"], [], result)
        self.assertEqual(result["mergegate"], "ready")
        self.assertEqual(rc, 0)

    def test_without_base_shiki_still_blocks(self) -> None:
        # AC2: the same argv with --base-shiki removed still blocks — fail-closed
        # behaviour is unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _build_post_merge_fixture(target)
            rc, result = _run_main(_metadata_argv(include_base_shiki=False), target)
        self.assertEqual(result["mergegate"], "blocked")
        self.assertEqual(rc, 1)
        self.assertTrue(
            any("base .shiki snapshot" in reason for reason in result["blocking_reasons"]),
            result["blocking_reasons"],
        )


# --- AC5: validate_shiki invocation contract ------------------------------


class ValidateWorkflowInvocationContract(unittest.TestCase):
    def _stage(self, tmp: Path) -> Path:
        """A throwaway repo root holding the hardened workflows plus every workflow
        named in the invocation contract so the contract check can run against a
        mutated copy. shiki-cca-completion.yml is included because ADR 0018 added a
        top-level entry for it to WORKFLOW_INVOCATION_REQUIRED_FLAGS; omitting it
        would FileNotFoundError inside validate_workflow_invocation_contracts."""
        dest = tmp / ".github" / "workflows"
        dest.mkdir(parents=True, exist_ok=True)
        for wf in (MERGEGATE_WF, ORCHESTRATOR_WF, CCA_WF):
            shutil.copyfile(wf, dest / wf.name)
        return tmp

    def test_repo_workflows_satisfy_contract(self) -> None:
        # The live repo workflows must pass the contract (proves it is wired in).
        validate_shiki.validate_workflow_invocation_contracts(REPO_ROOT)

    def test_missing_base_shiki_flag_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._stage(Path(tmp))
            wf = root / ".github" / "workflows" / MERGEGATE_WF.name
            wf.write_text(
                wf.read_text(encoding="utf-8").replace(
                    "--base-shiki .shiki/gha/base-shiki/.shiki \\\n", ""
                ),
                encoding="utf-8",
            )
            with self.assertRaises(validate_shiki.ValidationError):
                validate_shiki.validate_workflow_invocation_contracts(root)

    def test_missing_merged_prs_flag_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._stage(Path(tmp))
            wf = root / ".github" / "workflows" / MERGEGATE_WF.name
            text = wf.read_text(encoding="utf-8")
            self.assertIn('--merged-prs "$MERGED_PRS"', text)
            wf.write_text(text.replace('--merged-prs "$MERGED_PRS"', ""), encoding="utf-8")
            with self.assertRaises(validate_shiki.ValidationError):
                validate_shiki.validate_workflow_invocation_contracts(root)

    def test_expression_in_run_body_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._stage(Path(tmp))
            wf = root / ".github" / "workflows" / ORCHESTRATOR_WF.name
            text = wf.read_text(encoding="utf-8")
            self.assertIn('--plan-file "$PLAN_PATH"', text)
            wf.write_text(
                text.replace(
                    '--plan-file "$PLAN_PATH"',
                    '--plan-file "${{ steps.command.outputs.plan_path }}"',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(validate_shiki.ValidationError):
                validate_shiki.validate_workflow_invocation_contracts(root)


# --- AC3: commit-evidence stages before the change guard ------------------


class CommitEvidenceGuard(unittest.TestCase):
    STEP = "Execute Shiki plan and open evidence PR"

    def test_stages_before_guard(self) -> None:
        run = _step_run_body(ORCHESTRATOR_WF, self.STEP)
        self.assertIn("git add .shiki", run)
        self.assertIn("git diff --cached --quiet", run)
        self.assertLess(
            run.index("git add .shiki"),
            run.index("git diff --cached --quiet"),
            "staging must precede the change guard",
        )
        # The buggy bare guard (which only sees tracked files) must be gone.
        self.assertNotIn("if git diff --quiet", run)

    def test_new_files_only_are_committed_and_pushed(self) -> None:
        # AC3: with only NEW (untracked) evidence, the fixed stage-then-guard
        # sequence detects the change, commits and pushes — the bare guard did not.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin.git"
            work = root / "work"
            self._git("init", "--bare", str(origin))
            self._git("init", str(work))
            self._git("-C", str(work), "config", "user.email", "t@example.com")
            self._git("-C", str(work), "config", "user.name", "Shiki Test")
            self._git("-C", str(work), "remote", "add", "origin", str(origin))
            (work / "README.md").write_text("seed\n", encoding="utf-8")
            self._git("-C", str(work), "add", "README.md")
            self._git("-C", str(work), "commit", "-m", "seed")
            self._git("-C", str(work), "branch", "-M", "main")
            self._git("-C", str(work), "push", "-u", "origin", "main")

            branch = "shiki/orchestrator-evidence-1"
            self._git("-C", str(work), "switch", "-c", branch)
            # ONLY new untracked evidence — nothing tracked changes.
            evidence = work / ".shiki" / "gha" / "evidence.json"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("{}\n", encoding="utf-8")

            # The old guard: `git diff --quiet` reports "no changes" (exit 0).
            old = subprocess.run(["git", "-C", str(work), "diff", "--quiet"])
            self.assertEqual(old.returncode, 0, "bare git diff must miss untracked files")

            # The fixed guard: stage first, then diff the index.
            self._git("-C", str(work), "add", ".shiki")
            staged = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--quiet"])
            self.assertNotEqual(staged.returncode, 0, "staged diff must see the new file")

            self._git("-C", str(work), "commit", "-m", "shiki: record orchestrator evidence")
            self._git("-C", str(work), "push", "-u", "origin", branch)

            remote_log = subprocess.run(
                ["git", "-C", str(origin), "log", "--oneline", branch],
                capture_output=True, text=True, check=True,
            )
        self.assertIn("record orchestrator evidence", remote_log.stdout)

    @staticmethod
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], check=True, capture_output=True, text=True)


# --- AC4: comment payload reaches plan-file as a literal string -----------


class PlanFileInjection(unittest.TestCase):
    SHIKI = REPO_ROOT / "scripts" / "shiki.py"

    def test_plan_path_flows_through_env_not_expression(self) -> None:
        run = _step_run_body(ORCHESTRATOR_WF, "Run Shiki plan")
        self.assertIn('--plan-file "$PLAN_PATH"', run)
        self.assertNotIn("${{ steps.command.outputs.plan_path }}", run)

    def test_command_substitution_payload_is_literal(self) -> None:
        # AC4: a $(...) payload in the (comment-derived) plan path reaches shiki.py
        # as a literal string — it is NOT executed — and the script fails with its
        # own missing-file error, exactly as the env-var `"$PLAN_PATH"` handoff does.
        # This runs the workflow's actual `smoke live ... --plan-file "$PLAN_PATH"`
        # command; a stub `gh` on PATH satisfies smoke live's auth/repo checks so
        # the plan read (read_json -> "missing file") is what fires, hermetically.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            (target / ".shiki").mkdir(parents=True)
            subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "origin",
                 "https://github.com/example/shiki-injection-test.git"],
                check=True, capture_output=True,
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            gh_stub = fake_bin / "gh"
            gh_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            gh_stub.chmod(0o755)

            sentinel = root / "pwned"
            payload = f"$(touch {sentinel})"
            script = 'python3 "$SHIKI" smoke live --target "$TARGET" --plan-file "$PLAN_PATH" --dry-run'
            env = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "SHIKI": str(self.SHIKI),
                "TARGET": str(target),
                "PLAN_PATH": payload,
            }
            result = subprocess.run(
                ["bash", "-c", script], cwd=str(target), env=env, capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(sentinel.exists(), "command substitution payload was executed")
            combined = result.stdout + result.stderr
            self.assertIn("missing file", combined)
            self.assertIn(payload, combined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
