# Shiki（式） 使い方ガイド

セットアップが完了した状態から、実際のプロジェクト開発を行う手順。

---

## 目次

1. [5分で始める最小フロー](#1-5分で始める最小フロー)
2. [CLIモード: チーム開発](#2-cliモード-チーム開発)
3. [GitHubモード: Issue駆動開発](#3-githubモード-issue駆動開発)
4. [タスクの作り方](#4-タスクの作り方)
5. [契約（Contract）の使い方](#5-契約contractの使い方)
6. [exec verify の使い方](#6-exec-verify-の使い方)
7. [セッション復旧](#7-セッション復旧)
8. [プロンプト集](#8-プロンプト集)
9. [よくある操作](#9-よくある操作)

---

## 1. 5分で始める最小フロー

チーム編成なし、単一エージェントで最も簡単に使う方法。

```bash
# 1. GOAL.md を書く
cp templates/GOAL.template.md GOAL.md
# → ゴール、受け入れ条件、スコープを記入

# 2. Claude Code を起動
claude

# 3. 以下を指示
```

```
GOAL.md を読んで、このリポジトリの AI 開発統括として作業してください。
まず Plan モードで設計を行い、その後タスクを分解して実装してください。
CLAUDE.md のルールに従ってください。
```

これだけで θ₁〜θ₆ のフローが走る。

---

## 2. CLIモード: チーム開発

### 起動

```bash
./scripts/start_cli_session.sh
```

### Step 1: ゴールの確認（θ₁ UNDERSTAND）

Leader ペインで以下を指示:

```
あなたはこのリポジトリの AI 開発統括（Team Leader）です。
まずは Plan モードで GOAL.md を読み、以下を行ってください:

1. ゴールと受け入れ条件を抽出
2. 不明点やリスクの列挙
3. スコープの確認（やる/やらない）

この段階ではファイルを書き換えないでください。
```

### Step 2: 設計とタスク分解（θ₂ GENERATE）

```
次に、設計とタスク分解を行ってください:

1. アーキテクチャ方針を決定
2. タスクを 5〜15 個に分解
3. API 境界がある場合は Contract を定義
4. .shiki/plans/PLAN.md を作成
5. .shiki/tasks/ にタスク JSON を作成
```

### Step 3: チーム編成（θ₃ ALLOCATE）

```
Plan に基づき Agent Teams を編成してください:

- roles/roles.yaml の役割を参考に
- 各メンバーにファイル所有権を割り当て
- メンバーは Plan Mode で起動
- 依存関係を明示
```

Leader が `TeamCreate` → `TaskCreate` → メンバーへの割り当てを行う。

### Step 4: メンバーの計画承認

メンバーが Plan を `SendMessage` で提出する:

```
[Member → Leader]
T-0002 の実行計画:
- 対象: src/api/auth.ts
- 方針: JWT ミドルウェアを作成
- 変更ファイル: src/middleware/auth.ts, tests/auth.test.ts
- 見積もり: 30,000 トークン
```

Leader が確認し `ExitPlanMode` で承認 → Member は Standard Mode に移行。

### Step 5: 実装（θ₄ EXECUTE）

メンバーがスコープ内で実装し、exec verify を実行:

```
lint: PASS
typecheck: PASS
test: PASS (12/12)
→ 全チェック合格。T-0002 を完了にします。
```

### Step 6: レビューと統合（θ₅ VERIFY → θ₆ INTEGRATE）

Leader が全タスクの完了を確認、レポートをレビュー、θ₅ → θ₆ を進行。

---

## 3. GitHubモード: Issue駆動開発

### Issue を作成

```markdown
## ゴール
ユーザー認証 API を実装する

## 受け入れ条件
- [ ] POST /api/auth/login で JWT が返る
- [ ] 不正パスワードで 401 エラー
- [ ] npm test が全パス

## スコープ
### やる
- JWT 認証
### やらない
- OAuth, MFA
```

`ai-goal` ラベルを付与すると自動起動。

### ワークフローの自動実行

```
Issue 作成 + ai-goal ラベル
  → shiki-orchestrator.yml
    → θ₁ ゴール分析
    → θ₂ 計画・タスク分解
    → θ₃ DAG 生成
  → shiki-dag-executor.yml
    → バッチ 0: 依存なしタスクを並列実行（worktree）
    → バッチ 1: バッチ 0 に依存するタスクを実行
    → ...
  → PR 自動作成
  → shiki-review.yml で自動レビュー
  → 人間が確認してマージ
```

### 進捗の確認

- Issue のラベル変化で状態を追跡
- Actions タブでワークフローの実行状況を確認
- `.shiki/reports/` でタスクレポートを確認

---

## 4. タスクの作り方

### 手動作成

`.shiki/tasks/T-0001.json`:

```json
{
  "id": "T-0001",
  "title": "JWT 認証ミドルウェアの実装",
  "assigned_to": "claude-member",
  "status": "pending",
  "priority": "high",
  "authority_layer": "executor",
  "theta_phase": "execute",
  "depends_on": [],
  "context": {
    "target_files": ["src/middleware/auth.ts"],
    "ownership": {
      "include": ["src/middleware/", "tests/middleware/"],
      "exclude": []
    }
  },
  "acceptance": [
    "npm test -- --grep 'auth'",
    "npm run lint",
    "npx tsc --noEmit"
  ],
  "budget": {
    "estimated_tokens": 30000,
    "max_tokens": 50000
  }
}
```

### Agent Teams で作成（CLIモード）

Leader が `TaskCreate` ツールで作成:

```
TaskCreate:
  subject: "JWT 認証ミドルウェアの実装"
  description: "src/middleware/auth.ts に JWT 検証ミドルウェアを作成..."
```

作成後、`post_task_sync.py` フックが `.shiki/tasks/` に自動同期。

### タスク設計のルール

| ルール | 理由 |
|--------|------|
| 1タスク1責務 | 失敗時のリカバリが容易 |
| acceptance は実行可能なコマンド | 自動検証が可能 |
| ownership を明示 | スコープ外編集を防止 |
| depends_on を正直に | 並列実行時の破綻を防止 |
| budget を設定 | コスト暴走を防止 |

---

## 5. 契約（Contract）の使い方

### いつ使うか

- 2つ以上のタスクが同じ API/型を共有する場合
- フロントエンドとバックエンドの並列開発

### 作成

`.shiki/contracts/auth-api-v1.json`:

```json
{
  "contract_id": "auth-api-v1",
  "version": "1.0.0",
  "status": "proposed",
  "defined_by": "architect",
  "implemented_by": ["backend-dev"],
  "interface": {
    "type": "openapi",
    "path": "POST /api/auth/login",
    "request": {
      "body": { "email": "string", "password": "string" }
    },
    "response": {
      "200": { "token": "string", "expires_in": "number" },
      "401": { "error": "string" }
    }
  }
}
```

### 状態遷移

```
proposed → agreed → implemented → verified → integrated
```

- `proposed`: 設計者が提案。レビュー中
- `agreed`: 関係者が合意。以降の変更には再提案が必要
- `implemented`: 実装完了
- `verified`: テスト合格
- `integrated`: マージ済み

### 変更したい場合

coordinator に変更理由を報告 → status を proposed に戻す → 再合意。
**黙って変更しない**。並列タスクが壊れる。

---

## 6. exec verify の使い方

### 基本

実装完了後、以下のループを自動実行:

```
1. 変更を実施
2. チェック実行:
   - lint (npm run lint)
   - typecheck (npx tsc --noEmit)
   - test (npm test)
   - security (任意)
3. 全パス → 完了
4. 失敗 → 差分分析 → 修正 → Step 2 に戻る
5. 10回失敗 → guardian にエスカレーション
```

### acceptance の書き方

```json
"acceptance": [
  "npm test -- --grep 'auth' --bail",
  "npm run lint -- --max-warnings=0",
  "npx tsc --noEmit",
  "curl -sf http://localhost:3000/health | jq -e '.status==\"ok\"'"
]
```

ポイント:
- 全てコマンドラインで実行可能にする
- 成功時に exit code 0 を返すようにする
- 曖昧な条件（「コードが綺麗」）は書かない

### 収束しない場合

原因の80%:
1. 受け入れ条件が矛盾している
2. スコープ外の修正が必要
3. 設計の問題（θ₂ に戻る必要）
4. テスト環境の問題

→ guardian にエスカレーション。

---

## 7. セッション復旧

### 自動復旧

```bash
# 前回のセッションから再開
./scripts/start_cli_session.sh --resume
```

### 手動復旧

```bash
# 復旧プロンプト生成
python3 scripts/recover_session.py
```

### 復旧時に確認すべきもの

| 確認対象 | ファイル |
|---------|---------|
| セッション状態 | `.shiki/state/session-*.json` |
| 未完了タスク | `.shiki/tasks/*.json` (status != completed) |
| 作業ログ | `.ai/logs/YYYY-MM-DD.md` |
| 未コミット変更 | `git status` |

---

## 8. プロンプト集

詳細は `docs/08_prompts_library.md` を参照。以下はよく使うものの抜粋。

### G0: 初回指示（ゴール → 設計）

```
あなたはこのリポジトリの AI 開発統括（Team Leader）です。
まずは Plan モードで、コード変更を始めずに設計を行ってください。

入力:
- GOAL.md を読み、ゴールと受け入れ条件を抽出
- 不明点やリスクを列挙

出力:
1. 設計方針
2. タスク分割（並列化できる粒度）
3. 必要な役割（Team 編成案: 最大5役割）
4. .shiki/plans/PLAN.md の提案
```

### T0: チーム編成

```
Plan に基づき Agent Teams を編成してください。
roles/roles.yaml の役割を参照し、足りない役割は追加提案してください。

メンバーは Plan Mode で起動し、
各自が担当領域の調査 → 実行計画を作成して Leader に提出してください。
```

### E0: タスク・Contract 作成

```
Plan とチームの提案に基づき:
- .shiki/tasks/ にタスク JSON を作成
- API 境界がある場合 .shiki/contracts/ に Contract を作成
- acceptance に「テスト合格条件」を必ず入れる
```

### Self-Claim: タスクの自己申告

```
[executor → coordinator]
T-XXXX の自己割当を申請します。
理由: 自分のスコープ（src/api/）に合致し、依存タスク T-0001 が完了済みです。
```

### Escalation: エスカレーション

```
[executor → coordinator]
T-XXXX の実装中に、スコープ外のファイル（src/utils/helper.ts）の
変更が必要になりました。
理由: 共通ユーティリティに新しい関数が必要
提案: helper.ts に validateEmail() を追加
スコープ拡張を承認していただけますか？
```

---

## 9. よくある操作

### タスクの状態確認

CLIモード:
```
TaskList で全タスクを表示
```

ファイルベース:
```bash
# 全タスクの状態を一覧
for f in .shiki/tasks/T-*.json; do
  echo "$(basename $f): $(python3 -c "import json; print(json.load(open('$f'))['status'])")"
done
```

### θフェーズの確認

```bash
# 最新のセッション状態から確認
cat .shiki/state/session-*.json | python3 -c "
import json, sys, glob
files = sorted(glob.glob('.shiki/state/session-*.json'), reverse=True)
if files:
    data = json.load(open(files[0]))
    print(f'Phase: {data.get(\"theta_phase\", \"unknown\")}')
"
```

### バジェットの確認

```bash
# タスクごとのバジェット使用状況
for f in .shiki/tasks/T-*.json; do
  python3 -c "
import json
d = json.load(open('$f'))
b = d.get('budget', {})
if b:
    used = b.get('actual_tokens', 0)
    limit = b.get('max_tokens', 0)
    pct = (used/limit*100) if limit else 0
    print(f\"{d['id']}: {used}/{limit} ({pct:.0f}%)\")
"
done
```

### DAG の可視化

```bash
python3 scripts/dag_to_mermaid.py .shiki/dag/DAG-001.json
```

出力される Mermaid 図を Issue や PR に貼り付けて可視化できる。

### ログの確認

```bash
# 今日の LDD ログ
cat .ai/logs/$(date +%Y-%m-%d).md
```

### スキーマの検証

```bash
python3 scripts/validate_shiki.py
```

---

## 関連ドキュメント

| ドキュメント | 内容 |
|------------|------|
| [SETUP.md](./SETUP.md) | セットアップ手順 |
| [CLAUDE.md](./CLAUDE.md) | AI エージェント向け憲法 |
| [docs/16_best_practices.md](./docs/16_best_practices.md) | ベストプラクティス |
| [docs/08_prompts_library.md](./docs/08_prompts_library.md) | プロンプト集（全文） |
| [docs/12_authority_model.md](./docs/12_authority_model.md) | 4層権限モデル詳細 |
| [docs/13_convergence_model.md](./docs/13_convergence_model.md) | θ収束モデル詳細 |
| [docs/14_session_recovery.md](./docs/14_session_recovery.md) | セッション復旧詳細 |
| [docs/15_ldd_protocol.md](./docs/15_ldd_protocol.md) | LDD プロトコル |
