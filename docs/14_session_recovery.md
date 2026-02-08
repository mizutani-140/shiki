# セッション復旧ガイド

開発セッションが中断した場合（ターミナルクローズ、ネットワーク切断、マシン再起動等）の復旧手順を解説します。

---

## 目次

1. [復旧の概要](#1-復旧の概要)
2. [CLIモードの復旧](#2-cliモードの復旧)
3. [GitHubモードの復旧](#3-githubモードの復旧)
4. [混合モードの復旧](#4-混合モードの復旧)
5. [手動による状態再構築](#5-手動による状態再構築)
6. [保持される情報と失われる情報](#6-保持される情報と失われる情報)

---

## 1. 復旧の概要

### 復旧可能な状態の源泉

Shiki（式） は「会話よりファイルを優先する」原則に基づき、重要な状態をファイルに永続化しています。

| 状態情報 | 保存先 | 復旧可能 |
|---|---|---|
| タスク定義と状態 | `.shiki/tasks/*.json` | はい |
| 設計計画 | `.shiki/plans/` | はい |
| 契約定義 | `.shiki/contracts/*.json` | はい |
| DAG 定義 | `.shiki/dag/*.json` | はい |
| セッション状態 | `.shiki/state/session-*.json` | はい |
| 完了レポート | `.shiki/reports/*.md` | はい |
| Git の変更 | git history / worktree | はい |
| Agent Teams の会話 | メモリ内（揮発） | **いいえ** |
| チーム構成 | セッション状態ファイルに記録 | 部分的 |
| メンバーのコンテキスト | メモリ内（揮発） | **いいえ** |

### 復旧の基本戦略

```
1. 最新のセッション状態ファイルを読み込む
2. .shiki/ のファイル群から現在の状態を再構築する
3. チームを再作成する（TeamCreate）
4. 未完了タスクを再割当する
5. θフェーズを確認し、作業を継続する
```

---

## 2. CLIモードの復旧

### 2.1 自動復旧（推奨）

#### start_cli_session.sh を使用

```bash
# 起動時に自動で復旧ファイルを検出
./scripts/start_cli_session.sh

# 前回のセッションが見つかった場合、以下のプロンプトが表示される：
# ============================================
#   前回のセッションが見つかりました
# ============================================
#   セッションID: session-20250615T143022
#   タイムスタンプ: 2025-06-15T14:30:22Z
#   θフェーズ: execute
#
# 前回のセッションから再開しますか？ [Y/n]:
```

#### --resume フラグ

確認プロンプトをスキップして自動的にリジュームします：

```bash
./scripts/start_cli_session.sh --resume
```

### 2.2 recover_session.py を使用

復旧プロンプトを生成して Claude Code に渡します：

```bash
# 最新のセッションを自動検出して復旧プロンプトを生成
python3 scripts/recover_session.py

# 特定のセッションファイルを指定
python3 scripts/recover_session.py .shiki/state/session-20250615T143022.json

# Claude Code に直接パイプ
python3 scripts/recover_session.py | claude --prompt -
```

#### 復旧プロンプトに含まれる情報

1. **セッション概要**: ID、モード、中断時刻、θフェーズ
2. **Git 状態**: ブランチ、コミット、未コミットファイル
3. **チーム再構築指示**: メンバー一覧と役割
4. **タスク状況**: 進行中/ブロック中/未着手/レビュー待ち
5. **θフェーズ状態**: 現在フェーズと exit_criteria
6. **バジェット状態**: 使用量と残量
7. **再開手順**: ステップバイステップの指示

### 2.3 手動復旧

自動復旧が利用できない場合、以下の手順で手動復旧を行います。

#### Step 1: セッション状態の確認

```bash
# 最新のセッション状態を表示
cat .shiki/state/session-*.json | python3 -m json.tool

# セッション状態がない場合はタスクから状態を推測
ls -la .shiki/tasks/*.json
```

#### Step 2: Claude Code の起動

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
claude
```

#### Step 3: 復旧指示の入力

Claude Code に以下を入力します：

```
前回のセッションから再開してください。

1. .shiki/state/ の最新セッションファイルを読んでください
2. .shiki/tasks/ の全タスクを確認してください
3. 未完了タスクを特定し、チームを再構築してください
4. θフェーズの現在位置を確認し、exit_criteria に向けて作業を継続してください
```

### 2.4 tmux セッションの復旧

tmux セッションが生存している場合（ターミナルのみ閉じた場合）：

```bash
# tmux セッション一覧を確認
tmux list-sessions

# shiki セッションに再接続
tmux attach-session -t shiki
```

tmux セッション自体が失われた場合は、`start_cli_session.sh` で再作成します。

---

## 3. GitHubモードの復旧

### 3.1 DAG 状態からの復旧

GitHubモードでは、DAG ファイルがタスク実行の中心的な状態管理を担います。

#### Step 1: DAG 状態の確認

```bash
# DAG ファイルの確認
cat .shiki/dag/*.json | python3 -m json.tool
```

DAG ファイルの `nodes` 配列で各ノードの `status` を確認します：

- `pending` — 未実行
- `running` — 実行中（中断された可能性あり）
- `completed` — 完了
- `failed` — 失敗
- `skipped` — スキップ

#### Step 2: running ノードの確認

`running` 状態のノードは中断された可能性があります：

```bash
# running 状態のタスクを確認
python3 -c "
import json, glob
for f in glob.glob('.shiki/dag/*.json'):
    with open(f) as fp:
        dag = json.load(fp)
    for node in dag.get('nodes', []):
        if node.get('status') == 'running':
            print(f\"DAG: {dag['dag_id']}, Node: {node['node_id']}, Task: {node['task_id']}\")
"
```

#### Step 3: ワークフローの再実行

GitHub Actions のワークフローを再実行するか、ラベルを再設定してトリガーします。

### 3.2 Worktree の復旧

#### Worktree 状態の確認

```bash
# 既存の worktree を一覧
git worktree list

# 孤立した worktree の検出
git worktree prune --dry-run
```

#### 孤立 Worktree の処理

```bash
# 不要な worktree を削除（安全のため --dry-run で確認してから）
git worktree prune

# 必要な worktree を再作成
git worktree add ../worktrees/task-T-0001 feature/T-0001
```

### 3.3 Issue/PR 状態からの復旧

GitHub の Issue と PR の状態からプロジェクトの進捗を把握できます：

```bash
# Open Issue の確認（shiki ラベル付き）
gh issue list --label "shiki"

# Open PR の確認
gh pr list

# 特定の Issue の詳細
gh issue view <number>
```

---

## 4. 混合モードの復旧

CLIモードとGitHubモードを併用している場合、両方の状態を確認する必要があります。

### 復旧手順

#### Step 1: 状態源の統合確認

```bash
# ローカルの .shiki/ 状態
ls -la .shiki/tasks/*.json
ls -la .shiki/state/*.json

# GitHub の状態
gh issue list --label "shiki"
gh pr list

# Git の状態
git status
git log --oneline -10
git worktree list
```

#### Step 2: 状態の同期

`sync_agent_teams_state.py` を使って、Agent Teams の状態と `.shiki/` の状態を同期します：

```bash
# 差分の確認（ドライラン）
python3 scripts/sync_agent_teams_state.py --dry-run

# 同期の実行
python3 scripts/sync_agent_teams_state.py --direction both
```

#### Step 3: 優先する状態の決定

状態が競合する場合、以下の優先順位で解決します：

1. **Git のコミット履歴** — 最も信頼性が高い
2. **GitHub Issue/PR** — リモートに永続化されている
3. **`.shiki/` ファイル** — ローカルのファイルシステム
4. **セッション状態ファイル** — 最新のスナップショット
5. **Agent Teams のメモリ** — 揮発性（復旧不可）

---

## 5. 手動による状態再構築

セッション状態ファイルが存在せず、自動復旧ができない場合の手順です。

### 5.1 タスク状態の確認と修正

```bash
# 全タスクファイルの一覧
ls .shiki/tasks/

# 各タスクの status を確認
python3 -c "
import json, glob
for f in sorted(glob.glob('.shiki/tasks/*.json')):
    if f.endswith('.keep'):
        continue
    with open(f) as fp:
        t = json.load(fp)
    print(f\"{t.get('id','?'):>10} | {t.get('status','?'):>12} | {t.get('assigned_to','?'):>15} | {t.get('title','?')}\")
"
```

### 5.2 running/in_progress タスクの処理

中断時に `in_progress` だったタスクを確認し、実際の進捗に基づいて状態を更新します：

```bash
# in_progress タスクを特定
python3 -c "
import json, glob
for f in sorted(glob.glob('.shiki/tasks/*.json')):
    if f.endswith('.keep'):
        continue
    with open(f) as fp:
        t = json.load(fp)
    if t.get('status') == 'in_progress':
        print(f\"Task: {t['id']}\")
        print(f\"  Title: {t.get('title')}\")
        print(f\"  Assigned: {t.get('assigned_to')}\")
        print(f\"  Claimed: {t.get('claimed_by')}\")
        print(f\"  Acceptance: {t.get('acceptance', [])}\")
        print()
"
```

各タスクについて、以下のいずれかを判断します：

| 状況 | 対応 |
|---|---|
| 作業が完了していた | status を `review` または `completed` に更新 |
| 作業が途中だった | status を `in_progress` のまま、再割当する |
| 作業が始まっていなかった | status を `pending` に戻す |
| 成果物に問題がある | status を `failed` にし、新規タスクを作成 |

### 5.3 θフェーズの推定

セッション状態ファイルがない場合、タスクの状態からθフェーズを推定します：

```
全タスクが pending → θ₂ GENERATE or θ₃ ALLOCATE
一部が in_progress → θ₄ EXECUTE
全タスクが completed/review → θ₅ VERIFY
θ₅ の exit_criteria が満たされている → θ₆ INTEGRATE
```

### 5.4 セッション状態ファイルの再作成

状態を把握したら、セッション状態ファイルを手動で作成します：

```bash
cat > .shiki/state/session-$(date +%Y%m%dT%H%M%S)-recovered.json << 'EOF'
{
  "session_id": "recovered-YYYYMMDD",
  "mode": "cli",
  "timestamp": "2025-06-15T00:00:00Z",
  "team_name": "shiki-team",
  "members": [
    {
      "name": "leader",
      "role": "TeamLeader",
      "authority_layer": "coordinator",
      "status": "terminated"
    },
    {
      "name": "impl-1",
      "role": "Implementer",
      "authority_layer": "executor",
      "status": "terminated"
    }
  ],
  "active_tasks": [
    {
      "task_id": "T-0001",
      "status": "in_progress",
      "assigned_to": "claude-member",
      "claimed_by": "impl-1"
    }
  ],
  "theta_phase": "execute",
  "budget": {
    "tokens_used": 0,
    "tokens_remaining": 500000,
    "budget_limit": 500000
  },
  "notes": "手動復旧。元のセッション状態ファイルなし。"
}
EOF
```

---

## 6. 保持される情報と失われる情報

### 保持される情報（ファイル永続化済み）

| 情報 | 保存先 | 備考 |
|---|---|---|
| ゴールと受け入れ条件 | `GOAL.md` | 変更なし |
| 設計計画 | `.shiki/plans/` | 最新版が保存されている |
| タスク定義と状態 | `.shiki/tasks/` | 最後の更新時点の状態 |
| 契約定義 | `.shiki/contracts/` | 状態遷移含む |
| DAG 定義 | `.shiki/dag/` | バッチ進行状態含む |
| 完了レポート | `.shiki/reports/` | 完了タスクの証跡 |
| セッションスナップショット | `.shiki/state/` | 最後のスナップショット時点 |
| Git 履歴 | `.git/` | コミット済みの変更 |
| GitHub Issue/PR | GitHub | リモート永続化 |
| 設定 | `.shiki/config.yaml` | 変更なし |
| ロール定義 | `roles/roles.yaml` | 変更なし |

### 失われる情報（揮発性）

| 情報 | 理由 | 影響 |
|---|---|---|
| Agent Teams の会話履歴 | メモリ内のみ | チームメンバーの文脈が失われる |
| メンバーのコンテキスト | メモリ内のみ | 個々のメンバーが持っていた暗黙知が失われる |
| 未保存の編集 | ファイル保存前の変更 | エディタバッファの内容が失われる |
| tmux ペインの出力 | ターミナルバッファ | 直前の対話ログが失われる |
| Plan Mode の提出内容 | 会話内でのやり取り | 未承認の計画が失われる |
| P2P 通信の内容 | メモリ内のみ | メンバー間の合意事項が失われる |
| リアルタイムのバジェット計算 | メモリ内のみ | 概算値はセッション状態に残る |

### 影響を最小化するためのベストプラクティス

1. **重要な決定はファイルに記録する** — 会話で合意した内容は `.shiki/` に書き出す
2. **定期的にセッション状態を保存する** — coordinator が定期的に状態を `.shiki/state/` に書き出すよう指示する
3. **P2P で合意した内容は coordinator にも報告する** — ファイルで追跡可能にする
4. **タスク状態をこまめに更新する** — 着手時・完了時に即座に更新する
5. **Git のコミットをこまめに行う** — 実装の進捗を Git に永続化する

---

## トラブルシューティング

### Q: セッション状態ファイルが見つからない

**A**: タスクファイルから状態を再構築してください（「5.1 タスク状態の確認と修正」参照）。

### Q: タスクの status が実態と合っていない

**A**: 手動でタスクファイルの JSON を編集し、正しい status に更新してください。

### Q: Git に未コミットの変更がある

**A**: `git status` と `git diff` で変更内容を確認し、意図した変更であればコミットしてください。意図しない変更であれば `git stash` で退避してください。

### Q: Worktree が壊れている

**A**: `git worktree prune` で孤立した worktree を削除し、必要なら `git worktree add` で再作成してください。

### Q: 復旧後にチームメンバーが前の状態を覚えていない

**A**: これは正常な動作です。Agent Teams の会話は揮発性のため、復旧時にはタスクファイルとレポートから文脈を再構築する必要があります。復旧プロンプト（`recover_session.py`）がこの文脈再構築を支援します。

### Q: バジェットの正確な残量がわからない

**A**: セッション状態ファイルの `budget` フィールドが最後に記録された概算値です。正確な値が不明な場合は、保守的に見積もり、余裕を持って作業してください。
