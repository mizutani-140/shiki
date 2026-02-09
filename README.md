# Shiki（式） — デュアルモード AI自律駆動開発フレームワーク

> **1コマンドで新規プロジェクトを作成できます：**
>
> ```bash
> # shiki CLI でプロジェクト作成（テンプレートコピー + 初期化 + クリーンアップを自動実行）
> ./bin/shiki new my-project
>
> # ゴールを定義して開発開始
> cd my-project
> vim GOAL.md
> ./bin/shiki start
> ```
>
> 詳細は [SETUP.md](./.shiki/reference/SETUP.md) を参照。
> （`shiki new` 実行後、フレームワークドキュメントは `.shiki/reference/` に自動アーカイブされます）

---

```
              ┌─────────────────────────────────────────┐
              │           Shiki（式） v2                 │
              │    Goal → Plan → Team → Dev → Release   │
              └──────────────┬──────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
     ┌────────▼────────┐         ┌──────────▼─────────┐
     │   CLI Mode       │         │   GitHub Mode       │
     │ Agent Teams      │         │ Issue/Label/DAG     │
     │ tmux/iTerm2      │         │ Worktree/Actions    │
     │ Delegate Mode    │         │ Budget Tracking     │
     └────────┬────────┘         └──────────┬─────────┘
              │                             │
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │        共有コア基盤          │
              │  .shiki/ + Schemas + Hooks │
              │  θ収束モデル + 4層権限      │
              │  Dual Engine Architecture  │
              └──────────┬─────────────────┘
                         │
              ┌──────────▼──────────────────┐
              │  Claude          Codex      │
              │  協調・判断      隔離実行    │
              │  計画/レビュー   実装/テスト  │
              │  ← Smart Router + Fallback →│
              └─────────────────────────────┘
```

**CLIモード**：Agent Teams ネイティブ + tmux/iTerm2 で最大並列効率
**GitHubモード**：Issue/Label/DAG 駆動 + Worktree 分離 + Dual Engine 並列
**Dual Engine**：Claude（判断・協調）+ Codex（隔離実行）を Smart Router で自動振分、Fallback Chain で耐障害性確保

> **初めての方へ**: [SETUP.md](./SETUP.md)（セットアップ） → [USAGE.md](./USAGE.md)（使い方） → [docs/16_best_practices.md](./docs/16_best_practices.md)（ベストプラクティス）の順に読んでください。
>
> **新規プロジェクト作成後**: `shiki new` によりドキュメントは `.shiki/reference/` にアーカイブされます。

---

## 1. アーキテクチャ概要

### デュアルモード
| | CLIモード | GitHubモード |
|---|---|---|
| **起動** | `scripts/start_cli_session.sh` | Issue + `ai-goal` ラベル |
| **実行基盤** | Agent Teams (TeamCreate等) | GitHub Actions |
| **並列化** | tmux/iTerm2 ペイン | Worktree + DAG バッチ |
| **状態管理** | `~/.claude/teams/` + `.shiki/` | Issue/PR/Labels + `.shiki/` |
| **Leader制御** | Delegate Mode | Orchestrator Workflow |

### 4層権限モデル
| 層 | 役割例 | 許可 | 制限 |
|---|---|---|---|
| **guardian** | 人間 | 全権限・緊急停止・憲法修正 | なし（人間のみ） |
| **coordinator** | TeamLeader, Architect | 計画・調整・承認 | Edit/Write禁止 |
| **executor** | Implementer, QA | 実装・テスト | 所有権スコープ内のみ |
| **monitor** | SecurityReviewer | 読取・検査 | 実装ファイル編集不可 |

### θ収束モデル（6フェーズ）
```
θ₁ UNDERSTAND → θ₂ GENERATE → θ₃ ALLOCATE → θ₄ EXECUTE → θ₅ VERIFY → θ₆ INTEGRATE
     ↑                                                           │
     └───────────────── 品質不足なら再反復 ──────────────────────┘
```

---

## 2. ディレクトリ構造

```
.shiki/           ブリッジ層（タスク/契約/レポート/DAG/セッション状態/スキーマ）
.claude/            Claude Code 設定
  ├── agents/       エージェント定義（coordinator/executor/monitor）
  ├── hooks/        Hook スクリプト（安全/品質/同期）
  └── settings.json Hook 登録
.github/            GitHub統合
  ├── workflows/    自律開発ワークフロー
  ├── ISSUE_TEMPLATE/ Issueテンプレート
  └── labels.json   53ラベル定義
CLAUDE.md           Claude（Agent Teams含む）向け「憲法」
AGENTS.md           Codex向け「憲法」
roles/              役割定義（3層権限モデル）
docs/               ドキュメント（17ファイル）
scripts/            補助スクリプト
templates/          テンプレート集
examples/           サンプルファイル
```

---

## 3. クイックスタート：CLIモード

### 3-1) 事前準備
- Git
- Python 3.10+
- tmux（推奨）またはiTerm2
- Claude Code CLI

### 3-2) 初期化
```bash
./scripts/init_shiki.sh
# モード選択で「1) CLI mode」を選ぶ
```

### 3-3) Agent Teams 有効化
```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

永続化は `templates/home-claude-settings.json` を参考に `~/.claude/settings.json` を編集。

### 3-4) Codex CLI の導入（任意）
```bash
npm i -g @openai/codex

# 方式A: Pro/Plus プランログイン（推奨・サブスク枠内）
codex login

# 方式B: API キー（従量課金）
export OPENAI_API_KEY="sk-..."

# MCP 登録（認証情報は自動継承）
claude mcp add --transport stdio --scope project codex -- codex mcp-server
```

### 3-5) セッション開始
```bash
./scripts/start_cli_session.sh
```

### 3-6) ゴールを書いて実行
```bash
cp templates/GOAL.template.md GOAL.md
# GOAL.md を編集
claude
```

プロンプト例は `docs/08_prompts_library.md` を参照。

---

## 4. クイックスタート：GitHubモード

### 4-1) GitHub Secrets
Repo → Settings → Secrets → New repository secret

**方式A: Max プラン OAuth（推奨・従量課金なし）**
```bash
claude /install-github-app   # GitHub App インストール
claude setup-token            # OAuth トークン生成 → CLAUDE_CODE_OAUTH_TOKEN に設定
```

**方式B: API キー（従量課金）**
- `ANTHROPIC_API_KEY`

**Codex 連携（任意）**
- `OPENAI_API_KEY`（GitHub Actions での Codex 使用に必須。CI はブラウザ認証不可のため API キーが必要）

### 4-2) ラベル作成
```bash
./scripts/setup_github_labels.sh
```

### 4-3) ワークフロー配置
`.github/workflows/` をそのままリポジトリにコミット。

### 4-4) Issue から起動
1. Issue を作成（`shiki_task` テンプレート使用）
2. `ai-goal` ラベルを付与 → orchestrator が自動起動
3. または本文にゴールを書いてコメント `@claude plan`

---

## 5. モード選択ガイド

| 状況 | 推奨モード |
|---|---|
| 対話型で素早くプロトタイプ | CLI |
| チームで非同期開発 | GitHub |
| CI/CD自動化が重要 | GitHub |
| デバッグ/調査 | CLI |
| 大規模タスク（10+タスク） | GitHub（DAG並列） |
| 小規模タスク（1-5タスク） | CLI（直接的） |

---

## 6. 安全策
- `docs/07_security_and_guardrails.md` を参照
- **自動マージ/自動デプロイは最初はOFF**にして、段階的に上げてください
- 4層権限モデル（guardian/coordinator/executor/monitor）により、不正な操作は防止されます

---

## 7. ドキュメント一覧
- `docs/00_concepts.md` - 設計思想（8原則）
- `docs/01_workflow_goal_to_release.md` - ワークフロー全体
- `docs/02_roles_and_team_builder.md` - 役割と権限層
- `docs/03_task_contract_schemas.md` - スキーマ詳細
- `docs/04_local_setup_claude_code.md` - CLIモードセットアップ
- `docs/05_local_setup_codex.md` - Codex連携
- `docs/06_github_actions_setup.md` - GitHub Actions
- `docs/07_security_and_guardrails.md` - セキュリティ
- `docs/08_prompts_library.md` - プロンプト集
- `docs/09_mcp_bridge_patterns.md` - MCP/ブリッジ
- `docs/10_agent_teams_runbook.md` - Agent Teams運用
- `docs/11_cli_mode_guide.md` - CLIモードガイド
- `docs/12_authority_model.md` - 権限モデル詳説
- `docs/13_convergence_model.md` - θ収束モデル
- `docs/14_session_recovery.md` - セッション復旧
- `docs/15_ldd_protocol.md` - Log-Driven Development
- `docs/16_best_practices.md` - ベストプラクティス
