#!/usr/bin/env bash
set -euo pipefail

# T4 — loop task-lock guard (PRD 0002, gap #5 / Q5).
#
# Asserts, through the real scripts/ modules:
#   1. validate_shiki.loop_lock_warnings WARNS (never errors) for a loop task of
#      an active goal that lacks .shiki/** coverage, and is scoped out for
#      non-loop runtimes and terminal/archived goals.
#   2. the dispatch-time guarantee (shiki_tasks.try_acquire_locks /
#      allocate_worktree_record) records .shiki/** coverage for a loop task
#      WITHOUT mutating the registered task file's declared locks.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_tasks import (
    LOOP_SHIKI_STATE_LOCK,
    allocate_worktree_record,
    is_loop_executed_runtime,
    locks_cover_shiki_state,
    try_acquire_locks,
)
from validate_shiki import loop_lock_warnings

GOAL = "G-20260617T031753970001Z-1de3b322"
TASK = "T-20260617T090739482036Z-c92b21b3"


def fail(message: str) -> None:
    raise SystemExit(f"loop-lock-guard: {message}")


# --- (1) WARN-ONLY validator hint -------------------------------------------

def task(runtime: str, locks: list[str]) -> dict:
    return {"id": TASK, "goal_id": GOAL, "assigned_runtime": runtime, "locks": locks}


warns = loop_lock_warnings(
    {GOAL: {"id": GOAL, "status": "ready"}},
    {GOAL: [task("claude-code", ["path:scripts/x.py"])]},
)
if len(warns) != 1 or ".shiki/**" not in warns[0] or TASK not in warns[0]:
    fail(f"expected one .shiki/** warning for an active loop task, got {warns}")

if loop_lock_warnings(
    {GOAL: {"id": GOAL, "status": "ready"}},
    {GOAL: [task("codex", [LOOP_SHIKI_STATE_LOCK])]},
):
    fail("covered loop task must not warn")

if loop_lock_warnings(
    {GOAL: {"id": GOAL, "status": "ready"}},
    {GOAL: [task("human", ["path:docs/**"])]},
):
    fail("non-loop runtime must not warn")

for status in ("complete", "archived", "historical"):
    if loop_lock_warnings(
        {GOAL: {"id": GOAL, "status": status}},
        {GOAL: [task("claude-code", ["path:scripts/x.py"])]},
    ):
        fail(f"terminal/archived goal ({status}) must be scoped out of warnings")

if not is_loop_executed_runtime("claude-code") or not is_loop_executed_runtime("codex"):
    fail("claude-code/codex must classify as loop-executed")
if is_loop_executed_runtime("other") or is_loop_executed_runtime("hermes-runner"):
    fail("placeholder runtimes must not classify as loop-executed")

# --- (2) dispatch-time guarantee --------------------------------------------

with tempfile.TemporaryDirectory(prefix="shiki-loop-lock-") as tmp:
    target = Path(tmp)
    for sub in ("tasks", "locks", "ledger", "worktrees"):
        (target / ".shiki" / sub).mkdir(parents=True, exist_ok=True)
    declared = ["path:scripts/shiki_tasks.py"]
    task_file = target / ".shiki" / "tasks" / f"{TASK}.json"
    task_file.write_text(
        json.dumps(
            {
                "id": TASK,
                "goal_id": GOAL,
                "title": "loop task",
                "scope": "x",
                "non_goals": [],
                "dependencies": [],
                "locks": list(declared),
                "assigned_runtime": "claude-code",
                "risk_level": "medium",
                "acceptance_checks": ["a"],
                "expected_branch": "shiki/t4-probe",
                "ledger_evidence": ["L-seed"],
                "status": "planned",
            }
        ),
        encoding="utf-8",
    )

    ok, blockers, _ = try_acquire_locks(target, TASK)
    if not ok:
        fail(f"lock acquisition failed: {blockers}")
    lock_record = json.loads((target / ".shiki" / "locks" / f"{TASK}.json").read_text())
    if not locks_cover_shiki_state(lock_record["locks"]):
        fail(f"lock record must cover .shiki state: {lock_record['locks']}")

    allocate_worktree_record(target, TASK)
    wt_record = json.loads((target / ".shiki" / "worktrees" / f"{TASK}.json").read_text())
    if not locks_cover_shiki_state(wt_record["locks"]):
        fail(f"worktree record must cover .shiki state: {wt_record['locks']}")

    reread = json.loads(task_file.read_text())
    if reread["locks"] != declared:
        fail(f"task file locks must NOT be mutated; got {reread['locks']}")

print("loop-lock-guard: ok")
PY
