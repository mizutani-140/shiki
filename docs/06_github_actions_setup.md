# GitHub Actions セットアップ（Issue/PR 駆動）

## 0) 大事な前提
GitHub Actions で自律開発を回す場合、通常は **APIキー（Secrets）** が必要です。

- `ANTHROPIC_API_KEY`（Claude Code Action 用）
- `OPENAI_API_KEY`（Codex Action 用）

このZipの workflows は「まず動く」ことを優先してテンプレ化しています。
最初は **自動マージ/自動デプロイはOFF** で導入してください。

---

## 1) Secrets を設定
Repo → Settings → Secrets and variables → Actions → New repository secret

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

---

## 2) Workflows をコミット
`.github/workflows/` をそのまま配置して commit/push します。

- `shiki-plan.yml`：Issueコメント `@claude plan` で PLAN/Tasks/Contracts を提案PRとして作る
- `shiki-codex-worker.yml`：タスク追加を検知して実装PRを作る
- `shiki-review.yml`：Codex PR を Claude がレビューする
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

