# Shiki（式） ベストプラクティス

実プロジェクトで Shiki（式） を最大限に活用するための実践ガイド。
初回導入から本番運用まで、段階的に習得できるように構成しています。

---

## 目次

1. [導入の3ステップ](#1-導入の3ステップ)
2. [GOAL.md の書き方](#2-goalmd-の書き方)
3. [タスク設計の黄金律](#3-タスク設計の黄金律)
4. [契約（Contract）の活用](#4-契約contractの活用)
5. [CLIモードの実践パターン](#5-cliモードの実践パターン)
6. [GitHubモードの実践パターン](#6-githubモードの実践パターン)
7. [exec verify を効かせるコツ](#7-exec-verify-を効かせるコツ)
8. [チーム編成のパターン](#8-チーム編成のパターン)
9. [バジェット管理](#9-バジェット管理)
10. [セッション復旧の備え](#10-セッション復旧の備え)
11. [段階的自動化ロードマップ](#11-段階的自動化ロードマップ)
12. [アンチパターン集](#12-アンチパターン集)
13. [プロジェクト規模別ガイド](#13-プロジェクト規模別ガイド)
14. [トラブルシューティング](#14-トラブルシューティング)
15. [チェックリスト](#15-チェックリスト)

---

## 1. 導入の3ステップ

### Step 1: 最小構成で動かす（30分）

初回は全機能を有効にしない。まず以下だけで1タスクを完了させる。

```bash
# 初期化
./scripts/init_shiki.sh

# GOAL.md を書く
cp templates/GOAL.template.md GOAL.md
# → 自分のゴールを記入

# バリデーション
python3 scripts/validate_shiki.py
```

このステップでは CLIモード + 単一エージェント（チームなし）で十分。
θフェーズ、DAG、Contract は使わなくてよい。

### Step 2: チームで回す（2時間）

Step 1 の成功体験を得たら、Agent Teams を導入する。

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
./scripts/start_cli_session.sh
```

- coordinator 1名 + executor 1名 の **最小チーム** で始める
- Delegate Mode（coordinator が Edit/Write しない）を体験する
- Plan Mode → 承認 → Standard Mode の流れを確認する

### Step 3: 本格運用へ拡張

Step 2 で安定したら、以下を段階的に有効化する：

| 機能 | 有効化タイミング | 設定箇所 |
|------|----------------|----------|
| Contract | API 境界がある場合 | `.shiki/contracts/` |
| DAG | タスクが5つ以上ある場合 | `.shiki/dag/` |
| monitor | セキュリティ要件がある場合 | `.claude/agents/monitor.md` |
| GitHub Mode | CI/CD 統合が必要な場合 | `.github/workflows/` |
| LDD | ログ蓄積を始めたい場合 | `.shiki/config.yaml` |
| Guardian | 本番デプロイがある場合 | `.github/GUARDIAN.md` |

---

## 2. GOAL.md の書き方

GOAL.md の品質が開発全体の成否を決める。

### 良い GOAL.md の条件

```markdown
## 1. 何を達成したいか
ユーザーがメール+パスワードでログインし、JWT トークンを取得できる。

## 2. 受け入れ条件
- [ ] POST /api/auth/login でJWTが返る
- [ ] 不正パスワードで401が返る
- [ ] トークン有効期限は24時間
- [ ] npm test が全パスする
- [ ] npm run lint がエラーゼロ

## 3. スコープ
### やる
- 認証API（login/logout）
- JWT ミドルウェア
- ユニットテスト

### やらない
- SNSログイン（OAuth）
- パスワードリセット
- フロントエンド

## 4. 制約
- 新規依存: bcrypt, jsonwebtoken のみ許可
- DB: 既存の PostgreSQL を使用
- 期限: なし
- トークンバジェット: 300,000
```

### GOAL.md のチェックポイント

| チェック | 悪い例 | 良い例 |
|---------|--------|--------|
| 具体性 | 「認証機能を作る」 | 「POST /api/auth/login でJWTを返す」 |
| 測定可能性 | 「セキュアにする」 | 「OWASP Top 10 の該当項目をクリア」 |
| スコープ境界 | 「やらない」欄が空 | 「SNSログイン、パスワードリセットはやらない」 |
| 受け入れ条件 | 「テストが通る」 | 「npm test 全パス + カバレッジ80%以上」 |
| 制約 | 記述なし | 「新規依存は bcrypt, jsonwebtoken のみ」 |

### よくある失敗

- **曖昧なゴール**: 「パフォーマンスを改善する」→ AIが独自解釈して暴走
- **スコープ未定義**: 「認証を実装」→ OAuth, MFA, SSO まで勝手に拡張
- **受け入れ条件なし**: 完了判定ができず、無限に修正が続く

---

## 3. タスク設計の黄金律

### 適切な粒度

```
1プロジェクト = 5〜15タスク
1タスク = 5,000〜100,000 トークン（推定）
1タスク = 1〜3ファイルの変更
```

**粗すぎ**（1タスクで全実装）→ 失敗時のリカバリが困難、並列化不可
**細かすぎ**（50タスク）→ 調整コストが実装コストを上回る

### タスク JSON の書き方

```json
{
  "id": "T-0002",
  "title": "JWT認証ミドルウェアの実装",
  "assigned_to": "claude-member",
  "status": "pending",
  "priority": "high",
  "authority_layer": "executor",
  "theta_phase": "execute",
  "depends_on": ["T-0001"],
  "context": {
    "target_files": ["src/middleware/auth.ts"],
    "contract_ref": "auth-middleware-v1",
    "ownership": {
      "include": ["src/middleware/"],
      "exclude": []
    }
  },
  "acceptance": [
    "npm test -- --grep 'auth middleware'",
    "npm run lint"
  ],
  "budget": {
    "estimated_tokens": 30000,
    "max_tokens": 50000
  }
}
```

### タスク設計のルール

1. **1タスク1責務**: 「APIとテストとドキュメント」は3タスクに分ける
2. **受け入れ条件は実行可能なコマンド**: `npm test`、`curl ...`、`npx tsc --noEmit`
3. **ファイル所有権を明示**: `context.ownership` で編集範囲を限定
4. **依存関係を正直に書く**: `depends_on` をサボると並列実行時に壊れる
5. **バジェットを設定する**: 無制限にすると暴走する

---

## 4. 契約（Contract）の活用

### いつ Contract を使うか

- 2つ以上のタスクが同じ API/型/データ構造を共有する場合
- フロントエンドとバックエンドが並列開発する場合
- 外部サービスとの連携仕様を固定したい場合

### Contract の書き方

```json
{
  "contract_id": "auth-api-v1",
  "version": "1.0.0",
  "status": "agreed",
  "defined_by": "architect",
  "implemented_by": ["backend-dev"],
  "interface": {
    "type": "openapi",
    "path": "POST /api/auth/login",
    "request": {
      "body": {
        "email": "string",
        "password": "string"
      }
    },
    "response": {
      "200": { "token": "string", "expires_in": "number" },
      "401": { "error": "string" }
    }
  },
  "tests_path": "tests/contracts/auth-api.test.ts"
}
```

### Contract の状態遷移

```
proposed → agreed → implemented → verified → integrated
```

- **proposed**: 設計者が提案した段階。レビュー中
- **agreed**: 関係者全員が合意。この時点で interface を変更するには再提案が必要
- **implemented**: 実装完了。テストは未実行
- **verified**: テストパス。契約通りに動作することを確認
- **integrated**: メインブランチにマージ済み

### Contract を変更したくなったら

```
executor → coordinator:
「contract auth-api-v1 のレスポンスに user_id を追加したい。
理由: フロントエンドで必要。影響タスク: T-0003, T-0005。
承認をお願いします。」
```

**絶対にやってはいけないこと**: Contract を黙って変更する → 並列タスクが壊れる

---

## 5. CLIモードの実践パターン

### 基本的なセッション開始

```bash
# 環境準備
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1

# セッション起動
./scripts/start_cli_session.sh
```

### 推奨 tmux レイアウト

```
┌───────────────────────┬───────────────────────┐
│                       │                       │
│  coordinator          │  executor-1           │
│  (Delegate Mode)      │  (Plan → Standard)    │
│                       │                       │
│  設計・承認・調整    　  │  実装・テスト           │
│                       ├───────────────────────┤
│                       │                       │
│                       │  monitor              │
│                       │  (Read-only)          │
│                       │  レビュー・品質監視      │
└───────────────────────┴───────────────────────┘
```

### coordinator の典型的な指示フロー

```
1. GOAL.md を読む
2. 設計方針を決定し、PLAN.md を作成
3. TeamCreate でチームを編成
4. TaskCreate でタスクを割り当て
5. executor が Plan を提出 → 内容を確認
6. ExitPlanMode で承認（Standard Mode に移行）
7. 進捗を TaskList で確認
8. 完了報告を受けてレビュー
9. θフェーズの exit_criteria を評価
```

### executor の典型的な作業フロー

```
1. [Plan Mode] タスクの context と acceptance を確認
2. [Plan Mode] 対象ファイルを Read で調査
3. [Plan Mode] 実行計画を作成し coordinator に提出
4. --- coordinator の承認待ち ---
5. [Standard Mode] ファイル所有権スコープ内で実装
6. [Standard Mode] exec verify ループを実行
7. [Standard Mode] レポートを作成し TaskUpdate で完了報告
```

### Self-Claim（自己割当）の使い方

`config.yaml` で `self_claim: true` の場合：

```
executor → coordinator:
「T-0003 の自己割当を申請します。
理由: 自分のスコープ（src/api/）に合致し、依存タスク T-0001 が完了済みです。」
```

coordinator が承認すると、executor はそのタスクを着手できる。
**承認なしに着手してはいけない**。

---

## 6. GitHubモードの実践パターン

### Issue からの開発フロー

```
1. Issue を作成（テンプレート: .github/ISSUE_TEMPLATE/shiki_goal.md）
2. ai-goal ラベルを付与
3. shiki-orchestrator.yml が自動起動
   → θ₁ UNDERSTAND: ゴール分析
   → θ₂ GENERATE: 計画・タスク分解
   → θ₃ ALLOCATE: DAG 生成
4. shiki-dag-executor.yml が DAG を実行
   → バッチごとに worktree を作成
   → 各 worktree で Claude/Codex がタスク実行
5. PR が自動作成される
6. shiki-review.yml で自動レビュー
7. 人間が最終確認してマージ
```

### ラベルの活用

53ラベル全てを最初から使う必要はない。以下の最小セットから始める：

| カテゴリ | 最小ラベル | 用途 |
|---------|-----------|------|
| priority | `P1-High`, `P2-Medium` | 優先度 |
| status | `pending`, `in_progress`, `done` | 状態追跡 |
| type | `feature`, `bug`, `docs` | 分類 |
| trigger | `ai-goal` | 自動化トリガー |

### worktree の管理

```bash
# worktree 一覧
git worktree list

# 手動クリーンアップ
git worktree prune

# worktree マネージャー（推奨）
python3 scripts/worktree_manager.py --cleanup
```

**注意**: worktree を手動で `rm -rf` しない。`git worktree remove` を使う。

---

## 7. exec verify を効かせるコツ

### 基本ループ

```
変更 → lint → typecheck → test → security
  ↑                                    |
  └─── 失敗したら分析・修正して再試行 ──┘
       （最大10回。超えたら guardian へ）
```

### 効果的な acceptance の書き方

**悪い例**（曖昧で自動検証不可能）:
```json
"acceptance": ["テストが通ること", "コードが綺麗であること"]
```

**良い例**（具体的で実行可能）:
```json
"acceptance": [
  "npm test -- --grep 'auth' --bail",
  "npm run lint -- --max-warnings=0",
  "npx tsc --noEmit",
  "curl -s http://localhost:3000/health | jq -e '.status==\"ok\"'"
]
```

### MAX_ITERATIONS の調整

| チェック種別 | 推奨 MAX | 理由 |
|------------|---------|------|
| lint + typecheck | 10 | 修正が小さく収束しやすい |
| ユニットテスト | 10 | 反復で修正可能 |
| パフォーマンス | 5 | コストが高い、根本設計の問題の可能性 |
| セキュリティ | 3 | 反復で直らないなら設計見直しが必要 |

### exec verify が収束しない場合

原因の80%は以下のどれか：

1. **受け入れ条件が矛盾している**: 2つの条件が両立不可能
2. **スコープ外の問題**: 他タスクの変更が必要
3. **設計の問題**: 反復修正ではなく、θ₂ に戻って再設計が必要
4. **環境の問題**: テスト環境が壊れている

→ いずれも guardian にエスカレーションするのが正しい対応。

---

## 8. チーム編成のパターン

### パターン A: 最小構成（2名）

```
coordinator (1) + executor (1)
```

- 適用: 小規模タスク（3〜5タスク）、単一ドメイン
- 利点: 調整コスト最小
- 注意: セキュリティレビューは coordinator が兼務

### パターン B: 標準構成（3名）

```
coordinator (1) + executor (1) + monitor (1)
```

- 適用: 中規模タスク（5〜10タスク）、セキュリティ要件あり
- 利点: 実装と品質チェックの分離
- 注意: executor は1名なので並列実装はできない

### パターン C: 並列構成（4名）

```
coordinator (1) + executor (2) + monitor (1)
```

- 適用: 大規模タスク（10〜15タスク）、複数ドメイン
- 利点: フロントエンド/バックエンドの並列開発
- 注意: ファイル所有権の分離を厳密にする

### パターン D: フル構成（5名）

```
coordinator (1) + executor (2) + monitor (1) + executor-qa (1)
```

- 適用: 大規模 + 品質要件が高い
- 利点: 実装・テスト・レビューの完全分離
- 注意: 調整コストが高い。config.yaml の `max_team_size: 5` が上限

### ファイル所有権の分割例

```yaml
executor-frontend:
  ownership:
    include: ["src/components/", "src/pages/", "src/styles/"]
    exclude: ["src/components/shared/"]

executor-backend:
  ownership:
    include: ["src/api/", "src/middleware/", "src/db/"]
    exclude: []

# 共有ファイル（src/components/shared/）は contract で合意してから
# coordinator が一方に編集権限を付与する
```

---

## 9. バジェット管理

### バジェットの設定指針

| プロジェクト規模 | タスク当たり | セッション全体 |
|----------------|-------------|--------------|
| 小（3〜5タスク） | 30,000 | 200,000 |
| 中（5〜10タスク） | 50,000 | 500,000 |
| 大（10〜15タスク） | 100,000 | 1,000,000 |

### 警告と緊急停止

```yaml
# .shiki/config.yaml
github:
  budget:
    max_tokens_per_task: 100000
    max_tokens_per_session: 500000
    warn_threshold_pct: 80      # 80% で警告
guardian:
  budget_emergency_threshold_pct: 150  # 150% で緊急停止
```

- **80%到達**: coordinator に警告。残タスクの優先度を再評価
- **100%到達**: coordinator が判断（追加 or 分割 or 中止）
- **150%到達**: guardian にエスカレーション。全作業を一時停止

### バジェットを節約するコツ

1. **タスクの粒度を適切に**: 粗すぎるタスクは手戻りでトークンを浪費する
2. **Plan Mode を活用**: 計画段階でよく考えてから実装に入る
3. **exec verify の MAX_ITERATIONS を適切に**: 3回失敗したら設計を見直す
4. **Codex を活用**: 明確な実装タスクは Codex に委託（トークン効率が良い）
5. **Contract を早期に合意**: 後から変更するとやり直しが発生する

---

## 10. セッション復旧の備え

### 日常的にやっておくべきこと

1. **`.shiki/` を Git にコミットする**: タスク状態・契約・計画が残る
2. **LDD を有効にする**: `.ai/logs/` にセッションのコンテキストが記録される
3. **session state を確認する**: `on_stop_report.py` が自動で `.shiki/state/` に保存

### 復旧手順

```bash
# 自動復旧（推奨）
./scripts/start_cli_session.sh --resume

# 手動復旧
python3 scripts/recover_session.py
```

### 復旧時に確認すべきこと

```
1. 最新の .shiki/state/session-*.json を読む
   → θフェーズ、未完了タスク、バジェット残を確認

2. .shiki/tasks/*.json を確認
   → status が in_progress のタスクを特定
   → claimed_by を確認（前セッションの担当者）

3. .ai/logs/ の最新ログを読む
   → codex_prompt_chain から意図と計画を復元
   → tool_invocations から進捗を確認

4. git status で未コミットの変更を確認
   → 中途半端な変更がないか
```

### 復旧できないもの

- Agent Teams の会話履歴（揮発性）
- tmux ペインの内容
- メモリ上の一時変数

→ だからこそファイル（`.shiki/`、`.ai/logs/`）に状態を書き出すことが重要。

---

## 11. 段階的自動化ロードマップ

一気に全自動化しない。段階的に信頼を積み上げる。

### Level 0: 完全手動

```
人間がゴール設定 → 人間が設計 → AIが実装 → 人間がレビュー → 人間がマージ
```

- 最初はここから。AIの出力を100%人間がレビュー
- Guardian が全承認

### Level 1: 設計自動化

```
人間がゴール設定 → AIが設計提案 → 人間が承認 → AIが実装 → 人間がレビュー
```

- θ₁+θ₂ を AI に任せる
- PLAN.md を人間がレビューして承認

### Level 2: 実装+検証自動化

```
人間がゴール設定 → AIが設計+実装+exec verify → 人間がレビュー → 人間がマージ
```

- θ₁〜θ₅ を AI が自律的に実行
- 人間は最終レビュー（θ₅）とマージ（θ₆）のみ

### Level 3: 条件付き自動マージ

```
人間がゴール設定 → AIが全工程実行 → CI全パス+レビュー済み → 自動マージ
```

- ドキュメント、テスト、フォーマット変更のみ自動マージ許可
- 本番コード変更は人間レビュー必須

### Level 4: Issue 駆動の完全自動化

```
Issue作成 → AIが全工程実行 → 自動デプロイ（staging）→ 人間が本番承認
```

- GitHub Mode のフル活用
- staging デプロイまで自動
- 本番デプロイのみ guardian 承認

---

## 12. アンチパターン集

### AP-1: ゴールなき実装

```
悪い: 「とりあえずコードを書いて」
良い: 「GOAL.md を読んで、θ₁ UNDERSTAND から始めて」
```

GOAL.md なしで始めると、AIが独自解釈で暴走する。

### AP-2: coordinator が直接編集

```
悪い: coordinator が Edit/Write で直接コードを修正
良い: coordinator が TaskCreate で修正タスクを作り、executor に割り当てる
```

Delegate Mode の意味がなくなり、権限分離が崩壊する。

### AP-3: Contract なしの並列開発

```
悪い: executor-A と executor-B が口頭合意で同じ型を使う
良い: .shiki/contracts/ に interface を定義し、agreed にしてから実装開始
```

「言った言わない」で並列タスクが壊れる。

### AP-4: exec verify のスキップ

```
悪い: 「テストは後で書く」→ status を completed に
良い: acceptance の全条件をクリアしてから completed にする
```

品質ゲートを飛ばすと θ₅ VERIFY で全部やり直しになる。

### AP-5: バジェット無視

```
悪い: budget を設定せずに無制限にトークンを消費
良い: タスクごとに max_tokens を設定し、80%で警告を出す
```

コスト暴走の原因。

### AP-6: 巨大な単一タスク

```
悪い: T-0001「全機能を実装する」（推定500,000トークン）
良い: T-0001〜T-0010 に分解（各30,000〜50,000トークン）
```

失敗時のリカバリが不可能になる。

### AP-7: スコープ外の無断編集

```
悪い: executor が「ついでに」スコープ外のファイルを修正
良い: coordinator にスコープ拡張を申請し、承認を得てから編集
```

他の executor の作業と競合する。

### AP-8: θフェーズの飛ばし

```
悪い: θ₁ UNDERSTAND → いきなり θ₄ EXECUTE
良い: θ₁ → θ₂ → θ₃ → θ₄ の順に exit_criteria を満たしながら進行
```

計画なき実装は手戻りを生む。

### AP-9: ログなしの作業

```
悪い: LDD を無効にして作業 → セッションが切れて全コンテキスト消失
良い: LDD を有効にし、intent/plan/implementation/verification を記録
```

復旧不可能なセッション断絶の原因。

### AP-10: 全機能を初日から有効化

```
悪い: DAG + Worktree + 53ラベル + Guardian + MCP + 5名チーム を初日から
良い: 最小構成で成功 → 段階的に機能追加
```

複雑さに圧倒されて何も動かない。

---

## 13. プロジェクト規模別ガイド

### 小規模（1〜3時間、3〜5タスク）

```yaml
チーム構成: coordinator (1) + executor (1)
モード: CLI のみ
θフェーズ: θ₁→θ₂→θ₄→θ₆（θ₃ ALLOCATE と θ₅ VERIFY は簡略化）
Contract: 不要（単一担当者なので競合なし）
DAG: 不要（直列実行で十分）
Guardian: 不要（人間が直接操作）
LDD: 推奨（復旧に便利）
```

**実践例**: API エンドポイント1本の追加、バグ修正、ドキュメント更新

### 中規模（半日〜1日、5〜10タスク）

```yaml
チーム構成: coordinator (1) + executor (1〜2) + monitor (0〜1)
モード: CLI
θフェーズ: 全6フェーズ
Contract: API境界がある場合に使用
DAG: タスク間の依存関係を明示（直列+並列混在）
Guardian: 推奨（セキュリティ要件があれば必須）
LDD: 必須
```

**実践例**: 認証機能の実装、データベースマイグレーション、新画面の追加

### 大規模（数日、10〜15タスク）

```yaml
チーム構成: coordinator (1) + executor (2〜3) + monitor (1)
モード: CLI + GitHub 併用
θフェーズ: 全6フェーズ + 必要に応じて再反復
Contract: 必須（並列開発の境界を固定）
DAG: 必須（バッチ並列実行）
Guardian: 必須
LDD: 必須
バジェット管理: 厳密に追跡
```

**実践例**: マイクロサービスの新規追加、大規模リファクタリング、フルスタック機能開発

---

## 14. トラブルシューティング

### Q: executor が Plan Mode から出られない

**原因**: coordinator が ExitPlanMode を実行していない
**対処**: executor が SendMessage で Plan を coordinator に提出し、承認を待つ

### Q: exec verify が10回失敗して止まった

**原因**: 設計レベルの問題、または受け入れ条件の矛盾
**対処**:
1. エラーメッセージを分析
2. θ₂ GENERATE に戻って設計を見直す必要があるか判断
3. guardian にエスカレーション

### Q: 2つの executor の変更が競合した

**原因**: ファイル所有権の分割が不十分
**対処**:
1. 競合ファイルを特定
2. coordinator が所有権を再割当て
3. Contract で共有部分の interface を合意

### Q: セッションが切れて状態が分からない

**対処**:
1. `python3 scripts/recover_session.py` を実行
2. `.shiki/state/session-*.json` の最新を確認
3. `.ai/logs/` の当日ログを読む
4. `git log --oneline -20` で最近のコミットを確認

### Q: バジェットが足りない

**対処**:
1. 残タスクの優先度を再評価
2. 低優先タスクを次回セッションに延期
3. guardian にバジェット追加を申請
4. Codex への委託で効率化を検討

### Q: Contract が agreed 後に変更が必要になった

**対処**:
1. executor が coordinator に変更理由を報告
2. coordinator が影響範囲を評価（どのタスクに波及するか）
3. Contract の status を proposed に戻す
4. 関係者に通知し、再合意を取る

---

## 15. チェックリスト

### プロジェクト開始前

- [ ] GOAL.md が具体的な受け入れ条件と明確なスコープ境界を持っている
- [ ] `.shiki/config.yaml` のモードとバジェットが設定されている
- [ ] `python3 scripts/validate_shiki.py` がパスする
- [ ] チーム構成が決まっている（最小: coordinator + executor）

### θ₁ UNDERSTAND 完了時

- [ ] 受け入れ条件が全て testable（実行可能なコマンドで検証可能）
- [ ] スコープの「やらない」が明示されている
- [ ] 技術的制約が文書化されている

### θ₂ GENERATE 完了時

- [ ] `.shiki/plans/PLAN.md` が作成されている
- [ ] タスクが5〜15個に分解されている
- [ ] 各タスクに `acceptance` が設定されている
- [ ] API境界がある場合は Contract が proposed されている

### θ₃ ALLOCATE 完了時

- [ ] 全タスクに `assigned_to` と `authority_layer` が設定されている
- [ ] ファイル所有権が重複なく分割されている
- [ ] 依存関係（`depends_on`）が正しく設定されている
- [ ] Contract が agreed になっている

### θ₄ EXECUTE 中

- [ ] executor が Plan Mode で計画を提出している
- [ ] coordinator が承認してから Standard Mode に移行している
- [ ] exec verify を各タスク完了時に実行している
- [ ] バジェットを定期的に確認している

### θ₅ VERIFY 完了時

- [ ] 全タスクの acceptance がパスしている
- [ ] Contract が verified になっている
- [ ] セキュリティレビューが完了している（monitor による）
- [ ] レポートが `.shiki/reports/` に作成されている

### θ₆ INTEGRATE 完了時

- [ ] CI が全パスしている
- [ ] PR がマージされている
- [ ] GOAL.md の受け入れ条件が全て達成されている
- [ ] LDD ログに verification が記録されている

---

## 関連ドキュメント

- [コンセプト](./00_concepts.md) - 設計思想の詳細
- [ワークフロー](./01_workflow_goal_to_release.md) - Goal → Release の全体フロー
- [権限モデル](./12_authority_model.md) - 4層権限の詳細とエスカレーション手順
- [θ収束モデル](./13_convergence_model.md) - 6フェーズの exit_criteria 詳細
- [セッション復旧](./14_session_recovery.md) - 復旧手順の詳細
- [LDD プロトコル](./15_ldd_protocol.md) - ログ形式と活用方法
- [プロンプト集](./08_prompts_library.md) - コピペで使えるプロンプトテンプレート
- [Guardian](../.github/GUARDIAN.md) - 人間の最終権限の詳細
