# ワークフロー：Goal → Plan → Team → Execute → Integrate → Release

このフレームワークは、常に同じ順番で回します。
**CLIモード**と**GitHubモード**で実行経路が分岐しますが、基本フローは同じです。

---

## Phase 0: Goal（ユーザーがゴールを設定）
ユーザーが以下を埋めます（GOAL.md または Issue テンプレ）。

- 何を達成したいか（最終成果）
- 受け入れ条件（Acceptance Criteria）
- 制約（禁止事項・依存制約・コスト/期限）
- 既存システムの前提（リポジトリ、言語、実行環境）

**ポイント**：ゴールが曖昧なまま進めると、途中でAIの判断が分岐して破綻します。

---

## Phase 1: Plan（θ₁ UNDERSTAND + θ₂ GENERATE）

### CLIモード
- Leader（coordinator）が Plan Mode で設計
- `TeamCreate` でチーム作成、`TaskCreate` でタスク分解
- Agent Teams ネイティブツールで計画を共有

### GitHubモード
- Issue コメント `@claude plan` で起動
- `shiki-orchestrator.yml` が θ₁→θ₂ を実行
- `.shiki/plans/PLAN.md` と `.shiki/tasks/` を PR で提出

### 共通成果物
- 仕様の穴の洗い出し
- 分割戦略（どこを並列化できるか）
- Contracts（境界の合意）案
- Tasks（担当/状態/受け入れ基準）の草案
- Team（必要な役割/人数）の提案

---

## Phase 2: Team（θ₃ ALLOCATE）

### CLIモード
- Planで決めた役割に基づき、Agent Teams のメンバーを起動
- 各メンバーは Plan Mode で実行計画を作成→Leader 承認
- Delegate Mode：Leader は調整に専念、実装はメンバーに委譲
- DAG を `.shiki/dag/` に生成

### GitHubモード
- DAG（依存グラフ）を生成
- `shiki-dag-executor.yml` がバッチ並列実行を管理
- worktree でブランチ分離

### 共通チーム構成例
- Architect（設計・契約）
- Implementer（実装）
- QA（テスト・品質）
- Security（脆弱性/安全性）
- Release（統合・リリース）

**ポイント**：最初から人数を増やしすぎない（コストが爆発しやすい）。

---

## Phase 3: Execute（θ₄ EXECUTE）

### CLIモード
- メンバーが `TaskList` でタスクを確認、セルフクレーム
- executor 層が実装、monitor 層が監視
- `SendMessage` でP2P通信
- バジェット追跡

### GitHubモード
- DAG executor がバッチ毎に worktree worker を起動
- Codex/Claude が worktree 内で実装
- 受け入れ条件の自動検証
- ラベル自動更新

### 共通ルール
- 受け入れ条件（acceptance）に合格するまで繰り返す
- 同一ファイルの同時編集を避ける（ファイルオーナーシップを決める）

---

## Phase 4: Integrate（θ₅ VERIFY + θ₆ INTEGRATE）

### CLIモード
- coordinator が統合レビュー
- monitor（SecurityReviewer）がセキュリティチェック
- Contract 検証
- `SendMessage` で承認通知

### GitHubモード
- `shiki-review.yml` で自動レビュー
- worktree ブランチのマージ
- ラベル同期
- CI パス確認

---

## Phase 5: Release（CI/CD）
- CI（lint/test/security）を通す
- Deployは環境/承認ゲートを設定して段階的に自動化
- 失敗時は "自動修復"（Codex）→ "自動レビュー"（Claude）→ "再実行"

---

## おすすめの導入順（事故らない順）
1) ローカルで Plan → Team → 小さい実装
2) GitHub Actions で「PRレビューだけ自動」
3) Issue→PR を自動
4) CI失敗時の自動修復
5) 条件付きの自動マージ
6) デプロイ自動化（最後）
