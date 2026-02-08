# タスク・契約・レポート・DAG・セッションのスキーマ

このフレームワークは「ファイルを事実（Source of Truth）」として扱います。
AIが忘れても復旧できるよう、最低限の構造を固定します。

---

## Task（.shiki/tasks/*.json）
- `id`：一意ID（例: T-0001）
- `title`：短い説明
- `assigned_to`：`claude-team` / `claude-leader` / `claude-member` / `codex` / `human`
- `status`：`pending` / `in_progress` / `review` / `completed` / `blocked` / `failed`
- `priority`：`low` / `medium` / `high` / `critical`
- `authority_layer`：必要な権限層（`coordinator` / `executor` / `monitor`）
- `mode_origin`：作成元モード（`cli` / `github`）
- `dag_node_id`：DAGノード参照（DAG実行時）
- `theta_phase`：現在のθフェーズ（`understand`〜`integrate`）
- `budget`：トークン予算（`estimated_tokens` / `actual_tokens` / `max_tokens`）
- `worktree_branch`：worktreeブランチ名（GitHubモード）
- `depends_on`：前提タスク
- `context`：対象ファイル、設計前提、リンク等
- `acceptance`：合格条件（実行コマンド、チェック項目）
- `outputs`：成果物ファイルパス
- `claimed_by`：担当者（例: codex-gh-action）
- `lease_expires_at`：放置検知用（任意）

JSON Schema: `.shiki/schemas/task.schema.json`

---

## Contract（.shiki/contracts/*.json）
- `contract_id`：一意ID
- `version`：セマンティックバージョン（例: 1.0.0）
- `status`：`proposed` → `agreed` → `implemented` → `verified` → `integrated`
- `mode_origin`：作成元モード（`cli` / `github`）
- `defined_by` / `implemented_by`
- `review_required_by`：レビュー必要な権限層の配列（例: `["coordinator"]`）
- `interface`：OpenAPI/型/イベントなど
- `tests_path`：契約テスト（任意）

JSON Schema: `.shiki/schemas/contract.schema.json`

---

## DAG（.shiki/dag/*.json）
- `dag_id`：一意ID
- `plan_ref`：参照するPlanファイル
- `status`：`pending` / `running` / `completed` / `failed` / `cancelled`
- `theta_phase`：現在のθフェーズ
- `nodes`：ノード配列
  - `node_id`：ノードID
  - `task_id`：タスク参照
  - `batch`：並列バッチ番号（同じバッチは同時実行）
  - `status`：`pending` / `running` / `completed` / `failed` / `skipped`
  - `worktree_branch`：worktreeブランチ名
  - `engine`：実行エンジン
- `edges`：依存関係（from/to/type）

JSON Schema: `.shiki/schemas/dag.schema.json`

---

## Session State（.shiki/state/session-*.json）
- `session_id`：セッションID
- `mode`：`cli` / `github`
- `timestamp`：スナップショット時刻
- `team_name`：チーム名（CLIモード）
- `members`：メンバー一覧（名前/役割/権限層/状態）
- `active_tasks`：アクティブタスク一覧
- `theta_phase`：現在のθフェーズ
- `budget`：トークン使用量/残量/上限
- `dag_ref`：DAGファイル参照
- `git_state`：ブランチ/コミット/ダーティファイル

JSON Schema: `.shiki/schemas/session.schema.json`

---

## Report（.shiki/reports/*.md）
標準フォーマットは `templates/REPORT.template.md`。

- 変更ファイル
- 実行したコマンド
- 追加テスト
- 懸念点
- 次への引き継ぎ

---

## バリデーション
- `scripts/validate_shiki.py`（DAGサイクル検出、バジェット検証対応）
- きちんとやるなら `jsonschema` と `pyyaml` を入れてCIでチェックする（推奨）
