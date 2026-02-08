#!/usr/bin/env python3
"""
sync_agent_teams_state.py — Agent Teams ⇔ .shiki/ 双方向同期スクリプト

Claude Code Agent Teams のネイティブ状態（~/.claude/teams/）と
プロジェクトの .shiki/tasks/ の間でタスク状態を同期する。

使用方法:
    python3 scripts/sync_agent_teams_state.py [options]

オプション:
    --team-name NAME   チーム名を指定（デフォルト: 自動検出）
    --dry-run          実際の変更を行わず、差分のみ表示
    --direction DEST   同期方向: both(双方向), to-shiki, to-teams
    --verbose          詳細ログを表示

例:
    python3 scripts/sync_agent_teams_state.py --dry-run
    python3 scripts/sync_agent_teams_state.py --team-name my-project --direction to-shiki
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# --- 定数 ---
CLAUDE_HOME = Path.home() / ".claude"
TEAMS_DIR = CLAUDE_HOME / "teams"


class Logger:
    """ロギングユーティリティ"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def info(self, msg: str):
        print(f"[INFO] {msg}", file=sys.stderr)

    def warn(self, msg: str):
        print(f"[WARN] {msg}", file=sys.stderr)

    def error(self, msg: str):
        print(f"[ERROR] {msg}", file=sys.stderr)

    def debug(self, msg: str):
        if self.verbose:
            print(f"[DEBUG] {msg}", file=sys.stderr)

    def discrepancy(self, msg: str):
        print(f"[DISCREPANCY] {msg}", file=sys.stderr)


def find_project_root() -> Path:
    """プロジェクトルートを検出する"""
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    if (project_root / ".shiki" / "config.yaml").exists():
        return project_root

    cwd = Path.cwd()
    current = cwd
    while current != current.parent:
        if (current / ".shiki" / "config.yaml").exists():
            return current
        current = current.parent

    return project_root


def detect_team_name(project_root: Path) -> Optional[str]:
    """チーム名を自動検出する"""
    # 1. セッション状態から検出
    state_dir = project_root / ".shiki" / "state"
    if state_dir.exists():
        session_files = sorted(
            state_dir.glob("session-*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for sf in session_files:
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    session = json.load(f)
                    team_name = session.get("team_name")
                    if team_name:
                        return team_name
            except (json.JSONDecodeError, OSError):
                continue

    # 2. config.yaml の name から推測
    config_file = project_root / ".shiki" / "config.yaml"
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                    return name

    # 3. ディレクトリ名から推測
    return project_root.name


def load_teams_config(team_name: str) -> Optional[dict]:
    """~/.claude/teams/{team-name}/config.json を読み込む"""
    config_path = TEAMS_DIR / team_name / "config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return None


def load_teams_tasks(team_name: str) -> dict:
    """Agent Teams のタスク状態を読み込む"""
    tasks = {}

    # teams ディレクトリ配下のタスク関連ファイルを探索
    team_dir = TEAMS_DIR / team_name
    if not team_dir.exists():
        return tasks

    # tasks サブディレクトリ
    tasks_dir = team_dir / "tasks"
    if tasks_dir.exists():
        for task_file in tasks_dir.glob("*.json"):
            try:
                with open(task_file, "r", encoding="utf-8") as f:
                    task = json.load(f)
                    task_id = task.get("id", task_file.stem)
                    tasks[task_id] = task
            except (json.JSONDecodeError, OSError):
                continue

    # config.json 内の tasks フィールド
    config = load_teams_config(team_name)
    if config and "tasks" in config:
        for task in config["tasks"]:
            task_id = task.get("id")
            if task_id and task_id not in tasks:
                tasks[task_id] = task

    return tasks


def load_shiki_tasks(project_root: Path) -> dict:
    """shiki/tasks/ のタスクを読み込む"""
    tasks = {}
    tasks_dir = project_root / ".shiki" / "tasks"

    if not tasks_dir.exists():
        return tasks

    for task_file in tasks_dir.glob("*.json"):
        if task_file.name == ".keep":
            continue
        try:
            with open(task_file, "r", encoding="utf-8") as f:
                task = json.load(f)
                task_id = task.get("id", task_file.stem)
                tasks[task_id] = {
                    "data": task,
                    "path": task_file,
                }
        except (json.JSONDecodeError, OSError) as e:
            continue

    return tasks


def compare_task_states(
    teams_tasks: dict,
    shiki_tasks: dict,
    log: Logger,
) -> dict:
    """タスク状態を比較し、差分を検出する"""
    report = {
        "teams_only": [],       # Teams にのみ存在
        "shiki_only": [],     # shiki にのみ存在
        "status_mismatch": [],  # 両方に存在するがステータスが異なる
        "field_mismatch": [],   # 両方に存在するがその他フィールドが異なる
        "in_sync": [],          # 同期済み
    }

    all_task_ids = set(teams_tasks.keys()) | set(
        t_id for t_id in shiki_tasks.keys()
    )

    for task_id in sorted(all_task_ids):
        in_teams = task_id in teams_tasks
        in_shiki = task_id in shiki_tasks

        if in_teams and not in_shiki:
            report["teams_only"].append({
                "id": task_id,
                "data": teams_tasks[task_id],
            })
            log.discrepancy(f"タスク {task_id} は Agent Teams にのみ存在")

        elif not in_teams and in_shiki:
            report["shiki_only"].append({
                "id": task_id,
                "data": shiki_tasks[task_id]["data"],
                "path": str(shiki_tasks[task_id]["path"]),
            })
            log.discrepancy(f"タスク {task_id} は .shiki/tasks にのみ存在")

        else:
            # 両方に存在 — 状態を比較
            teams_data = teams_tasks[task_id]
            shiki_data = shiki_tasks[task_id]["data"]

            teams_status = teams_data.get("status", "unknown")
            shiki_status = shiki_data.get("status", "unknown")

            if teams_status != shiki_status:
                report["status_mismatch"].append({
                    "id": task_id,
                    "teams_status": teams_status,
                    "shiki_status": shiki_status,
                    "teams_data": teams_data,
                    "shiki_data": shiki_data,
                })
                log.discrepancy(
                    f"タスク {task_id}: ステータス不一致 "
                    f"(Teams={teams_status}, shiki={shiki_status})"
                )
            else:
                # その他のフィールドチェック
                compare_fields = ["assigned_to", "claimed_by", "priority"]
                mismatched_fields = []
                for field in compare_fields:
                    t_val = teams_data.get(field)
                    a_val = shiki_data.get(field)
                    if t_val != a_val and (t_val is not None or a_val is not None):
                        mismatched_fields.append({
                            "field": field,
                            "teams_value": t_val,
                            "shiki_value": a_val,
                        })

                if mismatched_fields:
                    report["field_mismatch"].append({
                        "id": task_id,
                        "mismatches": mismatched_fields,
                    })
                    for mf in mismatched_fields:
                        log.discrepancy(
                            f"タスク {task_id}: フィールド {mf['field']} 不一致 "
                            f"(Teams={mf['teams_value']}, shiki={mf['shiki_value']})"
                        )
                else:
                    report["in_sync"].append(task_id)
                    log.debug(f"タスク {task_id}: 同期済み")

    return report


def sync_to_shiki(
    teams_tasks: dict,
    shiki_tasks: dict,
    project_root: Path,
    dry_run: bool,
    log: Logger,
) -> int:
    """Agent Teams → .shiki/tasks/ に同期する"""
    changes = 0
    tasks_dir = project_root / ".shiki" / "tasks"

    for task_id, teams_data in teams_tasks.items():
        if task_id in shiki_tasks:
            # 既存タスクの更新
            shiki_data = shiki_tasks[task_id]["data"]
            shiki_path = shiki_tasks[task_id]["path"]

            updated = False
            for field in ["status", "assigned_to", "claimed_by", "priority"]:
                teams_val = teams_data.get(field)
                if teams_val is not None and teams_val != shiki_data.get(field):
                    log.info(
                        f"更新: {task_id}.{field}: "
                        f"{shiki_data.get(field)} → {teams_val}"
                    )
                    shiki_data[field] = teams_val
                    updated = True

            if updated:
                shiki_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                if not dry_run:
                    with open(shiki_path, "w", encoding="utf-8") as f:
                        json.dump(shiki_data, f, indent=2, ensure_ascii=False)
                        f.write("\n")
                changes += 1
        else:
            # 新規タスクの作成
            log.info(f"新規作成: {task_id} を .shiki/tasks/ に追加")
            new_task = dict(teams_data)
            new_task.setdefault("id", task_id)
            new_task.setdefault("status", "pending")
            new_task.setdefault("mode_origin", "cli")
            new_task["updated_at"] = datetime.now(timezone.utc).isoformat()

            if not dry_run:
                task_path = tasks_dir / f"{task_id}.json"
                with open(task_path, "w", encoding="utf-8") as f:
                    json.dump(new_task, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            changes += 1

    return changes


def sync_to_teams(
    teams_tasks: dict,
    shiki_tasks: dict,
    team_name: str,
    dry_run: bool,
    log: Logger,
) -> int:
    """shiki/tasks/ → Agent Teams に同期する"""
    changes = 0
    team_dir = TEAMS_DIR / team_name
    tasks_dir = team_dir / "tasks"

    for task_id, shiki_info in shiki_tasks.items():
        shiki_data = shiki_info["data"]

        if task_id in teams_tasks:
            # 既存タスクの更新
            teams_data = teams_tasks[task_id]
            updated = False

            for field in ["status", "assigned_to", "claimed_by", "priority"]:
                shiki_val = shiki_data.get(field)
                if shiki_val is not None and shiki_val != teams_data.get(field):
                    log.info(
                        f"更新 (Teams): {task_id}.{field}: "
                        f"{teams_data.get(field)} → {shiki_val}"
                    )
                    teams_data[field] = shiki_val
                    updated = True

            if updated:
                if not dry_run and tasks_dir.exists():
                    task_path = tasks_dir / f"{task_id}.json"
                    with open(task_path, "w", encoding="utf-8") as f:
                        json.dump(teams_data, f, indent=2, ensure_ascii=False)
                        f.write("\n")
                changes += 1
        else:
            # 新規タスクを Teams に追加
            log.info(f"新規作成 (Teams): {task_id} を Agent Teams に追加")
            if not dry_run:
                if not tasks_dir.exists():
                    tasks_dir.mkdir(parents=True, exist_ok=True)
                task_path = tasks_dir / f"{task_id}.json"
                with open(task_path, "w", encoding="utf-8") as f:
                    json.dump(shiki_data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            changes += 1

    return changes


def print_report(report: dict, log: Logger):
    """同期レポートを出力する"""
    print("\n" + "=" * 60)
    print("  Shiki ⇔ Agent Teams 同期レポート")
    print("=" * 60)

    print(f"\n同期済み:        {len(report['in_sync'])} タスク")
    print(f"Teams のみ:      {len(report['teams_only'])} タスク")
    print(f"shiki のみ:    {len(report['shiki_only'])} タスク")
    print(f"ステータス不一致: {len(report['status_mismatch'])} タスク")
    print(f"フィールド不一致: {len(report['field_mismatch'])} タスク")

    if report["status_mismatch"]:
        print("\n--- ステータス不一致の詳細 ---")
        for item in report["status_mismatch"]:
            print(
                f"  {item['id']}: Teams={item['teams_status']} ⇔ "
                f"shiki={item['shiki_status']}"
            )

    if report["field_mismatch"]:
        print("\n--- フィールド不一致の詳細 ---")
        for item in report["field_mismatch"]:
            for mf in item["mismatches"]:
                print(
                    f"  {item['id']}.{mf['field']}: "
                    f"Teams={mf['teams_value']} ⇔ shiki={mf['shiki_value']}"
                )

    total_issues = (
        len(report["teams_only"])
        + len(report["shiki_only"])
        + len(report["status_mismatch"])
        + len(report["field_mismatch"])
    )

    if total_issues == 0:
        print("\n全タスクが同期済みです。")
    else:
        print(f"\n合計 {total_issues} 件の不一致があります。")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Agent Teams ⇔ .shiki/ 双方向同期"
    )
    parser.add_argument(
        "--team-name",
        default=None,
        help="チーム名（デフォルト: 自動検出）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際の変更を行わず、差分のみ表示",
    )
    parser.add_argument(
        "--direction",
        choices=["both", "to-shiki", "to-teams"],
        default="both",
        help="同期方向（デフォルト: both）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="詳細ログを表示",
    )

    args = parser.parse_args()
    log = Logger(verbose=args.verbose)

    # プロジェクトルート検出
    project_root = find_project_root()
    log.info(f"プロジェクトルート: {project_root}")

    # チーム名検出
    team_name = args.team_name or detect_team_name(project_root)
    if not team_name:
        log.error("チーム名を特定できません。--team-name で指定してください。")
        sys.exit(1)
    log.info(f"チーム名: {team_name}")

    # 状態読み込み
    teams_tasks = load_teams_tasks(team_name)
    shiki_tasks = load_shiki_tasks(project_root)

    log.info(f"Agent Teams タスク数: {len(teams_tasks)}")
    log.info(f".shiki/tasks タスク数: {len(shiki_tasks)}")

    # 比較
    report = compare_task_states(teams_tasks, shiki_tasks, log)
    print_report(report, log)

    # 同期実行
    if args.dry_run:
        log.info("ドライランモード: 変更は適用されません")
    else:
        total_changes = 0

        if args.direction in ("both", "to-shiki"):
            changes = sync_to_shiki(
                teams_tasks, shiki_tasks, project_root, args.dry_run, log
            )
            total_changes += changes
            log.info(f"→ shiki: {changes} 件の変更を適用")

        if args.direction in ("both", "to-teams"):
            changes = sync_to_teams(
                teams_tasks, shiki_tasks, team_name, args.dry_run, log
            )
            total_changes += changes
            log.info(f"→ Teams: {changes} 件の変更を適用")

        log.info(f"合計 {total_changes} 件の変更を適用しました")


if __name__ == "__main__":
    main()
