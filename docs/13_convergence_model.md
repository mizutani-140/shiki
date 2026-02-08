# θ収束モデル（Convergence Model）

θ収束モデルは、Shiki（式） の開発プロセスを6つのフェーズ（θ₁〜θ₆）に分割し、各フェーズの品質基準（exit_criteria）を満たしながら段階的にゴールに収束させる仕組みです。

---

## 目次

1. [収束モデルの全体像](#1-収束モデルの全体像)
2. [各フェーズの詳細](#2-各フェーズの詳細)
3. [フェーズ遷移と再反復](#3-フェーズ遷移と再反復)
4. [品質メトリクス](#4-品質メトリクス)
5. [タスク状態・DAGとの対応](#5-タスク状態dagとの対応)
6. [バジェットへの影響](#6-バジェットへの影響)

---

## 1. 収束モデルの全体像

```
θ₁ UNDERSTAND → θ₂ GENERATE → θ₃ ALLOCATE → θ₄ EXECUTE → θ₅ VERIFY → θ₆ INTEGRATE
     ↑              ↑              ↑              ↑             ↑
     └──────────────┴──────────────┴──────────────┴─────────────┘
                         再反復（品質不足時）
```

### 基本原則

- **順方向進行**: 通常は θ₁ → θ₂ → ... → θ₆ と順番に進む
- **exit_criteria 必須**: 各フェーズの exit_criteria をすべて満たさないと次に進めない
- **再反復可能**: 品質不足が発見された場合、任意の前フェーズに戻れる
- **θフェーズ記録**: すべてのタスクに `theta_phase` を記録し、追跡可能にする

---

## 2. 各フェーズの詳細

### θ₁ UNDERSTAND（理解フェーズ）

#### 目的
ユーザーのゴールを正確に理解し、開発スコープを確定する。

#### 主な活動
- GOAL.md の読解と分析
- ユーザーの受け入れ条件（acceptance criteria）の明確化
- スコープの境界定義（何をやるか/何をやらないか）
- 技術的制約の洗い出し
- 不明点の質問リスト作成

#### entry_criteria
- GOAL.md が存在すること
- ユーザーからゴールが提示されていること

#### exit_criteria

| 基準 | 説明 | 検証方法 |
|---|---|---|
| `acceptance_criteria_defined` | 受け入れ条件が明文化されている | GOAL.md にチェックリストが記載されている |
| `scope_bounded` | スコープが明確に定義されている | 「やること/やらないこと」リストが存在する |

#### 成果物
- GOAL.md（更新済み: 受け入れ条件が追記されている）
- スコープ定義書（`.shiki/plans/` 内）

#### 担当権限層
- **coordinator**: 主導
- executor: 技術調査のサポート
- monitor: -

---

### θ₂ GENERATE（生成フェーズ）

#### 目的
設計を策定し、タスクを分解し、インターフェース契約を定義する。

#### 主な活動
- アーキテクチャ設計
- タスク分解（5〜15タスク程度の粒度）
- 契約（contracts）の策定
- リスク分析と緩和策

#### entry_criteria
- θ₁ の exit_criteria がすべて満たされていること

#### exit_criteria

| 基準 | 説明 | 検証方法 |
|---|---|---|
| `plan_created` | 設計計画が作成されている | `.shiki/plans/PLAN.md` が存在する |
| `tasks_decomposed` | タスクが分解されている | `.shiki/tasks/` に JSON ファイルが存在する |
| `contracts_drafted` | 境界の契約が策定されている | `.shiki/contracts/` に JSON ファイルが存在する（必要な場合） |

#### 成果物
- `.shiki/plans/PLAN.md` — 設計計画
- `.shiki/tasks/*.json` — タスク定義
- `.shiki/contracts/*.json` — インターフェース契約

#### 担当権限層
- **coordinator**: 設計方針決定、タスク分解
- executor: 技術的実現可能性の評価
- monitor: -

---

### θ₃ ALLOCATE（配分フェーズ）

#### 目的
タスクの実行順序を決定し、リソース（メンバー/ツール）を割り当てる。

#### 主な活動
- DAG（有向非巡回グラフ）の構築
- メンバーへのロール/タスク割当
- ファイル所有権スコープの定義
- 依存関係の解決と並列バッチの定義
- バジェット配分

#### entry_criteria
- θ₂ の exit_criteria がすべて満たされていること

#### exit_criteria

| 基準 | 説明 | 検証方法 |
|---|---|---|
| `dag_valid` | DAG が有効（循環なし、依存解決済み） | `.shiki/dag/` の DAG ファイルが検証済み |
| `roles_assigned` | 全タスクに担当が割り当てられている | 全タスクの `assigned_to` が設定されている |
| `dependencies_resolved` | 依存関係が解決されている | ブロック状態のタスクがない（想定ブロック除く） |

#### 成果物
- `.shiki/dag/*.json` — DAG 定義
- `.shiki/tasks/*.json` — 割当済みタスク定義

#### 担当権限層
- **coordinator**: 割当決定、DAG 構築指示
- executor: Self-Claim 申請
- monitor: -

---

### θ₄ EXECUTE（実行フェーズ）

#### 目的
タスクを実装し、テストを通す。

#### 主な活動
- 各メンバーが Plan → 承認 → Standard で実装
- テストの実行と合格確認
- タスク状態の更新（pending → in_progress → review → completed）
- レポートの作成

#### entry_criteria
- θ₃ の exit_criteria がすべて満たされていること
- 少なくとも1つのタスクが `pending` であること

#### exit_criteria

| 基準 | 説明 | 検証方法 |
|---|---|---|
| `all_tasks_completed` | 全タスクが completed | 全タスクの status が `completed` |
| `acceptance_passed` | 受け入れ条件に合格 | 各タスクの acceptance がすべてクリア |

#### 成果物
- 実装コード（各タスクの対象ファイル）
- テストコード
- `.shiki/reports/*.md` — タスク完了レポート

#### 担当権限層
- coordinator: 承認、進捗管理
- **executor**: 実装・テスト
- monitor: レビュー（次フェーズの先行実施も可）

---

### θ₅ VERIFY（検証フェーズ）

#### 目的
実装の品質、セキュリティ、契約遵守を検証する。

#### 主な活動
- コードレビュー
- セキュリティ監査
- 契約検証（API シグネチャ、型、レスポンス形式の一致）
- 統合テストの実行
- ブロッキングタスクの解決

#### entry_criteria
- θ₄ の exit_criteria がすべて満たされていること

#### exit_criteria

| 基準 | 説明 | 検証方法 |
|---|---|---|
| `review_approved` | コードレビューが承認されている | coordinator が全変更を承認済み |
| `security_passed` | セキュリティチェックに合格 | monitor の Critical/High 問題がゼロ |
| `contracts_verified` | 契約が検証済み | 全契約の status が `verified` |

#### 成果物
- Monitor レポート（品質・セキュリティ）
- 契約検証レポート
- 修正タスク（問題があった場合）

#### 担当権限層
- coordinator: レビュー承認、修正タスク管理
- executor: 修正タスクの実装
- **monitor**: 品質・セキュリティ検査

---

### θ₆ INTEGRATE（統合フェーズ）

#### 目的
全成果物を統合し、最終テストを通し、リリース/マージする。

#### 主な活動
- ブランチの統合/マージ
- CI/CD パイプラインの実行
- 最終テスト（回帰テスト含む）
- リリースタグの付与
- ドキュメントの最終更新

#### entry_criteria
- θ₅ の exit_criteria がすべて満たされていること

#### exit_criteria

| 基準 | 説明 | 検証方法 |
|---|---|---|
| `ci_passed` | CI が合格している | GitHub Actions / ローカルテストが全パス |
| `merged` | メインブランチにマージされている | PR がマージ済み |
| `deployed_or_tagged` | デプロイまたはタグ付けが完了 | リリースタグまたはデプロイ確認 |

#### 成果物
- マージ済みの PR
- リリースタグ
- 最終レポート

#### 担当権限層
- **coordinator**: 統合判断、マージ承認
- executor: CI 修正
- monitor: 最終検証

---

## 3. フェーズ遷移と再反復

### 進行判断フロー

```
フェーズ N の作業完了
  ↓
exit_criteria の評価
  ↓
┌─── すべて満たされた → フェーズ N+1 へ進行
│
└─── 一部未達 → 判断:
       ├── 現フェーズ内で修正可能 → 修正してから再評価
       └── 前フェーズの成果物に問題 → 前フェーズに再反復
```

### 再反復（Re-iteration）の条件

以下の場合にフェーズを戻します：

| 状況 | 戻り先 | 例 |
|---|---|---|
| 受け入れ条件の解釈間違い | θ₁ UNDERSTAND | テストが受け入れ条件と一致しない |
| 設計の根本的な問題 | θ₂ GENERATE | アーキテクチャの変更が必要 |
| タスク分解の粒度問題 | θ₂ GENERATE | タスクが大きすぎて管理不能 |
| 依存関係の見落とし | θ₃ ALLOCATE | 循環依存の発見 |
| 実装の品質問題 | θ₄ EXECUTE | テスト不合格、バグ発見 |
| セキュリティ問題 | θ₄ EXECUTE | monitor が Critical 問題を検出 |
| 契約違反の発見 | θ₂ GENERATE | 契約の再定義が必要 |

### 再反復の手順

1. **coordinator が判断**: 品質不足の根本原因を分析し、戻り先フェーズを決定
2. **チームに通知**: `SendMessage` で全メンバーにフェーズ変更を通知
3. **タスク状態の調整**: 影響を受けるタスクを `pending` に戻す
4. **セッション状態の更新**: `theta_phase` を更新フェーズに変更
5. **作業再開**: 戻り先フェーズの活動を再実施
6. **再評価**: exit_criteria を再度評価して進行判断

### 再反復回数の上限

明確な上限は設けませんが、バジェット消費との兼ね合いを考慮します。
同じフェーズに3回以上戻る場合は、根本的な問題（ゴール設定、チーム編成等）を見直す必要があります。

---

## 4. 品質メトリクス

### フェーズ別メトリクス

#### θ₁ UNDERSTAND
| メトリクス | 基準 | 測定方法 |
|---|---|---|
| 受け入れ条件の明確性 | 曖昧な表現がないこと | チェックリスト形式で検証可能 |
| スコープの完全性 | 漏れがないこと | 「やること/やらないこと」の対称性 |
| リスク識別率 | 主要リスクが識別されていること | リスク一覧が作成されている |

#### θ₂ GENERATE
| メトリクス | 基準 | 測定方法 |
|---|---|---|
| タスク粒度 | 1タスク = 数時間で完了可能 | 推定トークン数が上限内 |
| 契約完全性 | 全境界に契約がある | API/型の契約がすべて定義されている |
| 設計一貫性 | 矛盾がないこと | アーキテクチャ図と実装計画が一致 |

#### θ₃ ALLOCATE
| メトリクス | 基準 | 測定方法 |
|---|---|---|
| DAG 有効性 | 循環なし | グラフ検証ツールで確認 |
| 負荷分散 | メンバー間の偏りがないこと | タスク数/推定トークンの分散 |
| 依存解決率 | 100% | 未解決の依存がゼロ |

#### θ₄ EXECUTE
| メトリクス | 基準 | 測定方法 |
|---|---|---|
| タスク完了率 | 100% | completed / total |
| テストカバレッジ | プロジェクト基準以上 | カバレッジツールで測定 |
| 受け入れ条件合格率 | 100% | 全 acceptance がクリア |

#### θ₅ VERIFY
| メトリクス | 基準 | 測定方法 |
|---|---|---|
| Critical 問題数 | 0 | monitor レポートで確認 |
| High 問題数 | 0 | monitor レポートで確認 |
| 契約検証率 | 100% | 全契約が verified |
| レビュー承認率 | 100% | coordinator が全承認 |

#### θ₆ INTEGRATE
| メトリクス | 基準 | 測定方法 |
|---|---|---|
| CI 合格率 | 100% | CI パイプラインの結果 |
| 回帰テスト合格率 | 100% | 既存テストがすべて通過 |
| マージコンフリクト | 0 | git merge の結果 |

---

## 5. タスク状態・DAGとの対応

### タスク状態とθフェーズの関係

| タスク status | 典型的なθフェーズ | 説明 |
|---|---|---|
| `pending` | θ₂〜θ₃ | 作成済みだが未着手 |
| `in_progress` | θ₄ | 実装中 |
| `review` | θ₅ | レビュー待ち |
| `completed` | θ₅〜θ₆ | 完了 |
| `blocked` | 任意 | ブロック中 |
| `failed` | 任意 | 失敗 |

### DAG 実行とθフェーズ

DAG の `theta_phase` フィールドは、その DAG が属するフェーズを示します。

```json
{
  "dag_id": "dag-execute-001",
  "theta_phase": "execute",
  "nodes": [
    { "node_id": "n1", "task_id": "T-0001", "batch": 0, "status": "completed" },
    { "node_id": "n2", "task_id": "T-0002", "batch": 0, "status": "running" },
    { "node_id": "n3", "task_id": "T-0003", "batch": 1, "status": "pending" }
  ]
}
```

### DAG バッチ実行

同じ `batch` 番号のノードは並列実行できます。バッチ N のすべてのノードが完了してからバッチ N+1 のノードが実行されます。

```
Batch 0: [T-0001, T-0002]  ← 並列実行
           ↓
Batch 1: [T-0003]           ← Batch 0 完了後に実行
           ↓
Batch 2: [T-0004, T-0005]  ← Batch 1 完了後に並列実行
```

---

## 6. バジェットへの影響

### 再反復によるバジェット消費

再反復は追加のトークン消費を伴います。coordinator は再反復の判断時にバジェットへの影響を考慮する必要があります。

### 再反復コストの見積もり

| 戻り先フェーズ | 追加コスト見積もり | 理由 |
|---|---|---|
| θ₁ → 再実行 | 高（セッション全体の 30〜50%） | ほぼ全工程をやり直す |
| θ₂ → 再実行 | 中〜高（20〜40%） | 設計変更→実装やり直し |
| θ₃ → 再実行 | 中（15〜25%） | 再割当→部分的やり直し |
| θ₄ → 再実行 | 低〜中（10〜20%） | 修正タスクの追加実行 |
| θ₅ → 再実行 | 低（5〜10%） | 修正と再検証 |

### バジェット枯渇時の対応

バジェットが不足した場合の判断フロー：

```
バジェット残量 < 必要量
  ↓
coordinator が判断:
  ├── 必須タスクのみ実行 → 低優先度をスキップ
  ├── ユーザーに追加承認を求める → バジェット上限を引き上げ
  └── 現状で完了可能な範囲に縮退 → スコープを縮小
```

### 推奨: バジェット配分の目安

| フェーズ | 推奨配分 |
|---|---|
| θ₁ UNDERSTAND | 5% |
| θ₂ GENERATE | 10% |
| θ₃ ALLOCATE | 5% |
| θ₄ EXECUTE | 50% |
| θ₅ VERIFY | 20% |
| θ₆ INTEGRATE | 10% |

**予備**: 常に 10〜20% の予備を確保し、再反復に備えてください。

---

## 付録: config.yaml の convergence セクション

```yaml
convergence:
  theta_1_understand:
    description: "Goal comprehension and scope definition"
    exit_criteria: ["acceptance_criteria_defined", "scope_bounded"]
  theta_2_generate:
    description: "Plan generation and task decomposition"
    exit_criteria: ["plan_created", "tasks_decomposed", "contracts_drafted"]
  theta_3_allocate:
    description: "Resource allocation and DAG construction"
    exit_criteria: ["dag_valid", "roles_assigned", "dependencies_resolved"]
  theta_4_execute:
    description: "Implementation and testing"
    exit_criteria: ["all_tasks_completed", "acceptance_passed"]
  theta_5_verify:
    description: "Review, security, and quality verification"
    exit_criteria: ["review_approved", "security_passed", "contracts_verified"]
  theta_6_integrate:
    description: "Integration, merge, and release"
    exit_criteria: ["ci_passed", "merged", "deployed_or_tagged"]
```
