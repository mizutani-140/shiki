# ローカルセットアップ：Claude Code CLIモード（Agent Teams / Delegate Mode / Hooks）

CLIモードでは Claude Code Agent Teams をローカル環境で直接操作し、tmux または iTerm2 でチームメンバーを並列に可視化しながら対話的に開発を進めます。

---

## 目次

1. [前提条件](#1-前提条件)
2. [Agent Teams の有効化](#2-agent-teams-の有効化)
3. [tmux セッションセットアップ](#3-tmux-セッションセットアップ)
4. [iTerm2 代替セットアップ](#4-iterm2-代替セットアップ)
5. [Delegate Mode の設定](#5-delegate-mode-の設定)
6. [Hook の設定](#6-hook-の設定)
7. [初回セッションのウォークスルー](#7-初回セッションのウォークスルー)
8. [前回セッションからの復旧](#8-前回セッションからの復旧)

---

## 1. 前提条件

### 必須

| ソフトウェア | バージョン | 確認コマンド |
|---|---|---|
| Claude Code CLI | 最新版 | `claude --version` |
| Python | 3.10 以上 | `python3 --version` |
| Git | 2.20 以上 | `git --version` |

### 推奨（表示モードに応じて）

| ソフトウェア | 用途 | インストール |
|---|---|---|
| tmux | ペイン分割表示（推奨） | `brew install tmux` (macOS) / `apt install tmux` (Linux) |
| jq | JSON 処理 | `brew install jq` (macOS) / `apt install jq` (Linux) |
| iTerm2 | macOS 用代替ターミナル | [iterm2.com](https://iterm2.com/) |

### プロジェクト初期化

まだ `.shiki/` ディレクトリが作成されていない場合は、初期化スクリプトを実行します：

```bash
./scripts/init_shiki.sh
```

---

## 2. Agent Teams の有効化

### 環境変数の設定（必須）

Agent Teams 機能を有効にするため、以下の環境変数を設定します：

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

### 永続化

シェルの設定ファイルに追加して永続化します：

```bash
# ~/.bashrc または ~/.zshrc に追加
echo 'export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1' >> ~/.zshrc
source ~/.zshrc
```

### settings.json での設定

`~/.claude/settings.json` でも設定できます。テンプレートは `templates/home-claude-settings.json` を参考にしてください。

---

## 3. tmux セッションセットアップ

### 自動セットアップ（推奨）

`start_cli_session.sh` を使って tmux セッションを自動構築します：

```bash
# .shiki/config.yaml で display: tmux を確認後
./scripts/start_cli_session.sh
```

このスクリプトは以下を自動で行います：
1. tmux がインストールされているか確認
2. 既存セッションの有無を確認
3. 復旧ファイルがあればリジュームを提案
4. tmux セッション「shiki」を作成
5. Leader + Member のペイン分割を構成
6. 環境変数を設定
7. Claude Code を起動

### レイアウト

```
┌─────────────────────┬─────────────────────┐
│                     │    Member 1          │
│    Leader           │    (Executor)        │
│    (Coordinator)    ├─────────────────────┤
│                     │    Member 2          │
│                     │    (Executor/Monitor) │
└─────────────────────┴─────────────────────┘

ウィンドウ 2: [monitor] — セッション状態のリアルタイム表示
```

### 手動セットアップ

tmux を手動で構成する場合：

```bash
# セッション作成
tmux new-session -d -s shiki -n leader

# ペイン分割
tmux split-window -h -t shiki:leader
tmux split-window -v -t shiki:leader.1

# 各ペインで環境変数を設定
tmux send-keys -t shiki:leader.0 "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" C-m
tmux send-keys -t shiki:leader.1 "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" C-m
tmux send-keys -t shiki:leader.2 "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" C-m

# Leader ペインで Claude Code を起動
tmux send-keys -t shiki:leader.0 "cd /path/to/project && claude" C-m

# セッションに接続
tmux attach-session -t shiki
```

### ペインの操作

| 操作 | キーバインド |
|---|---|
| ペイン移動 | `Ctrl+B` → 矢印キー |
| ペインズーム | `Ctrl+B` → `z` |
| ウィンドウ切替 | `Ctrl+B` → `n` / `p` |
| セッション切断 | `Ctrl+B` → `d` |
| セッション再接続 | `tmux attach -t shiki` |

---

## 4. iTerm2 代替セットアップ

macOS で iTerm2 を使用する場合の設定方法です。

### 手動ペイン分割

1. iTerm2 を開く
2. `Cmd+D` で垂直分割
3. 右ペインで `Cmd+Shift+D` で水平分割
4. 各ペインで環境変数設定と Claude Code 起動を行う

### AppleScript による自動化

`start_cli_session.sh` を `display: iterm2` で実行すると、AppleScript のテンプレートが表示されます：

```bash
# config.yaml を編集
# cli:
#   display: iterm2

./scripts/start_cli_session.sh
# → AppleScript が表示される
```

表示された AppleScript を iTerm2 の Script Editor にペーストして実行します。

### in-process モード（フォールバック）

tmux も iTerm2 も使用しない場合は、in-process モードで単一ターミナルから操作できます：

```yaml
# .shiki/config.yaml
cli:
  display: in-process
```

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
claude
```

---

## 5. Delegate Mode の設定

### 概要

Delegate Mode は Leader（coordinator）が実装に直接手を出さず、計画・調整・承認に専念するためのモードです。

### 有効化

`.shiki/config.yaml` で設定します（デフォルトで有効）：

```yaml
cli:
  delegate_mode: true
```

### Delegate Mode の効果

Leader は以下のツールのみ使用可能になります：
- `TeamCreate` / `SendMessage` / `TaskCreate` / `TaskList` / `TaskUpdate`
- `EnterPlanMode` / `ExitPlanMode`

`Edit` / `Write` ツールは使用不可になります。

### エージェント定義

各エージェントタイプの詳細な動作定義は以下のファイルに記載されています：

```
.claude/agents/
├── coordinator.md   # Leader (Delegate Mode)
├── executor.md      # Member (Plan → Standard)
└── monitor.md       # Member (Plan Mode 固定)
```

---

## 6. Hook の設定

### 設定ファイル

`.claude/settings.json` に Hook の設定が含まれています。

### 利用可能な Hook

| Hook | ファイル | 説明 |
|---|---|---|
| SessionStart | `session_start_inject_context.py` | `.shiki/` の状態を注入 |
| PreToolUse | `pre_bash_guard.py` | 危険コマンドをブロック |
| PostToolUse | `post_edit_quality.py` | 編集後の品質チェック |
| PostToolUse | `post_task_sync.py` | タスク状態の同期確認 |
| Stop | `on_stop_report.py` | セッション終了時のレポート |

### 推奨の有効化順序

1. まず `PreToolUse`（`pre_bash_guard.py`）を有効化 — 安全確保
2. 次に `PostToolUse`（`post_edit_quality.py`）を有効化 — 品質確保
3. 慣れたら `SessionStart` と `Stop` を有効化 — 状態管理の自動化
4. 最後に `post_task_sync.py` を有効化 — タスク同期の自動化

---

## 7. 初回セッションのウォークスルー

### Step 1: セッションの起動

```bash
./scripts/start_cli_session.sh
```

### Step 2: Leader ペインで初期指示

Leader ペインの Claude Code に以下を入力します（`docs/08_prompts_library.md` の G0 プロンプト参照）：

```
あなたはこのリポジトリのAI開発統括（Team Leader）です。
まずはPlanモードで、コード変更を始めずに設計を行ってください。

入力（ゴール）:
- GOAL.md を読み、ユーザーの最終ゴールと受け入れ条件を抽出する
- 不明点やリスクを列挙する

出力:
1. 設計方針
2. タスク分割
3. 必要な役割（Team編成案）
4. .shiki/plans/PLAN.md を作る提案
```

### Step 3: チーム編成

Plan が完成したら、チームを編成します：

```
Planに基づき Agent Teams を編成してください。
役割は roles/roles.yaml を参照してください。
メンバーはまず Plan Mode で起動してください。
```

### Step 4: メンバーの Plan 承認

各メンバーが Plan を提出したら、Leader がレビューして承認します：

```
[Member ペインから Plan が SendMessage で届く]

→ Leader: 内容を確認し、ExitPlanMode で承認
→ Member: Standard Mode に移行し、実装開始
```

### Step 5: 実装と完了

- メンバーが実装を進め、受け入れ条件をクリア
- `TaskUpdate` で status を `review` に変更
- Leader がレポートを確認し、`completed` に更新
- 全タスク完了後、θフェーズを進行

---

## 8. 前回セッションからの復旧

### 自動復旧

```bash
# 起動時に復旧ファイルを自動検出
./scripts/start_cli_session.sh

# 自動リジューム
./scripts/start_cli_session.sh --resume
```

### 復旧プロンプトの生成

```bash
# 最新セッションから復旧プロンプトを生成
python3 scripts/recover_session.py

# Claude Code にパイプ
python3 scripts/recover_session.py | claude --prompt -
```

### 詳細手順

復旧の詳細な手順は `docs/14_session_recovery.md` を参照してください。

### tmux セッションの再接続

tmux セッションがまだ生存している場合は、単純に再接続できます：

```bash
tmux attach-session -t shiki
```
