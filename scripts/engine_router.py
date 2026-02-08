#!/usr/bin/env python3
"""Shiki Engine Router — タスク特性に基づくエンジン自動選択

Usage:
    python3 scripts/engine_router.py <task_file>
    python3 scripts/engine_router.py --all           # 全 pending タスクを振り分け
    python3 scripts/engine_router.py --dry-run <task_file>  # 変更せず結果だけ表示

Exit: prints selected engine to stdout ("codex" or "claude-team")
"""

import json
import glob
import os
import sys
from typing import Any, Dict, Optional, Tuple

# ─────────────────────────────────────────────
# Affinity Rules: タスクの特徴 → エンジン適性
# スコアが高いほどそのエンジンに適している
# ─────────────────────────────────────────────

CLAUDE_SIGNALS = {
    # キーワード in title/description → Claude 適性スコア
    "keywords": {
        "refactor": 3,
        "redesign": 3,
        "architect": 3,
        "design": 2,
        "review": 3,
        "debug": 2,
        "investigate": 2,
        "analyze": 2,
        "plan": 3,
        "migrate": 2,
        "security": 2,
        "vulnerability": 2,
        "multi-file": 2,
        "cross-cutting": 2,
        "integration": 2,
        "coordinate": 2,
    },
    # 複数ファイル対象 → Claude
    "multi_file_threshold": 3,  # target_files が N 以上なら Claude
    # authority_layer による加算
    "authority_bonus": {
        "coordinator": 3,
        "monitor": 2,
    },
}

CODEX_SIGNALS = {
    "keywords": {
        "implement": 2,
        "create": 1,
        "add": 1,
        "test": 3,
        "fix": 1,
        "bugfix": 2,
        "ci": 2,
        "lint": 2,
        "format": 2,
        "boilerplate": 3,
        "scaffold": 2,
        "generate": 1,
        "docs": 2,
        "documentation": 2,
        "typing": 2,
        "types": 1,
    },
    # 単一ファイル対象 → Codex
    "single_file_bonus": 2,
    # authority_layer による加算
    "authority_bonus": {
        "executor": 1,
    },
}


def check_codex_available() -> bool:
    """Check if Codex CLI is installed and authenticated."""
    import shutil
    import subprocess

    if not shutil.which("codex"):
        return False
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass
    # Fallback: check OPENAI_API_KEY
    return bool(os.environ.get("OPENAI_API_KEY"))


def load_config() -> Dict[str, Any]:
    """Load .shiki/config.yaml (simple parser, no pyyaml dependency)."""
    config: Dict[str, Any] = {
        "routing_strategy": "affinity",
        "fallback": True,
        "codex_available": check_codex_available(),
        "phase_defaults": {
            "understand": "claude",
            "generate": "claude",
            "allocate": "claude",
            "execute": "auto",
            "verify": "claude",
            "integrate": "claude",
        },
    }

    config_paths = [".shiki/config.yaml", "../.shiki/config.yaml"]
    for cp in config_paths:
        if os.path.exists(cp):
            try:
                with open(cp, encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith("strategy:"):
                            config["routing_strategy"] = stripped.split(":")[1].strip()
                        elif stripped.startswith("fallback:"):
                            config["fallback"] = stripped.split(":")[1].strip().lower() == "true"
            except Exception:
                pass
            break

    return config


def score_task(task: Dict[str, Any]) -> Tuple[int, int]:
    """Score a task for Claude vs Codex affinity.

    Returns (claude_score, codex_score).
    """
    claude_score = 0
    codex_score = 0

    title = task.get("title", "").lower()
    desc = task.get("description", "").lower()
    text = f"{title} {desc}"

    # Keyword matching
    for keyword, score in CLAUDE_SIGNALS["keywords"].items():
        if keyword in text:
            claude_score += score

    for keyword, score in CODEX_SIGNALS["keywords"].items():
        if keyword in text:
            codex_score += score

    # File count heuristic
    target_files = task.get("context", {}).get("target_files", [])
    file_count = len(target_files)

    if file_count >= CLAUDE_SIGNALS["multi_file_threshold"]:
        claude_score += 3
    elif file_count <= 1:
        codex_score += CODEX_SIGNALS["single_file_bonus"]

    # Authority layer
    authority = task.get("authority_layer", "executor")
    claude_score += CLAUDE_SIGNALS["authority_bonus"].get(authority, 0)
    codex_score += CODEX_SIGNALS["authority_bonus"].get(authority, 0)

    # Contract reference → Codex (spec is clear)
    if task.get("context", {}).get("contract_ref"):
        codex_score += 2

    # Has acceptance criteria (testable) → Codex advantage
    acceptance = task.get("acceptance", [])
    if len(acceptance) >= 2:
        codex_score += 1

    # Theta phase override
    theta = task.get("theta_phase", "execute")
    if theta in ("understand", "generate", "allocate", "verify", "integrate"):
        claude_score += 5  # Non-execute phases strongly favor Claude

    return (claude_score, codex_score)


def route_task(task: Dict[str, Any], config: Optional[Dict] = None) -> Dict[str, str]:
    """Determine the optimal engine for a task.

    Returns dict with:
        primary: "codex" | "claude-team"
        fallback: "claude-team" | "codex"
        reason: human-readable explanation
    """
    if config is None:
        config = load_config()

    strategy = config.get("routing_strategy", "affinity")
    codex_ok = config.get("codex_available", True)

    # If Codex is not available, force Claude-only
    if not codex_ok and strategy not in ("claude-only",):
        return {"primary": "claude-team", "fallback": "claude-team", "reason": "codex_unavailable(not authenticated or not installed)"}

    # Fixed strategy overrides
    if strategy == "claude-only":
        return {"primary": "claude-team", "fallback": "claude-team", "reason": "strategy=claude-only"}
    if strategy == "codex-only":
        if not codex_ok:
            return {"primary": "claude-team", "fallback": "claude-team", "reason": "strategy=codex-only but codex_unavailable"}
        return {"primary": "codex", "fallback": "codex", "reason": "strategy=codex-only"}

    # Check phase defaults
    theta = task.get("theta_phase", "execute")
    phase_defaults = config.get("phase_defaults", {})
    phase_default = phase_defaults.get(theta, "auto")

    if phase_default != "auto":
        engine = "claude-team" if phase_default == "claude" else "codex"
        fallback = "codex" if engine == "claude-team" else "claude-team"
        return {"primary": engine, "fallback": fallback, "reason": f"phase_default({theta}={phase_default})"}

    # Explicit engine in task
    assigned = task.get("assigned_to", "")
    if assigned in ("claude-team", "claude-leader", "claude-member"):
        return {"primary": "claude-team", "fallback": "codex", "reason": "explicit_assignment"}
    if assigned == "codex":
        return {"primary": "codex", "fallback": "claude-team", "reason": "explicit_assignment"}
    if assigned == "human":
        return {"primary": "human", "fallback": "human", "reason": "human_task"}

    # Affinity scoring
    claude_score, codex_score = score_task(task)

    if claude_score > codex_score:
        return {
            "primary": "claude-team",
            "fallback": "codex",
            "reason": f"affinity(claude={claude_score},codex={codex_score})",
        }
    elif codex_score > claude_score:
        return {
            "primary": "codex",
            "fallback": "claude-team",
            "reason": f"affinity(claude={claude_score},codex={codex_score})",
        }
    else:
        # Tie → prefer Codex for execute phase (sandbox advantage)
        return {
            "primary": "codex",
            "fallback": "claude-team",
            "reason": f"affinity_tie(claude={claude_score},codex={codex_score})->codex(sandbox)",
        }


def update_task_engine(task_file: str, dry_run: bool = False) -> Dict[str, str]:
    """Route a single task and optionally update the file."""
    with open(task_file, encoding="utf-8") as f:
        task = json.load(f)

    result = route_task(task)
    task_id = task.get("id", os.path.basename(task_file))

    if not dry_run and task.get("status") == "pending":
        # Update task with routing decision
        task["assigned_to"] = result["primary"]
        if "engine" not in task:
            task["engine"] = {}
        if isinstance(task.get("engine"), str):
            task["engine"] = {"primary": task["engine"]}
        task["engine"] = {
            "primary": result["primary"],
            "fallback": result["fallback"],
            "routing_reason": result["reason"],
        }

        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2, ensure_ascii=False)

    return {"task_id": task_id, **result}


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if not args or args[0] == "--help":
        print(__doc__)
        sys.exit(0)

    if args[0] == "--all":
        task_files = sorted(glob.glob(".shiki/tasks/*.json"))
        if not task_files:
            print("No task files found in .shiki/tasks/")
            sys.exit(0)

        config = load_config()
        codex_ok = config.get("codex_available", False)
        print(f"Codex: {'available' if codex_ok else 'NOT available (all tasks -> Claude)'}")
        print(f"Routing {len(task_files)} tasks...\n")
        print(f"{'Task':<12} {'Primary':<14} {'Fallback':<14} Reason")
        print("-" * 70)

        for tf in task_files:
            try:
                result = update_task_engine(tf, dry_run=dry_run)
                print(f"{result['task_id']:<12} {result['primary']:<14} {result['fallback']:<14} {result['reason']}")
            except (json.JSONDecodeError, OSError) as e:
                print(f"{'ERROR':<12} {tf}: {e}")

        if dry_run:
            print("\n(dry-run: no files modified)")
    else:
        task_file = args[0]
        if not os.path.exists(task_file):
            print(f"File not found: {task_file}", file=sys.stderr)
            sys.exit(1)

        result = update_task_engine(task_file, dry_run=dry_run)
        # stdout: just the engine name (for shell scripts to capture)
        print(result["primary"])

        # stderr: details (for logging)
        print(f"  fallback={result['fallback']} reason={result['reason']}", file=sys.stderr)


if __name__ == "__main__":
    main()
