#!/usr/bin/env python3
"""Worktree lifecycle manager for Shiki（式） GitHub mode.

Git worktree を使用してタスクごとにブランチを分離し、
並列実行を可能にするための管理スクリプト。

Usage:
  python3 scripts/worktree_manager.py create --branch <branch> --task-id <task_id>
  python3 scripts/worktree_manager.py cleanup --branch <branch>
  python3 scripts/worktree_manager.py merge --branch <branch> [--target <target>]
  python3 scripts/worktree_manager.py conflicts --branches <b1> <b2> [<b3> ...]
  python3 scripts/worktree_manager.py list

Requirements:
  - git (with worktree support, git >= 2.5)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def run_git(args: List[str], cwd: Optional[str] = None, check: bool = True) -> Tuple[int, str, str]:
    """Execute a git command and return (returncode, stdout, stderr)."""
    cmd = ["git"] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_repo_root() -> str:
    """Get the repository root directory."""
    _, root, _ = run_git(["rev-parse", "--show-toplevel"])
    return root


def get_worktree_base_dir() -> str:
    """Get the worktree base directory from config or default."""
    repo_root = get_repo_root()
    config_path = os.path.join(repo_root, ".shiki", "config.yaml")

    base_dir = os.path.join(repo_root, "..", "worktrees")

    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("base_dir:"):
                    value = stripped.split(":", 1)[1].strip()
                    if value:
                        if os.path.isabs(value):
                            base_dir = value
                        else:
                            base_dir = os.path.join(repo_root, value)
                    break

    return os.path.abspath(base_dir)


def create_worktree(branch: str, task_id: str) -> None:
    """Create a new git worktree for a task.

    Args:
        branch: Branch name (e.g., 'shiki/task-T-0001')
        task_id: Task ID for metadata tracking
    """
    repo_root = get_repo_root()
    base_dir = get_worktree_base_dir()
    worktree_path = os.path.join(base_dir, branch.replace("/", "_"))

    # Ensure base directory exists
    os.makedirs(base_dir, exist_ok=True)

    # Check if worktree already exists
    _, list_out, _ = run_git(["worktree", "list", "--porcelain"], check=False)
    if worktree_path in list_out:
        print(f"[WARN] Worktree already exists at: {worktree_path}")
        return

    # Check if branch exists
    rc, _, _ = run_git(["rev-parse", "--verify", branch], check=False)
    if rc == 0:
        # Branch exists, create worktree from it
        run_git(["worktree", "add", worktree_path, branch])
        print(f"[OK] Created worktree from existing branch '{branch}' at: {worktree_path}")
    else:
        # Create new branch and worktree
        run_git(["worktree", "add", "-b", branch, worktree_path])
        print(f"[OK] Created worktree with new branch '{branch}' at: {worktree_path}")

    # Write metadata
    meta_dir = os.path.join(worktree_path, ".shiki", "state")
    os.makedirs(meta_dir, exist_ok=True)
    meta_file = os.path.join(meta_dir, f"worktree-{task_id}.json")

    metadata = {
        "task_id": task_id,
        "branch": branch,
        "worktree_path": worktree_path,
        "created_from": "worktree_manager",
    }

    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    print(f"[OK] Worktree metadata written to: {meta_file}")


def cleanup_worktree(branch: str) -> None:
    """Safely remove a worktree and optionally its branch.

    Args:
        branch: Branch name of the worktree to remove
    """
    repo_root = get_repo_root()
    base_dir = get_worktree_base_dir()
    worktree_path = os.path.join(base_dir, branch.replace("/", "_"))

    # Check if worktree exists
    _, list_out, _ = run_git(["worktree", "list", "--porcelain"], check=False)
    if worktree_path not in list_out:
        print(f"[WARN] Worktree not found at: {worktree_path}")
        # Still try to clean up the directory if it exists
        if os.path.exists(worktree_path):
            print(f"[INFO] Directory exists, removing: {worktree_path}")
            import shutil
            shutil.rmtree(worktree_path, ignore_errors=True)
        return

    # Check for uncommitted changes
    rc, status, _ = run_git(["status", "--porcelain"], cwd=worktree_path, check=False)
    if status:
        print(f"[WARN] Worktree has uncommitted changes:")
        print(f"  {status[:500]}")
        print(f"[WARN] Proceeding with forced removal")

    # Remove worktree
    try:
        run_git(["worktree", "remove", worktree_path, "--force"])
        print(f"[OK] Removed worktree: {worktree_path}")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to remove worktree: {e.stderr}")
        # Force cleanup
        run_git(["worktree", "remove", worktree_path, "--force"], check=False)
        print(f"[OK] Force-removed worktree: {worktree_path}")

    # Prune stale worktree references
    run_git(["worktree", "prune"], check=False)
    print(f"[OK] Pruned stale worktree references")


def detect_conflicts(branches: List[str]) -> None:
    """Check for file conflicts between worktree branches.

    Compares the set of modified files in each branch against the common base
    to identify potential merge conflicts.

    Args:
        branches: List of branch names to check
    """
    if len(branches) < 2:
        print("[ERROR] Need at least 2 branches to check conflicts")
        sys.exit(1)

    # Get the merge base for all branches
    _, base, _ = run_git(["merge-base", "--octopus"] + branches, check=False)
    if not base:
        # Fall back to HEAD
        _, base, _ = run_git(["rev-parse", "HEAD"])

    # Get modified files for each branch
    branch_files: dict[str, set[str]] = {}

    for branch in branches:
        rc, _, _ = run_git(["rev-parse", "--verify", branch], check=False)
        if rc != 0:
            print(f"[WARN] Branch '{branch}' not found, skipping")
            continue

        _, diff_out, _ = run_git(["diff", "--name-only", base, branch], check=False)
        files = set(f for f in diff_out.split("\n") if f)
        branch_files[branch] = files
        print(f"  {branch}: {len(files)} modified files")

    # Find overlapping files
    conflicts_found = False
    checked_pairs = set()

    for b1 in branch_files:
        for b2 in branch_files:
            if b1 == b2:
                continue
            pair = tuple(sorted([b1, b2]))
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)

            overlap = branch_files[b1] & branch_files[b2]
            if overlap:
                conflicts_found = True
                print(f"\n[CONFLICT] Potential conflicts between '{b1}' and '{b2}':")
                for f in sorted(overlap):
                    print(f"  - {f}")

    if not conflicts_found:
        print("\n[OK] No file conflicts detected between branches")
    else:
        print(f"\n[WARN] Found potential conflicts in {len(checked_pairs)} branch pairs")
        sys.exit(1)


def merge_worktree(branch: str, target: str = "main") -> None:
    """Merge a worktree branch back into the target branch.

    Args:
        branch: Source branch to merge from
        target: Target branch to merge into (default: 'main')
    """
    repo_root = get_repo_root()

    # Verify source branch exists
    rc, _, _ = run_git(["rev-parse", "--verify", branch], check=False)
    if rc != 0:
        print(f"[ERROR] Source branch '{branch}' not found")
        sys.exit(1)

    # Verify target branch exists
    rc, _, _ = run_git(["rev-parse", "--verify", target], check=False)
    if rc != 0:
        print(f"[ERROR] Target branch '{target}' not found")
        sys.exit(1)

    # Get current branch
    _, current, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"])

    # Check for conflicts first
    print(f"[INFO] Checking merge compatibility: {branch} -> {target}")
    _, merge_base, _ = run_git(["merge-base", target, branch])
    _, diff_stat, _ = run_git(["diff", "--stat", merge_base, branch], check=False)
    if diff_stat:
        print(f"[INFO] Changes to merge:\n{diff_stat}")

    # Attempt dry-run merge
    if current != target:
        run_git(["checkout", target])

    rc, _, stderr = run_git(["merge", "--no-commit", "--no-ff", branch], check=False)
    if rc != 0:
        print(f"[ERROR] Merge conflict detected:")
        print(f"  {stderr[:500]}")
        run_git(["merge", "--abort"], check=False)
        if current != target:
            run_git(["checkout", current], check=False)
        sys.exit(1)

    # Complete the merge
    _, _, _ = run_git(["commit", "-m", f"shiki: merge worktree branch '{branch}' into {target}"],
                      check=False)
    print(f"[OK] Merged '{branch}' into '{target}'")

    # Optionally clean up the worktree
    base_dir = get_worktree_base_dir()
    worktree_path = os.path.join(base_dir, branch.replace("/", "_"))
    if os.path.exists(worktree_path):
        # Check config for cleanup_on_merge
        config_path = os.path.join(repo_root, ".shiki", "config.yaml")
        cleanup = False
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                for line in f:
                    if "cleanup_on_merge:" in line and "true" in line.lower():
                        cleanup = True
                        break

        if cleanup:
            print(f"[INFO] Cleaning up worktree (cleanup_on_merge=true)")
            cleanup_worktree(branch)

    # Return to original branch
    if current != target:
        run_git(["checkout", current], check=False)


def list_worktrees() -> None:
    """List all active worktrees with their task associations."""
    _, list_out, _ = run_git(["worktree", "list", "--porcelain"])

    if not list_out.strip():
        print("[INFO] No worktrees found")
        return

    worktrees = []
    current_wt: dict = {}

    for line in list_out.split("\n"):
        line = line.strip()
        if line.startswith("worktree "):
            if current_wt:
                worktrees.append(current_wt)
            current_wt = {"path": line[9:]}
        elif line.startswith("HEAD "):
            current_wt["head"] = line[5:8] + "..."
        elif line.startswith("branch "):
            current_wt["branch"] = line[7:]
        elif line == "bare":
            current_wt["bare"] = True
        elif line == "detached":
            current_wt["detached"] = True

    if current_wt:
        worktrees.append(current_wt)

    # Find task associations
    print(f"{'Branch':<40} {'Path':<50} {'Task':<12} {'Status'}")
    print("-" * 110)

    for wt in worktrees:
        branch = wt.get("branch", "refs/heads/???").replace("refs/heads/", "")
        path = wt.get("path", "???")
        task_id = ""
        status = ""

        # Try to find associated task
        if "shiki/task-" in branch:
            task_id = branch.replace("shiki/task-", "")

        # Check for worktree metadata
        meta_pattern = os.path.join(path, ".shiki", "state", "worktree-*.json")
        import glob
        meta_files = glob.glob(meta_pattern)
        if meta_files:
            try:
                with open(meta_files[0], encoding="utf-8") as f:
                    meta = json.load(f)
                task_id = meta.get("task_id", task_id)
            except (json.JSONDecodeError, OSError):
                pass

        # Try to read task status
        if task_id:
            repo_root = get_repo_root()
            task_file = os.path.join(repo_root, ".shiki", "tasks", f"{task_id}.json")
            if os.path.exists(task_file):
                try:
                    with open(task_file, encoding="utf-8") as f:
                        task = json.load(f)
                    status = task.get("status", "unknown")
                except (json.JSONDecodeError, OSError):
                    status = "error"

        bare = " (bare)" if wt.get("bare") else ""
        detached = " (detached)" if wt.get("detached") else ""
        print(f"{branch:<40} {path:<50} {task_id:<12} {status}{bare}{detached}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Worktree lifecycle manager for Shiki（式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s create --branch shiki/task-T-0001 --task-id T-0001
  %(prog)s cleanup --branch shiki/task-T-0001
  %(prog)s merge --branch shiki/task-T-0001 --target main
  %(prog)s conflicts --branches shiki/task-T-0001 shiki/task-T-0002
  %(prog)s list
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # create
    create_parser = subparsers.add_parser("create", help="Create a new worktree for a task")
    create_parser.add_argument("--branch", required=True, help="Branch name for the worktree")
    create_parser.add_argument("--task-id", required=True, help="Task ID to associate")

    # cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Remove a worktree safely")
    cleanup_parser.add_argument("--branch", required=True, help="Branch name of the worktree")

    # merge
    merge_parser = subparsers.add_parser("merge", help="Merge a worktree branch back")
    merge_parser.add_argument("--branch", required=True, help="Source branch to merge")
    merge_parser.add_argument("--target", default="main", help="Target branch (default: main)")

    # conflicts
    conflicts_parser = subparsers.add_parser("conflicts", help="Check for file conflicts between branches")
    conflicts_parser.add_argument("--branches", nargs="+", required=True, help="Branches to check")

    # list
    subparsers.add_parser("list", help="List active worktrees with task associations")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "create":
            create_worktree(args.branch, args.task_id)
        elif args.command == "cleanup":
            cleanup_worktree(args.branch)
        elif args.command == "merge":
            merge_worktree(args.branch, args.target)
        elif args.command == "conflicts":
            detect_conflicts(args.branches)
        elif args.command == "list":
            list_worktrees()
        else:
            parser.print_help()
            return 1
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Git command failed: {e.cmd}", file=sys.stderr)
        if e.stderr:
            print(f"  {e.stderr}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
