# CLI セッション開始テンプレート

このテンプレートは、CLIモードで Agent Teams セッションを開始する際の手順と各ステップのコマンド/指示を示します。

---

## 1. チーム作成

### TeamCreate の実行

```
TeamCreate:
  team_name: "{project_name}-team"
  description: "GOAL.md に基づく開発チーム"
```

### チーム名の命名規則
- プロジェクト名 + `-team` を基本とする
- 例: `user-auth-team`, `api-refactor-team`, `bugfix-2024Q3-team`

---

## 2. ロール割当

### roles.yaml からのロール選定

`roles/roles.yaml` の `authority_layers` と `roles` セクションを参照し、必要なロールを選定します。

### 典型的なチーム編成（3〜5名）

#### 小規模（3名）
```
メンバー 1: TeamLeader     (coordinator) — Delegate Mode
メンバー 2: Implementer    (executor)    — Plan Mode 開始
メンバー 3: QA             (executor)    — Plan Mode 開始
```

#### 中規模（4名）
```
メンバー 1: TeamLeader     (coordinator) — Delegate Mode
メンバー 2: Implementer    (executor)    — Plan Mode 開始
メンバー 3: Implementer    (executor)    — Plan Mode 開始
メンバー 4: SecurityReviewer (monitor)   — 常時 Plan Mode
```

#### 大規模（5名）
```
メンバー 1: TeamLeader     (coordinator) — Delegate Mode
メンバー 2: Architect      (coordinator) — Plan Mode
メンバー 3: Implementer    (executor)    — Plan Mode 開始
メンバー 4: QA             (executor)    — Plan Mode 開始
メンバー 5: SecurityReviewer (monitor)   — 常時 Plan Mode
```

### ロール割当時の注意事項
- coordinator は最大2名（Leader + Architect）まで
- executor は実装規模に応じて1〜3名
- monitor は1名を推奨（大規模なら2名も可）
- 合計は `.shiki/config.yaml` の `defaults.max_team_size`（デフォルト5）以内

---

## 3. 初期タスク作成

### TaskCreate の実行

GOAL.md と Plan に基づき、タスクを作成します。

#### タスクJSON テンプレート

```json
{
  "id": "T-0001",
  "title": "[タスクタイトル]",
  "assigned_to": "claude-member",
  "status": "pending",
  "priority": "high",
  "authority_layer": "executor",
  "mode_origin": "cli",
  "theta_phase": "execute",
  "budget": {
    "estimated_tokens": 50000,
    "max_tokens": 100000
  },
  "depends_on": [],
  "context": {
    "ownership": {
      "include": ["src/api/**"],
      "exclude": []
    },
    "description": "[タスクの詳細な説明]",
    "contract_ref": null
  },
  "acceptance": [
    "npm test が全パス",
    "npm run lint がエラーなし",
    "該当APIのE2Eテストが追加されている"
  ],
  "outputs": [
    "src/api/auth.ts",
    "tests/api/auth.test.ts"
  ],
  "claimed_by": null,
  "notes": ""
}
```

### タスク分解の目安

| 粒度 | タスク数 | 推奨場面 |
|---|---|---|
| 粗い | 3〜5 | 小規模プロジェクト、単機能実装 |
| 標準 | 5〜10 | 中規模プロジェクト、複数機能 |
| 細かい | 10〜15 | 大規模プロジェクト、並列化重視 |

### 依存関係の定義

```json
{
  "id": "T-0003",
  "depends_on": ["T-0001", "T-0002"],
  "notes": "T-0001（API定義）とT-0002（DB設計）の完了が必要"
}
```

---

## 4. メンバー起動

### 各メンバーのエージェント定義

メンバー起動時に、`.claude/agents/` のエージェント定義ファイルが適用されます。

| メンバータイプ | エージェント定義 | 起動モード |
|---|---|---|
| coordinator | `.claude/agents/coordinator.md` | Delegate Mode |
| executor | `.claude/agents/executor.md` | Plan Mode |
| monitor | `.claude/agents/monitor.md` | Plan Mode（常時） |

### 起動時の指示テンプレート

#### executor への初期指示

```
あなたは executor（実装担当）です。以下のタスクを担当してください。

担当タスク: T-XXXX "[タスクタイトル]"
ファイル所有権: src/api/**
依存タスク: [T-YYYY]（完了済み/待ち）

まず Plan Mode で以下を行ってください：
1. 関連コードを読み取り、現状を理解する
2. 実装アプローチを検討する
3. 変更対象ファイルを特定する
4. 実行計画を作成し、SendMessage で coordinator に提出する

Plan が承認されるまで Edit/Write は使用しないでください。
```

#### monitor への初期指示

```
あなたは monitor（品質・セキュリティ監視）です。

監視対象:
- 全メンバーの変更内容
- セキュリティ（入力検証、認証、秘密情報）
- 契約遵守（.shiki/contracts/）
- コード品質（命名、構造、エラーハンドリング）

常時 Plan Mode で動作してください（Edit/Write 禁止）。
問題を発見したら：
- Critical/High: ブロッキングタスクを TaskCreate で作成
- Medium/Low: coordinator に SendMessage で報告

.claude/agents/monitor.md のチェックリストに従ってレビューしてください。
```

---

## 5. θフェーズ初期化

### セッション状態ファイルの作成

セッション開始時に `.shiki/state/session-*.json` を作成します。

```json
{
  "session_id": "session-{YYYYMMDDTHHMMSS}",
  "mode": "cli",
  "timestamp": "{ISO 8601}",
  "team_name": "{project_name}-team",
  "members": [
    {
      "name": "leader",
      "role": "TeamLeader",
      "authority_layer": "coordinator",
      "status": "active"
    },
    {
      "name": "impl-1",
      "role": "Implementer",
      "authority_layer": "executor",
      "status": "active"
    },
    {
      "name": "qa-1",
      "role": "QA",
      "authority_layer": "executor",
      "status": "active"
    }
  ],
  "active_tasks": [],
  "theta_phase": "understand",
  "budget": {
    "tokens_used": 0,
    "tokens_remaining": 500000,
    "budget_limit": 500000
  },
  "notes": "新規セッション開始"
}
```

### θ₁ UNDERSTAND の開始

```
θ₁ UNDERSTAND フェーズを開始します。

目標:
- GOAL.md のゴールと受け入れ条件を理解する
- スコープを確定する

exit_criteria:
- acceptance_criteria_defined: 受け入れ条件が明文化されている
- scope_bounded: スコープが明確に定義されている

全メンバーに GOAL.md を読み取らせ、理解を共有してください。
不明点があれば質問をまとめてください。
```

---

## チェックリスト

セッション開始時に以下を確認してください：

- [ ] `.shiki/config.yaml` が正しく設定されている
- [ ] `GOAL.md` が作成されている
- [ ] `roles/roles.yaml` が参照可能
- [ ] `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` が設定されている
- [ ] tmux / iTerm2 が起動可能（表示モードに応じて）
- [ ] Python 3.10+ が利用可能
- [ ] `.claude/agents/` のエージェント定義が存在する
- [ ] `.claude/hooks/` のフックが設定されている（推奨）
