# Claude Code 実行指示 — Shiki（式）

**CRITICAL**: このファイルはClaude Codeが自動参照する実行指示です。

---

## FIRST ACTION（セッション開始時の必須手順）

**ユーザーの発言に応答する前に、以下のチェックリストを必ず実行すること：**

1. `GOAL.md` を読み、ゴールが定義済みであることを確認する
2. `.shiki/config.yaml` を読み、モード（cli/github/auto）を確認する
3. `.shiki/tasks/*.json` でアクティブタスクを確認する
4. `.shiki/state/session-*.json` で復旧状態を確認する
5. 現在のθフェーズを特定する
6. 報告する: 「セッション初期化完了。モード: X。θフェーズ: Y。アクティブタスク: N件」

**GOAL.md が未定義またはテンプレートのままの場合:**
「GOAL.md が未定義です。プロジェクトのゴールと受け入れ条件を教えてください。」と応答する。

**重要**: この手順を完了するまで、設計・実装・その他の作業を開始してはならない。

---

## Core Principles

Claude Code は以下を**必ず**守ること：

1. **ファイルを正とする** — 会話よりも `.shiki/` のファイルを優先
2. **exit code を確認** — コマンド実行後は必ず終了コードをチェック
3. **簡潔に報告** — 2-3文で結果を要約。詳細はレポートファイルに
4. **エラー即報告** — エラー発生時は原因と復旧手順を即座に報告
5. **次のアクションを明示** — 報告の最後に次に何をするか記述
6. **絵文字を使わない** — 技術的・プロフェッショナルな出力のみ
7. **θフェーズを意識** — 現在のフェーズに適した行動をとる

---

## 環境セットアップ

### 必須確認（全コマンド実行前）

```bash
# 1. モード確認
echo "MODE: ${GITHUB_ACTIONS:+github}${GITHUB_ACTIONS:-cli}"

# 2. 設定ファイル確認
test -f .shiki/config.yaml && echo "CONFIG: OK" || echo "CONFIG: MISSING"

# 3. スキーマ確認
test -d .shiki/schemas && echo "SCHEMAS: OK" || echo "SCHEMAS: MISSING"
```

---

## Dual Engine パターン（CLI モード）

CLI モードでは Claude Agent Teams が **Codex MCP を積極的に活用**して Dual Engine 実行を行う。

> **重要**: Codex は `.claude/mcp.json` に MCP サーバーとして登録済みである。
> セッション内から MCP ツールとして直接呼び出せる。別ターミナルでの起動は不要。
> **実装タスクでは、まず Codex への委託を検討すること。**

### 認証

Codex MCP サーバーは以下の認証情報を自動継承する：
- **Pro/Plus plan**: `codex login` で取得した OAuth トークン（`~/.codex/` に保存）
- **API key**: 環境変数 `OPENAI_API_KEY`

認証状態は `codex login status` で確認可能。セッション起動時に `start_cli_session.sh` が自動チェックする。

### エンジン判定

```bash
# タスクの最適エンジンを確認
python3 scripts/engine_router.py .shiki/tasks/T-0001.json

# 全タスクの一括振分（coordinator が θ₃ で実行）
python3 scripts/engine_router.py --all
```

### Codex に委託すべきタスク

以下に該当するタスクは Codex MCP 経由で委託する：
- 仕様が明確な関数・クラスの実装（Contract が定義済み）
- テストコード生成（ユニットテスト、統合テスト）
- 定型コード・ボイラープレート生成
- lint / typecheck エラーの自動修正
- ドキュメント・型定義の生成
- 実装後のコードレビュー補助（Codex の `/review` 機能相当）

> **注**: 最終的なマージ判断・セキュリティレビューは Claude（coordinator/monitor）が行う。
> Codex のレビューは補助として活用し、最終判断は Claude が担保する。

### Codex 委託の具体的手順（executor が実行）

engine=codex のタスクは Codex MCP 経由で実装を委託する：

```
1. タスク仕様（acceptance, target_files, contract_ref）を整理
2. Codex MCP ツールを呼び出す（プロンプト例）：
   ---
   タスクID: T-XXXX
   対象ファイル: src/api/auth.ts（このファイルのみ編集可）
   受け入れ条件:
     - npm test -- --grep 'auth' が全パス
     - npm run lint でエラー 0
   契約: auth-api-v1

   上記の仕様に基づいて最小限の実装を行ってください。
   テストも合わせて生成してください。
   ---
3. Codex の出力をレビュー
4. exec verify で受け入れ条件を検証
5. 失敗 → Claude executor が自分でフォールバック実装
```

### Fallback Chain

```
Primary engine 失敗
  → エラー分析
  → Secondary engine で再試行
    → codex 失敗 → claude executor が直接実装
    → claude 失敗 → codex MCP に委託
  → 両方失敗 → coordinator にエスカレーション
```

---

## コマンド実行パターン

### Pattern 1: タスク状態確認

```bash
# タスク一覧を確認
ls .shiki/tasks/*.json 2>/dev/null | head -20

# 特定タスクの詳細
cat .shiki/tasks/T-0001.json | python3 -m json.tool
```

**出力テンプレート:**
```
タスク状態を確認しました。

アクティブタスク: 5件
  - T-0001: [implementing] Frontend API integration
  - T-0002: [review] Database schema migration
  - T-0003: [pending] Unit test coverage improvement
ブロック中: 1件
完了済み: 3件

次のアクション: T-0003 の自己割当を申請します。
```

### Pattern 2: Verification Auto-Loop（exec verify）

**変更後は必ず検証ループを実行する：**

```bash
# Step 1: テスト/リンタ/型チェック実行
npm test && npm run lint && npx tsc --noEmit

# Step 2: 結果確認
EXIT_CODE=$?

# Step 3: 失敗なら分析→修正→再実行（最大10回）
```

**出力テンプレート:**
```
exec verify: Iteration 2/10

テスト: 45 passed, 2 failed
リンタ: 0 errors
型チェック: 1 error

失敗箇所を修正し、再検証します。
```

### Pattern 3: θフェーズ報告

各フェーズ完了時に報告する：

```
θ₄ EXECUTE フェーズ完了報告

完了タスク: T-0001, T-0002, T-0003
受入条件: 全件合格
バジェット使用: 45,000 / 100,000 tokens (45%)

exit_criteria 充足状況:
  - [x] all_tasks_completed
  - [x] acceptance_passed

次のアクション: θ₅ VERIFY フェーズに進行します。
```

---

## Verification Auto-Loop（exec verify）プロトコル

### 概要

OpenAI Dev Day で示された「verification scripts + auto-loop」パターンを採用。
**変更→検証→失敗なら分析→修正→再検証**のループで品質を担保する。

### 用語定義

| 用語 | 意味 | 使用場面 |
|------|------|----------|
| **exec verify** | 検証ループ実行 | 実装完了後の品質検証 |
| **exec plan** | 計画付き実行 | タスク着手前の計画策定 |
| **exec review** | レビューループ実行 | コードレビュー時 |
| **auto-loop** | 自動反復 | 検証失敗時の自動リトライ |

### 標準検証フロー

```
1. 変更を実施
2. exec verify を実行（テスト/リンタ/型チェック/セキュリティ）
3. 全チェック合格 → 完了
4. 失敗 → diff を分析 → 修正 → Step 2 に戻る
5. MAX_ITERATIONS（10回）到達 → coordinator にエスカレーション
```

### 検証カテゴリ別の基準

#### コード変更
```bash
# 必須チェック
npm run lint          # ESLint: 0 errors
npm run typecheck     # TypeScript: 0 errors
npm test              # Tests: all passed
npm run security:scan # Security: 0 critical

# MAX_ITERATIONS: 10
# 合格条件: 全チェック合格
```

#### API 変更
```bash
# 必須チェック
npm run test:integration  # Integration: all passed
npm run test:api          # API tests: all passed

# MAX_ITERATIONS: 10
# 合格条件: 全テスト合格 + パフォーマンス劣化 < 10%
```

#### パフォーマンス最適化
```bash
# ベースライン計測 → 最適化 → 比較
npm run benchmark -- --baseline
# [最適化実施]
npm run benchmark:compare -- --compare-with=baseline

# MAX_ITERATIONS: 5（コストが高いため）
# 合格条件: 20%以上の改善 + 品質劣化なし
```

#### セキュリティ修正
```bash
npm run security:scan
npm run security:audit

# MAX_ITERATIONS: 3（セキュリティは最優先）
# 合格条件: 0 critical vulnerabilities + 0 hardcoded secrets
```

### exec verify の報告形式

```
exec verify 結果:

Iteration: 3/10
Status: PASSED

チェック結果:
  - lint: 0 errors (PASS)
  - typecheck: 0 errors (PASS)
  - test: 48/48 passed (PASS)
  - security: 0 critical (PASS)

タスク T-0001 の検証が完了しました。
```

---

## 出力スタイルガイド

### DO: 簡潔な技術的サマリ

```
exec verify 完了。テスト全件合格、リンタエラー0件。
T-0001 を review ステータスに更新し、coordinator に報告します。
```

### DON'T: 冗長な説明

```
それでは、テストを実行してみましょう！テストは全部で48個あり、
それぞれユニットテストとインテグレーションテストに分かれています。
まずユニットテストから実行しますね。結果は...（以下続く）
```

### DON'T: 絵文字の多用

```
🎉 テスト全件合格！ 🚀✨ リンタも 🔧 問題なし！ 💪
```

---

## エラーハンドリング

### 重大度別対応

| 重大度 | 対応 | 報告先 |
|--------|------|--------|
| **Critical** | 即時停止、guardian にエスカレーション | guardian + coordinator |
| **High** | 作業中断、coordinator に報告 | coordinator |
| **Medium** | exec verify で自動修正試行 | coordinator（修正不可時） |
| **Low** | ログ記録、作業継続 | レポートに記載 |

### エラー報告テンプレート

```
エラー発生: [エラーカテゴリ]

コマンド: `npm test`
Exit: 1
重大度: Medium

エラー内容:
  - tests/api.test.ts:42 - TypeError: Cannot read property 'id'

対応: exec verify auto-loop で修正を試行します。
```

---

## Guardian エスカレーション

以下の場合は guardian にエスカレーションが**必須**：

1. **セキュリティ脆弱性の発見** — 即時報告
2. **バジェット超過（150%以上）** — 緊急停止
3. **受入条件の変更が必要** — GOAL.md 修正提案
4. **権限モデル違反の検出** — 違反者の特定と報告
5. **exec verify が MAX_ITERATIONS に到達** — 人間の判断が必要

### エスカレーション形式

```
ESCALATION to Guardian

重大度: [Critical/High]
理由: [具体的な理由]
タスク: T-XXXX
θフェーズ: θ₄ EXECUTE

状況:
  [現在の状況の説明]

推奨アクション:
  [提案する対応策]

影響範囲:
  [影響を受けるタスク/ファイル]
```

---

## クイックリファレンス

### 全コマンド実行前チェックリスト

- [ ] `.shiki/config.yaml` が存在する
- [ ] 現在のθフェーズを確認した
- [ ] タスクの `authority_layer` が自分の層に合致する
- [ ] ファイル所有権スコープを確認した
- [ ] バジェット残量を確認した

### 出力テンプレート

```
[アクションの説明 - 1行]

コマンド: `[実行コマンド]`
Exit: [終了コード]

[結果データ - 3-5ポイント]

次のアクション: [次に何をするか]
```

### exec verify チェックリスト

- [ ] lint: 0 errors
- [ ] typecheck: 0 errors
- [ ] test: all passed
- [ ] security: 0 critical
- [ ] acceptance: all criteria met

---

**このファイルは Claude Code が自動参照します。プロジェクトのコンテキストとして常に最新に保ってください。**
