# Shiki（式） セットアップガイド

プロジェクトへの導入手順。所要時間: 約10分。

---

## 前提条件

| 必須 | 確認コマンド |
|------|-------------|
| Claude Code CLI | `claude --version` |
| Python 3.10+ | `python3 --version` |
| Git 2.20+ | `git --version` |

| 推奨 | 用途 |
|------|------|
| tmux | 並列ペイン表示（CLIモード） |
| jq | JSON 処理 |
| Node.js 18+ | Codex 連携・MCP サーバー |

---

## 1. フレームワークの配置

### 新規プロジェクトの場合

```bash
# Shiki（式） をクローン/コピー
cp -r /path/to/ai-autodev-framework/.shiki /your/project/
cp -r /path/to/ai-autodev-framework/.claude /your/project/
cp -r /path/to/ai-autodev-framework/.github /your/project/
cp -r /path/to/ai-autodev-framework/.ai /your/project/
cp /path/to/ai-autodev-framework/CLAUDE.md /your/project/
cp /path/to/ai-autodev-framework/AGENTS.md /your/project/
cp -r /path/to/ai-autodev-framework/roles /your/project/
cp -r /path/to/ai-autodev-framework/scripts /your/project/
cp -r /path/to/ai-autodev-framework/templates /your/project/
```

### 既存プロジェクトの場合

```bash
cd /your/project
/path/to/ai-autodev-framework/scripts/init_shiki.sh
```

---

## 2. 初期設定

### 2-1. config.yaml を確認

`.shiki/config.yaml` を開き、自分の環境に合わせて調整する。

```yaml
# モード: auto / cli / github
mode: auto

cli:
  display: tmux          # tmux / iterm2 / in-process
  delegate_mode: true    # coordinator は Edit/Write 禁止
  self_claim: true       # executor がタスクを自己申告
  plan_mode_required: true  # executor は Plan Mode から開始

github:
  budget:
    max_tokens_per_task: 100000
    max_tokens_per_session: 500000
```

### 2-2. Agent Teams を有効化

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

永続化する場合:

```bash
echo 'export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1' >> ~/.zshrc
source ~/.zshrc
```

### 2-3. GOAL.md を作成

```bash
cp templates/GOAL.template.md GOAL.md
```

GOAL.md を編集し、以下を明確に書く:

```markdown
## 1. 何を達成したいか
（1〜3行で目的を記述）

## 2. 受け入れ条件
- [ ] テスト可能な条件1
- [ ] テスト可能な条件2

## 3. スコープ
### やる
- （実装する範囲）
### やらない
- （明確に除外する範囲）

## 4. 制約
- 依存追加: （許可/禁止するパッケージ）
- バジェット: （トークン上限）
```

### 2-4. バリデーション

```bash
python3 scripts/validate_shiki.py
```

`Validation OK` が出れば準備完了。

---

## 3. CLIモード セットアップ

### tmux で起動（推奨）

```bash
./scripts/start_cli_session.sh
```

自動で以下が構築される:

```
┌─────────────────────┬─────────────────────┐
│                     │   Member 1 (Executor)│
│   Leader            ├─────────────────────┤
│   (Coordinator)     │   Member 2 (Monitor) │
└─────────────────────┴─────────────────────┘
```

### tmux なしで起動

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

## 4. GitHubモード セットアップ

### 4-1. GitHub Secrets を設定

Repository → Settings → Secrets and variables → Actions:

| Secret 名 | 内容 |
|-----------|------|
| `ANTHROPIC_API_KEY` | Claude API キー |
| `OPENAI_API_KEY` | Codex API キー（Codex 使用時） |

### 4-2. ラベルを作成

```bash
# GitHub CLI (gh) がインストール済みの場合
./scripts/setup_github_labels.sh
```

### 4-3. ワークフローを有効化

`.github/workflows/` 内のファイルをそのままコミット&プッシュ。

### 4-4. Issue から起動

1. Issue を作成（テンプレート: `shiki_goal.md`）
2. `ai-goal` ラベルを付与
3. orchestrator ワークフローが自動起動

---

## 5. Codex 連携（任意）

```bash
npm i -g @openai/codex
export OPENAI_API_KEY="sk-..."
claude mcp add --transport stdio --scope project codex -- codex mcp-server
```

---

## 6. Guardian の設定（任意）

`.shiki/config.yaml` の guardian セクション:

```yaml
guardian:
  enabled: true
  github_username: "your-github-username"
  escalation_label: "guardian-review"
```

詳細は `.github/GUARDIAN.md` を参照。

---

## 7. Hook の確認

`.claude/settings.json` に以下の Hook が登録済み:

| イベント | Hook | 機能 |
|---------|------|------|
| SessionStart | `session_start_inject_context.py` | 状態の自動注入 |
| SessionStart | `ldd_logger.py session_start` | LDD ログ記録 |
| PreToolUse (Bash) | `pre_bash_guard.py` | 危険コマンドブロック |
| PostToolUse (Edit) | `post_edit_quality.py` | 品質チェック |
| PostToolUse (Task) | `post_task_sync.py` | タスク状態同期 |
| Stop | `on_stop_report.py` | セッション状態保存 |
| Stop | `ldd_logger.py session_stop` | LDD ログ記録 |

---

## セットアップ完了チェックリスト

- [ ] `.shiki/config.yaml` を環境に合わせて編集した
- [ ] `GOAL.md` を作成した
- [ ] `python3 scripts/validate_shiki.py` が `Validation OK` を返す
- [ ] `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` が設定されている（CLIモード）
- [ ] GitHub Secrets が設定されている（GitHubモード）

---

## 次のステップ

- [USAGE.md](./USAGE.md) — 使い方ガイド
- [docs/16_best_practices.md](./docs/16_best_practices.md) — ベストプラクティス
- [docs/04_local_setup_claude_code.md](./docs/04_local_setup_claude_code.md) — CLIモード詳細設定
- [docs/06_github_actions_setup.md](./docs/06_github_actions_setup.md) — GitHubモード詳細設定
