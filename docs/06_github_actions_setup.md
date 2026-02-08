# GitHub Actions セットアップ（Issue/PR 駆動）

## 0) 大事な前提
GitHub Actions で自律開発を回す場合、認証設定が必要です。
2つの認証方式があり、**Max プラン OAuth が推奨**です。

このZipの workflows は「まず動く」ことを優先してテンプレ化しています。
最初は **自動マージ/自動デプロイはOFF** で導入してください。

---

## 1) 認証を設定

Repo → Settings → Secrets and variables → Actions → New repository secret

### 方式A: Max プラン OAuth（推奨）

Claude Max / Pro プランのサブスクリプション枠内で GitHub Actions を実行できます。
API の従量課金が発生しないため、コスト面で有利です。

#### セットアップ手順

```bash
# Step 1: Claude GitHub App のインストール
# ブラウザで https://claude.ai/settings を開き、"Install GitHub App" をクリック
# 対象リポジトリへのアクセスを許可

# Step 2: OAuth トークンの生成
claude /install-github-app   # GitHub App がまだなら
claude setup-token            # OAuth トークンを生成

# Step 3: GitHub Secret に登録
# 表示されたトークンをコピーし、リポジトリの Secret に設定:
#   Secret 名: CLAUDE_CODE_OAUTH_TOKEN
```

| Secret 名 | 必須 | 内容 |
|-----------|------|------|
| `CLAUDE_CODE_OAUTH_TOKEN` | **必須** | `claude setup-token` で取得した OAuth トークン |
| `OPENAI_API_KEY` | 任意 | Codex API キー。Codex 使用時に**必須**（CI はブラウザ認証不可のため、Pro plan login は使用不可） |

### 方式B: API キー（従量課金）

Anthropic API の従量課金で実行する従来の方式です。

| Secret 名 | 必須 | 内容 |
|-----------|------|------|
| `ANTHROPIC_API_KEY` | **必須** | Anthropic Console で取得した API キー |
| `OPENAI_API_KEY` | 任意 | Codex API キー。Codex 使用時に**必須**（CI はブラウザ認証不可のため、Pro plan login は使用不可） |

### 両方設定した場合

`CLAUDE_CODE_OAUTH_TOKEN` と `ANTHROPIC_API_KEY` の両方が設定されている場合、
**OAuth トークンが優先**されます。移行期間中は両方設定しておくことも可能です。

### Codex の認証について

GitHub Actions（CI 環境）では Codex の `codex login`（ブラウザ OAuth）は使用できません。
Codex を GitHub Actions で使用する場合は、必ず `OPENAI_API_KEY` を GitHub Secret に設定してください。

| 環境 | Claude 認証 | Codex 認証 |
|------|------------|-----------|
| CLI モード | CLI ログイン済み | `codex login` (Pro plan) 推奨 |
| GitHub Actions | `CLAUDE_CODE_OAUTH_TOKEN` 推奨 | `OPENAI_API_KEY` 必須 |

---

## 2) Workflows をコミット
`.github/workflows/` をそのまま配置して commit/push します。

- `claude.yml`：Issue/PR で `@claude` メンションに応答する基本ワークフロー
- `shiki-orchestrator.yml`：`ai-goal` ラベルで θ₁→θ₂→θ₃ を自動実行
- `shiki-plan.yml`：Issueコメント `@claude plan` で PLAN/Tasks/Contracts を提案PRとして作る
- `shiki-dag-executor.yml`：DAG に基づきバッチ並列でタスクを実行
- `shiki-worktree-worker.yml`：個別タスクを worktree ブランチで実行
- `shiki-codex-worker.yml`：タスク追加を検知して Codex で実装PRを作る
- `shiki-review.yml`：PR を Claude がレビューする（θ₅ VERIFY）
- `shiki-ci-autofix.yml`：CI 失敗時に Codex で自動修復
- `shiki-budget-check.yml`：トークン使用量の定期監視
- `shiki-label-sync.yml`：Issue ラベルとタスク JSON の双方向同期
- `ci.yml`：あなたのプロジェクト用に編集（テスト・ビルド）

---

## 3) 使い方（最小）
1) Issue を作る（テンプレ `templates/ISSUE.template.md` を参考）
2) Issue コメント：
   - `@claude plan`
3) Claude が Plan PR を作る（内容を確認してマージ）
4) Codex が実装PRを作る
5) Claude がレビューする
6) 人間がマージする（最初は必ず人間ゲート）

---

## 4) CI/CD を自動に寄せる（段階的）
- Phase 1：レビューだけ自動
- Phase 2：小粒な変更だけ自動マージ
- Phase 3：CI失敗時の自動修復（Codex→PR）
- Phase 4：デプロイは環境承認ゲート（GitHub Environments）を使う

