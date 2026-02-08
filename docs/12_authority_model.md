# 権限モデル（Authority Model）

Shiki（式） は **4層の権限モデル**を採用し、各層の責務と制約を明確に分離することで、無秩序な変更を防ぎ、開発プロセスの安全性と予測可能性を確保します。

---

## 目次

1. [4層権限モデルの概要](#1-4層権限モデルの概要)
2. [各層の詳細](#2-各層の詳細)
3. [権限マトリクス](#3-権限マトリクス)
4. [エスカレーション手順](#4-エスカレーション手順)
5. [CLIエージェントタイプとの対応](#5-cliエージェントタイプとの対応)
6. [GitHubジョブタイプとの対応](#6-githubジョブタイプとの対応)
7. [権限違反の防止と検出](#7-権限違反の防止と検出)

---

## 1. 4層権限モデルの概要

```
┌─────────────────────────────────────────────┐
│           guardian（人間の最終権限）          │
│  全権限・緊急停止・憲法修正 │ 人間のみ       │
├─────────────────────────────────────────────┤
│           coordinator（調整層）              │
│  計画・調整・承認 │ Edit/Write 禁止          │
├─────────────────────────────────────────────┤
│           executor（実行層）                 │
│  実装・テスト │ 所有権スコープ内のみ編集可   │
├─────────────────────────────────────────────┤
│           monitor（監視層）                  │
│  読取・検査 │ 編集不可・ブロッキングタスク可  │
└─────────────────────────────────────────────┘
```

### 設計原則

- **人間の最終権限**: guardian（人間）が全ての重要判断の最終承認者
- **関心の分離**: 計画する人と実装する人と検査する人を分ける
- **最小権限**: 各層は必要最小限のツールのみ使用可能
- **上位層の承認**: executor の実行は coordinator の承認が必要
- **独立した監視**: monitor は実装から独立し、客観的な評価を行う
- **95%自律・5%人間介入**: 日常業務はAIが自律的に処理し、重要判断のみ guardian が介入

---

## 2. 各層の詳細

### 2.0 guardian（人間の最終権限）

#### 役割
人間の最終権限者として、システム全体の安全性と方向性を保証します。AIエージェントには割り当てられません。

#### 権限
- 全権限（coordinator 以下の全ての操作が可能）
- 緊急停止命令
- 憲法（CLAUDE.md）の修正承認
- バジェット変更承認
- 本番デプロイ承認
- GOAL.md の変更承認

#### エスカレーション条件（coordinator → guardian）

| 重要度 | 条件 | 対応 |
|--------|------|------|
| Critical | セキュリティ脆弱性（CVSS 7.0+） | 即座に作業停止、guardian 通知 |
| Critical | バジェット150%超過 | 即座に報告、承認待ち |
| High | exec verify MAX_ITERATIONS到達 | guardian-review ラベル付与 |
| High | GOAL.md / CLAUDE.md の変更提案 | 変更案を提出、承認待ち |
| Medium | 新規依存パッケージの追加 | 理由と代替案を提示 |
| Low | θフェーズの再反復 | 報告のみ（auto_approve: true なら自動承認） |

#### CLIモードでのエスカレーション
```
coordinator → SendMessage to guardian:
"[ESCALATION] セキュリティ脆弱性を検出しました。
詳細: [問題の説明]
推奨対応: [対応案]
承認をお願いします。"
```

#### GitHubモードでのエスカレーション
- `guardian-review` ラベルを付与
- `.github/GUARDIAN.md` の手順に従う

#### 詳細
`.github/GUARDIAN.md` を参照。

---

### 2.1 coordinator（調整層）

#### 役割
チーム全体の計画策定、タスク割当、メンバーの承認・指導、θフェーズの進行管理を担当します。

#### 対応するロール（roles.yaml）
- `TeamLeader` — チーム統括
- `GoalClarifier` — ゴール明確化
- `Architect` — 設計・契約
- `ReleaseCaptain` — 統合・リリース

#### 動作モード
- **Delegate Mode 強制**: Edit/Write ツールが使用不可
- 実装に直接手を出さず、メンバーへの指示で変更を実現する

#### 責務
- GOAL.md の理解と受け入れ条件の確定
- タスク分解と優先度付け
- メンバーの Plan 提出レビューと承認
- θフェーズの exit_criteria 評価
- バジェット管理と超過時の対応
- ブロッキング問題の解決調整
- 最終レビューとマージ判断

### 2.2 executor（実行層）

#### 役割
タスクに定義された範囲内で、コードの実装・テスト・修正を行います。

#### 対応するロール（roles.yaml）
- `Implementer` — 機能実装
- `QA` — テスト設計・実行

#### 動作モード
- **Plan Mode 開始**: 起動時は読取専用
- **Standard Mode**: coordinator 承認後に編集が可能（所有権スコープ内のみ）

#### 責務
- Plan Mode で実行計画を作成し、coordinator に提出
- 承認後、ファイル所有権スコープ内で実装を行う
- 受け入れ条件（acceptance）をすべてクリアする
- `.shiki/reports/` にレポートを作成する
- バジェットを意識し、超過時は即座に報告する

### 2.3 monitor（監視層）

#### 役割
コード品質、セキュリティ、契約遵守を独立した視点で検査し、問題を報告します。

#### 対応するロール（roles.yaml）
- `SecurityReviewer` — 脆弱性・安全性レビュー

#### 動作モード
- **常時 Plan Mode**: Edit/Write は一切使用不可
- 読取専用で動作し、ブロッキングタスクの作成で問題に対処する

#### 責務
- コード品質のレビュー
- セキュリティ脆弱性の検出
- 契約遵守の確認
- ブロッキングタスクの作成（Critical/High の問題に対して）
- coordinator への定期レポート

---

## 3. 権限マトリクス

### ツール別権限

| ツール | guardian | coordinator | executor (Plan) | executor (Standard) | monitor |
|---|---|---|---|---|---|
| `Read` | OK | OK | OK | OK | OK |
| `Edit` | OK | **禁止** | **禁止** | **スコープ内** | **禁止** |
| `Write` | OK | **禁止** | **禁止** | **スコープ内** | **禁止** |
| `Bash` | OK | 制限あり | 制限あり | OK | **禁止** |
| `Grep` | OK | OK | OK | OK | OK |
| `Glob` | OK | OK | OK | OK | OK |
| `TeamCreate` | OK | OK | - | - | - |
| `SendMessage` | OK | OK | OK | OK | OK |
| `TaskCreate` | OK | OK | - | - | OK (blocking) |
| `TaskList` | OK | OK | OK | OK | OK |
| `TaskUpdate` | OK | OK | 制限あり | OK | **禁止** |
| `EnterPlanMode` | OK | OK | - | - | - |
| `ExitPlanMode` | OK | OK | - | - | - |

### アクション別権限

| アクション | guardian | coordinator | executor | monitor |
|---|---|---|---|---|
| 緊急停止 | OK | - | - | - |
| 憲法修正承認 | OK | - | - | - |
| バジェット変更承認 | OK | - | - | - |
| 本番デプロイ承認 | OK | - | - | - |
| チーム作成 | OK | OK | - | - |
| タスク割当 | OK | OK | - | - |
| Plan 承認 | OK | OK | - | - |
| ファイル編集 | OK | **禁止** | スコープ内 | **禁止** |
| テスト実行 | OK | - | OK | - |
| ブロッキングタスク作成 | OK | OK | - | OK |
| θフェーズ進行判断 | OK | OK | - | - |
| 契約変更 | OK | OK (提案) | - | - |
| セキュリティレビュー | OK | - | - | OK |
| コードレビュー | OK | - | - | OK |
| レポート作成 | OK | OK | OK | OK |
| Self-Claim 承認 | OK | OK | - | - |
| Self-Claim 申請 | - | - | OK | - |

---

## 4. エスカレーション手順

### coordinator → guardian へのエスカレーション

以下の場合、coordinator は guardian（人間）にエスカレーションする義務があります：

#### 4.0a セキュリティ脆弱性

```
[coordinator → guardian]
"[CRITICAL ESCALATION] セキュリティ脆弱性を検出しました。
ファイル: src/auth/login.ts:42
CVSS: 8.5 (High)
問題: SQLインジェクション脆弱性
推奨対応: 入力サニタイズの実装
ブロッキングタスク: BLOCK-0001 を作成済み
承認をお願いします。"
```

#### 4.0b バジェット緊急超過

```
[coordinator → guardian]
"[BUDGET EMERGENCY] セッションバジェットが150%を超過しました。
上限: 500,000 トークン
現在: 780,000 トークン (156%)
残タスク: 3件
対応選択肢:
A) バジェット追加 (+300,000)
B) 残タスクを次回セッションに延期
C) 現状で終了
承認をお願いします。"
```

#### 4.0c GOAL.md / CLAUDE.md 変更

```
[coordinator → guardian]
"[CONSTITUTION CHANGE] CLAUDE.md の変更を提案します。
変更箇所: セクション 3 権限モデル
変更内容: [具体的な差分]
理由: [変更が必要な理由]
承認をお願いします。"
```

---

### executor → coordinator へのエスカレーション

以下の場合、executor は coordinator にエスカレーションする義務があります：

#### 4.1 スコープ変更が必要な場合

```
[executor → coordinator]
SendMessage:
"T-XXXX の実装中に、スコープ外のファイル（src/utils/helper.ts）の
変更が必要になりました。
理由: [具体的な理由]
提案: [最小限の変更内容]
スコープ拡張を承認していただけますか？"
```

coordinator の対応:
1. 影響範囲を評価する
2. 他メンバーの所有権と競合しないか確認する
3. 承認する場合: `TaskUpdate` でスコープを更新し、承認を返す
4. 却下する場合: 代替アプローチを提案するか、別タスクを作成する

#### 4.2 契約変更が必要な場合

```
[executor → coordinator]
SendMessage:
"contract-001 の API レスポンス形式を変更する必要があります。
理由: [技術的な理由]
現在の定義: { status: number, data: T }
変更提案: { status: number, data: T, metadata: { ... } }
この変更は T-YYYY にも影響します。"
```

coordinator の対応:
1. 影響を受ける全タスクを確認する
2. 関連メンバーに通知する
3. 契約ステータスを `proposed` に戻す
4. 合意形成後に `agreed` に更新する

#### 4.3 バジェット超過が見込まれる場合

```
[executor → coordinator]
SendMessage:
"T-XXXX のバジェット（100,000 トークン）に対し、現在 85,000 トークンを
使用しています。残りの作業に約 30,000 トークンが必要です。
選択肢:
A) バジェットを追加（+30,000）
B) 残りの作業を別タスクに分割
C) 優先度の低い部分をスキップ"
```

#### 4.4 ブロッキング問題が発生した場合

```
[executor → coordinator]
SendMessage:
"T-XXXX がブロックされています。
原因: T-YYYY（依存タスク）が未完了
影響: 他の依存タスクにも波及する可能性あり
推奨対応: T-YYYY の優先度を上げる"
```

### monitor → coordinator へのエスカレーション

#### 4.5 Critical/High セキュリティ問題

```
[monitor → coordinator]
SendMessage:
"[CRITICAL] セキュリティ脆弱性を検出しました。
ファイル: src/auth/login.ts:42
問題: ユーザー入力がサニタイズされずに SQL クエリに挿入されている
ブロッキングタスク: BLOCK-0001 を作成済み
推奨: T-XXXX のマージをブロックしてください"
```

---

## 5. CLIエージェントタイプとの対応

CLIモード（Agent Teams）では、各権限層がエージェントタイプに対応します：

| 権限層 | CLIエージェントタイプ | 特徴 |
|---|---|---|
| coordinator | Leader (Delegate Mode) | Edit/Write 禁止、調整に専念 |
| executor | Member (Plan → Standard) | 承認後に実装可能 |
| monitor | Member (Plan Mode 固定) | 読取専用、ブロッキングタスク作成可 |

### エージェント定義ファイル

各エージェントタイプの詳細な定義は以下のファイルに記載されています：

- `.claude/agents/coordinator.md` — coordinator の動作定義
- `.claude/agents/executor.md` — executor の動作定義
- `.claude/agents/monitor.md` — monitor の動作定義

---

## 6. GitHubジョブタイプとの対応

GitHubモード（GitHub Actions）では、権限層がジョブタイプに対応します：

| 権限層 | GitHubジョブタイプ | ワークフロー |
|---|---|---|
| coordinator | orchestrator / planner | `shiki-plan.yml` |
| executor | worker | `shiki-codex-worker.yml` |
| monitor | reviewer | `shiki-review.yml` |

### CLI と GitHub の対応関係

```
coordinator → cli: Leader (Delegate)    ⇔  github: orchestrator job
executor    → cli: Member (Plan→Std)    ⇔  github: worker job
monitor     → cli: Member (Plan only)   ⇔  github: reviewer job
```

各モードで異なるツールを使いますが、権限の意味（誰が何をできるか）は統一されています。

---

## 7. 権限違反の防止と検出

### 防止メカニズム

#### CLIモード
1. **Delegate Mode**: coordinator の Edit/Write を Claude Code レベルで制限
2. **Plan Mode**: executor の起動時に Edit/Write を制限
3. **エージェント定義**: `.claude/agents/*.md` で許可/禁止ツールを明示

#### GitHubモード
1. **ジョブ分離**: ワークフロー内のジョブが独立して実行
2. **Worktree 分離**: タスクごとに別ブランチで作業
3. **CI チェック**: PRレビューで権限違反を検出

### 検出メカニズム

#### Hooks による検出
`.claude/hooks/` 内のフックスクリプトが以下を監視します：

- `pre_bash_guard.py` — 危険コマンドの実行をブロック
- `post_edit_quality.py` — 編集後の品質チェック
- `post_task_sync.py` — タスク状態の整合性チェック

#### monitor による検出

monitor エージェントが以下を監視します：

- executor のファイル所有権スコープ逸脱
- 未承認でのフェーズ進行
- 契約違反
- セキュリティ問題

### 権限違反の例と対処

#### 例 1: executor がスコープ外のファイルを編集しようとした

```
[問題] executor が src/core/database.ts を編集（スコープ: src/api/**）
[検出] post_edit_quality.py / monitor
[対処]
  1. 変更を差し戻す
  2. coordinator にエスカレーション
  3. スコープ拡張を申請するか、別タスクを作成する
```

#### 例 2: coordinator が直接コードを編集しようとした

```
[問題] coordinator（Delegate Mode）が Edit ツールを使用
[検出] Delegate Mode の制約により、ツール使用自体がブロックされる
[対処] 修正タスクを TaskCreate で作成し、executor に割り当てる
```

#### 例 3: monitor が実装ファイルを修正しようとした

```
[問題] monitor が Plan Mode で Write ツールを使用
[検出] Plan Mode の制約により、ツール使用自体がブロックされる
[対処] ブロッキングタスクを TaskCreate で作成し、coordinator に報告する
```

#### 例 4: executor が承認なしで Standard Mode に移行

```
[問題] executor が Plan を提出せずに Edit を開始
[検出] Plan Mode の制約により、Edit がブロックされる
[対処] Plan を作成し、coordinator に提出する
```

---

## 付録: roles.yaml との対応表

`roles/roles.yaml` で定義されている各ロールの権限層対応：

| ロール | 権限層 | CLI タイプ | GitHub タイプ |
|---|---|---|---|
| **Guardian** | **guardian** | **人間** | **人間** |
| TeamLeader | coordinator | coordinator | orchestrator |
| GoalClarifier | coordinator | coordinator | planner |
| Architect | coordinator | coordinator | planner |
| Implementer | executor | executor | worker |
| QA | executor | executor | worker |
| SecurityReviewer | monitor | monitor | reviewer |
| ReleaseCaptain | coordinator | coordinator | integrator |

> **Note**: Guardian は人間のみが担当します。AIエージェントには割り当てられません。
> 詳細は `.github/GUARDIAN.md` を参照してください。
