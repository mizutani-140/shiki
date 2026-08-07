#!/usr/bin/env bash
set -euo pipefail

# loop-lock advisory guard — derive-at-judgment-time mirror-lock model
# (SADR-0016 / A-LOCKS).
#
# Asserts, through the real scripts/ modules, that
# validate_shiki.loop_lock_warnings NO LONGER warns for a narrow-lock
# loop-executed task on an active goal. A registered task's stored locks are
# exactly what the plan declared; MergeGate recomputes the task's id-scoped
# mirror set at judgment time (mergegate_check._derive_task_mirror_locks) and
# unions it into the effective locks, so a narrow-lock loop task is the intended,
# fully-covered state. The advisory must stay silent and must not claim a
# dispatch-time .shiki/** guarantee (which never existed).

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from validate_shiki import loop_lock_warnings

GOAL = "G-20260617T031753970001Z-1de3b322"
TASK = "T-20260617T090739482036Z-c92b21b3"


def fail(message: str) -> None:
    raise SystemExit(f"loop-lock-guard: {message}")


def task(runtime: str, locks: list[str]) -> dict:
    return {"id": TASK, "goal_id": GOAL, "assigned_runtime": runtime, "locks": locks}


# A narrow-lock loop task on an active goal is fully covered by the derived
# mirror set at judgment time, so the advisory must not warn.
if loop_lock_warnings(
    {GOAL: {"id": GOAL, "status": "ready"}},
    {GOAL: [task("claude-code", ["path:scripts/x.py"])]},
):
    fail("narrow-lock loop task on an active goal must not warn under the derived-lock model")

# Silent across loop runtimes and active goal statuses.
for runtime in ("claude-code", "codex"):
    for status in ("planned", "ready", "blocked"):
        if loop_lock_warnings(
            {GOAL: {"id": GOAL, "status": status}},
            {GOAL: [task(runtime, ["path:scripts/x.py"])]},
        ):
            fail(f"loop task ({runtime}, {status}) must not warn")

# A task may still declare path:.shiki/** as a circularity break; still silent.
if loop_lock_warnings(
    {GOAL: {"id": GOAL, "status": "ready"}},
    {GOAL: [task("claude-code", ["path:.shiki/**"])]},
):
    fail("declaring path:.shiki/** must not warn")

# Missing goal payload must not crash and must not warn.
if loop_lock_warnings({}, {GOAL: [task("claude-code", ["path:scripts/x.py"])]}):
    fail("missing goal payload must not warn")

print("loop-lock-guard: ok")
PY
