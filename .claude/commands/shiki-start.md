# Shiki — Session Start

Shiki（式）フレームワークの開発セッションを開始します。
以下の手順を**省略せずに全て実行**してください。

## 1. フレームワーク確認

```bash
test -f .shiki/config.yaml && echo "OK" || echo "ERROR: .shiki/config.yaml not found"
test -f CLAUDE.md && echo "OK" || echo "ERROR: CLAUDE.md not found"
test -f GOAL.md && echo "OK" || echo "ERROR: GOAL.md not found"
```

いずれかが見つからない場合、「このディレクトリは Shiki プロジェクトではありません。`/shiki` で新規作成してください。」と案内して停止。

## 2. CLAUDE.md を読む

CLAUDE.md を全文読み、憲法（Constitution）のルールを把握してください。
特に以下のセクションを理解すること：
- MANDATORY: Session Start Protocol
- θ収束モデル（6フェーズ）
- Section 5: Agent Teams 必須構成とエンジン割当ルール
- Section 5a: Dual Engine Architecture（Codex MCP の使い方）

## 3. GOAL.md を読む

GOAL.md を読み、以下を把握してください：
- プロジェクトのゴール
- 受け入れ条件
- スコープ（やる / やらない）
- 制約

GOAL.md がテンプレートのまま（「（例）」を含む）の場合、ユーザーにゴール定義を依頼してください。

## 4. 現在の状態を確認

以下を確認してください：
- `.shiki/config.yaml` — モード（cli / github / auto）
- `.shiki/state/session-*.json` — 復旧セッションの有無
- `.shiki/tasks/*.json` — アクティブなタスクの状態
- `.shiki/plans/PLAN.md` — 既存の設計計画

## 5. Agent Teams を作成する（必須）

**GOAL に基づいて `TeamCreate` で即座にチームを作成してください。**

エンジン割当ルール（厳守）：
- **Codex に委託**: 関数実装、テスト生成、コードレビュー、定型コード、CI修復
- **CC が担当**: 設計判断、デバッグ、リファクタリング、計画、マージ判断

GOAL の内容に応じて必要な役割（implementer, tester, reviewer, architect, debugger 等）を判断し、
それぞれを Codex / CC に割り振ってチームを編成してください。

## 6. セッション開始報告

以下の形式で状態を報告してください：

```
セッション初期化完了。
  モード: [cli/github/auto]
  θフェーズ: [現在のフェーズ、初回は theta_1_understand]
  チーム: [チーム名] — [N]名（CC: N名、Codex: N名）
  アクティブタスク: [N] 件
  GOAL: [ゴールの1行要約]
  Codex MCP: [利用可能/利用不可]
  復旧セッション: [あり/なし]
```

## 7. θ₁ UNDERSTAND を開始

復旧セッションがない場合は θ₁ UNDERSTAND から開始してください：
1. ゴールと受け入れ条件を再確認
2. 不明点やリスクを列挙
3. スコープの確認

復旧セッションがある場合は、前回の θ フェーズから再開を提案してください。

**重要**: この手順が完了するまで、いかなる実装コードも書かないでください。
