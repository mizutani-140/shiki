#!/usr/bin/env python3
"""
recover_session.py — Shiki（式） セッション復旧スクリプト

前回中断したセッションの状態を読み取り、Claude Code 用の復旧プロンプトを生成する。

使用方法:
    python3 scripts/recover_session.py [session_file]
    python3 scripts/recover_session.py                    # 最新のセッションを自動検出
    python3 scripts/recover_session.py path/to/session.json  # 指定ファイルから復旧

出力:
    stdout に復旧プロンプトを出力（claude にパイプ可能）

例:
    python3 scripts/recover_session.py | claude --prompt -
"""

import json
import os
import sys
import glob
from datetime import datetime
from pathlib import Path
from typing import Optional


def find_project_root() -> Path:
    """プロジェクトルートを検出する"""
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    config_file = project_root / ".shiki" / "config.yaml"
    if config_file.exists():
        return project_root

    # カレントディレクトリからも探す
    cwd = Path.cwd()
    if (cwd / ".shiki" / "config.yaml").exists():
        return cwd

    # 親ディレクトリを遡る
    current = cwd
    while current != current.parent:
        if (current / ".shiki" / "config.yaml").exists():
            return current
        current = current.parent

    return project_root


def find_latest_session(project_root: Path) -> Optional[Path]:
    """最新のセッション状態ファイルを検出する"""
    state_dir = project_root / ".shiki" / "state"
    if not state_dir.exists():
        return None

    session_files = sorted(
        state_dir.glob("session-*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if session_files:
        return session_files[0]
    return None


def load_session(session_path: Path) -> dict:
    """セッションファイルを読み込む"""
    with open(session_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_tasks(project_root: Path) -> list:
    """タスクファイルを読み込む"""
    tasks_dir = project_root / ".shiki" / "tasks"
    tasks = []
    if tasks_dir.exists():
        for task_file in tasks_dir.glob("*.json"):
            if task_file.name == ".keep":
                continue
            try:
                with open(task_file, "r", encoding="utf-8") as f:
                    task = json.load(f)
                    tasks.append(task)
            except (json.JSONDecodeError, OSError) as e:
                print(f"# WARNING: タスクファイル読み込みエラー: {task_file}: {e}", file=sys.stderr)
    return tasks


def load_config(project_root: Path) -> dict:
    """config.yaml の基本情報を読み取る（簡易パーサ）"""
    config_file = project_root / ".shiki" / "config.yaml"
    config = {}
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, value = line.partition(":")
                    value = value.strip().split("#")[0].strip()
                    if value:
                        config[key.strip()] = value
    return config


def categorize_tasks(tasks: list) -> dict:
    """タスクをステータス別に分類する"""
    categories = {
        "incomplete": [],        # pending, in_progress, blocked
        "in_progress": [],       # in_progress
        "blocked": [],           # blocked
        "pending": [],           # pending
        "review": [],            # review
        "completed": [],         # completed
        "failed": [],            # failed
    }

    for task in tasks:
        status = task.get("status", "pending")
        task_summary = {
            "id": task.get("id", "UNKNOWN"),
            "title": task.get("title", "No title"),
            "status": status,
            "assigned_to": task.get("assigned_to", "unassigned"),
            "claimed_by": task.get("claimed_by"),
            "priority": task.get("priority", "medium"),
            "depends_on": task.get("depends_on", []),
            "theta_phase": task.get("theta_phase"),
            "authority_layer": task.get("authority_layer"),
        }

        if status in ("pending", "in_progress", "blocked"):
            categories["incomplete"].append(task_summary)
        if status == "in_progress":
            categories["in_progress"].append(task_summary)
        if status == "blocked":
            categories["blocked"].append(task_summary)
        if status == "pending":
            categories["pending"].append(task_summary)
        if status == "review":
            categories["review"].append(task_summary)
        if status == "completed":
            categories["completed"].append(task_summary)
        if status == "failed":
            categories["failed"].append(task_summary)

    return categories


def format_task_list(tasks: list, indent: str = "  ") -> str:
    """タスクリストをフォーマットする"""
    if not tasks:
        return f"{indent}（なし）\n"

    lines = []
    for t in tasks:
        claimed = f" [担当: {t['claimed_by']}]" if t.get("claimed_by") else ""
        deps = f" (依存: {', '.join(t['depends_on'])})" if t.get("depends_on") else ""
        phase = f" [θ: {t['theta_phase']}]" if t.get("theta_phase") else ""
        lines.append(
            f"{indent}- {t['id']}: {t['title']} "
            f"(status={t['status']}, priority={t['priority']}, assigned={t['assigned_to']}{claimed}){deps}{phase}"
        )
    return "\n".join(lines) + "\n"


def generate_recovery_prompt(
    session: dict,
    tasks: list,
    project_root: Path,
    config: dict,
) -> str:
    """復旧プロンプトを生成する"""
    categories = categorize_tasks(tasks)

    # セッション情報の抽出
    session_id = session.get("session_id", "unknown")
    mode = session.get("mode", "cli")
    theta_phase = session.get("theta_phase", "unknown")
    team_name = session.get("team_name", "shiki-team")
    members = session.get("members", [])
    budget = session.get("budget", {})
    git_state = session.get("git_state", {})
    notes = session.get("notes", "")
    timestamp = session.get("timestamp", "unknown")

    # バジェット情報
    tokens_used = budget.get("tokens_used", 0)
    tokens_remaining = budget.get("tokens_remaining", 0)
    budget_limit = budget.get("budget_limit", 0)
    budget_pct = (tokens_used / budget_limit * 100) if budget_limit > 0 else 0

    # プロンプト構築
    prompt_parts = []

    prompt_parts.append(
        "# セッション復旧指示\n\n"
        "前回中断したセッションから作業を再開してください。\n"
        "以下の状態情報に基づいて、チームを再構築し、未完了タスクを完了させてください。\n"
    )

    # セッション概要
    prompt_parts.append(
        f"\n## 1. セッション概要\n"
        f"- セッションID: {session_id}\n"
        f"- モード: {mode}\n"
        f"- 中断時刻: {timestamp}\n"
        f"- θフェーズ: {theta_phase}\n"
        f"- チーム名: {team_name}\n"
    )

    # Git状態
    if git_state:
        prompt_parts.append(
            f"\n## 2. Git 状態\n"
            f"- ブランチ: {git_state.get('branch', 'unknown')}\n"
            f"- コミット: {git_state.get('commit_sha', 'unknown')}\n"
        )
        dirty = git_state.get("dirty_files", [])
        if dirty:
            prompt_parts.append(f"- 未コミットファイル:\n")
            for f in dirty:
                prompt_parts.append(f"  - {f}\n")

    # チーム再構築指示
    prompt_parts.append(
        f"\n## 3. チーム再構築\n"
        f"以下のメンバーでチームを再構築してください（TeamCreate で '{team_name}' を作成）:\n\n"
    )

    if members:
        for m in members:
            status_note = ""
            if m.get("status") == "terminated":
                status_note = " ※前回終了済み — 再作成が必要"
            elif m.get("status") == "active":
                status_note = " ※前回アクティブ — 再作成が必要"
            prompt_parts.append(
                f"- **{m.get('name', 'unknown')}** (role={m.get('role', 'unknown')}, "
                f"authority={m.get('authority_layer', 'unknown')}){status_note}\n"
            )
    else:
        prompt_parts.append(
            "- メンバー情報なし。`roles/roles.yaml` を参照してチームを編成してください。\n"
        )

    # タスク状況
    prompt_parts.append(f"\n## 4. タスク状況\n")

    prompt_parts.append(f"\n### 進行中タスク（最優先で再開）\n")
    prompt_parts.append(format_task_list(categories["in_progress"]))

    prompt_parts.append(f"\n### ブロック中タスク（ブロッカー解決が必要）\n")
    prompt_parts.append(format_task_list(categories["blocked"]))

    prompt_parts.append(f"\n### 未着手タスク（着手待ち）\n")
    prompt_parts.append(format_task_list(categories["pending"]))

    prompt_parts.append(f"\n### レビュー待ちタスク\n")
    prompt_parts.append(format_task_list(categories["review"]))

    prompt_parts.append(
        f"\n### 完了/失敗タスク（参考）\n"
        f"  完了: {len(categories['completed'])}件, 失敗: {len(categories['failed'])}件\n"
    )

    # θフェーズ状態
    prompt_parts.append(
        f"\n## 5. θフェーズ状態\n"
        f"- 現在フェーズ: **{theta_phase}**\n"
        f"- `.shiki/config.yaml` の `convergence` セクションで exit_criteria を確認してください\n"
        f"- 未完了タスクがすべて完了した後、exit_criteria を評価してフェーズ進行を判断してください\n"
    )

    # バジェット状態
    prompt_parts.append(
        f"\n## 6. バジェット状態\n"
        f"- 使用済み: {tokens_used:,} トークン\n"
        f"- 残り: {tokens_remaining:,} トークン\n"
        f"- 上限: {budget_limit:,} トークン\n"
        f"- 使用率: {budget_pct:.1f}%\n"
    )
    if budget_pct >= 80:
        prompt_parts.append(
            f"\n**警告: バジェット使用率が {budget_pct:.1f}% に達しています。**\n"
            f"残りタスクの優先度を再評価し、最小限の作業で完了を目指してください。\n"
        )

    # 申し送り事項
    if notes:
        prompt_parts.append(
            f"\n## 7. 申し送り事項\n"
            f"{notes}\n"
        )

    # 再開手順
    prompt_parts.append(
        f"\n## 8. 再開手順\n"
        f"1. この情報を確認し、`.shiki/` の最新状態をファイルから直接読み取ってください\n"
        f"2. チームを再構築してください（TeamCreate）\n"
        f"3. 進行中タスクのメンバーを再割当してください\n"
        f"4. ブロック中タスクのブロッカーを解決してください\n"
        f"5. θフェーズ {theta_phase} の exit_criteria 達成に向けて作業を継続してください\n"
        f"6. バジェット残量に注意してください\n"
    )

    return "".join(prompt_parts)


def main():
    project_root = find_project_root()

    # セッションファイルの決定
    session_path: Optional[Path] = None

    if len(sys.argv) > 1:
        # 引数で指定されたファイル
        arg_path = Path(sys.argv[1])
        if arg_path.exists():
            session_path = arg_path
        else:
            print(f"エラー: セッションファイルが見つかりません: {arg_path}", file=sys.stderr)
            sys.exit(1)
    else:
        # 自動検出
        session_path = find_latest_session(project_root)

    if session_path is None:
        print("エラー: 復旧可能なセッションが見つかりません", file=sys.stderr)
        print(f"検索ディレクトリ: {project_root / '.shiki' / 'state'}", file=sys.stderr)
        sys.exit(1)

    # セッション読み込み
    print(f"# セッションファイル: {session_path}", file=sys.stderr)
    session = load_session(session_path)

    # タスク読み込み
    tasks = load_tasks(project_root)
    print(f"# タスク数: {len(tasks)}", file=sys.stderr)

    # 設定読み込み
    config = load_config(project_root)

    # 復旧プロンプト生成
    prompt = generate_recovery_prompt(session, tasks, project_root, config)

    # stdout に出力
    print(prompt)


if __name__ == "__main__":
    main()
