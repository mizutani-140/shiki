# Log-Driven Development（LDD）プロトコル

LDD は全ての開発活動をログとして記録し、AI の学習資産として蓄積する開発手法です。ログはセッション復旧のコンテキスト復元にも使用されます。

---

## 目次

1. [概要](#1-概要)
2. [ログ形式](#2-ログ形式)
3. [ログファイル構造](#3-ログファイル構造)
4. [記録タイミング](#4-記録タイミング)
5. [設定](#5-設定)
6. [活用方法](#6-活用方法)

---

## 1. 概要

### LDD の原則

- **全作業を記録する**: 意図・計画・実装・検証の4段階で記録
- **AIの学習資産**: 過去のログから成功/失敗パターンを学習
- **セッション復旧**: 中断されたセッションのコンテキスト復元に使用
- **透明性**: 何が・いつ・なぜ行われたかを追跡可能にする

### codex_prompt_chain

LDD の中核は `codex_prompt_chain` と呼ばれる4段階の思考チェーンです：

| 段階 | 英語 | 説明 |
|------|------|------|
| 1 | **intent** | 何を達成しようとしているか（目的） |
| 2 | **plan** | どのように達成するか（手順） |
| 3 | **implementation** | 何を変更したか（実装内容） |
| 4 | **verification** | どうやって確認したか（検証結果） |

---

## 2. ログ形式

### ファイル命名規則

```
.ai/logs/YYYY-MM-DD.md
```

1日1ファイル。同じ日に複数セッションがある場合は同じファイルに追記されます。

### ヘッダー

```markdown
# Log-Driven Development Log - 2025-01-15

**Device**: MacBook-Pro
**Project**: my-project
**Date**: 2025-01-15

---

## codex_prompt_chain

**intent**:
**plan**:
**implementation**:
**verification**:

---

## tool_invocations
```

### エントリー形式

```markdown
### [2025-01-15T10:30:00Z]
- **type**: session_start
- **content**: Session started (mode=cli), active tasks: T-0001: API実装

### [2025-01-15T10:35:00Z]
- **type**: command
- **content**: npm run lint

### [2025-01-15T12:00:00Z]
- **type**: session_stop
- **content**: Session ended
```

---

## 3. ログファイル構造

```
.ai/
└── logs/
    ├── .keep
    ├── 2025-01-13.md
    ├── 2025-01-14.md
    └── 2025-01-15.md
```

### セクション説明

#### codex_prompt_chain

セッション全体の目的と成果を要約する。セッション開始時に `intent` と `plan` を記入し、終了時に `implementation` と `verification` を追記する。

```markdown
## codex_prompt_chain

**intent**: ユーザー認証APIの実装
**plan**: 1. JWT ミドルウェア作成 2. ログインエンドポイント実装 3. テスト作成
**implementation**: src/auth/jwt.ts, src/api/login.ts, tests/auth.test.ts を作成
**verification**: npm test 全パス、lint エラーなし、型チェック合格
```

#### tool_invocations

セッション中のツール呼び出しを時系列で記録する。自動的に追記される。

---

## 4. 記録タイミング

### 自動記録（Hooks による）

| イベント | フック | 記録内容 |
|----------|--------|----------|
| セッション開始 | `ldd_logger.py session_start` | モード、アクティブタスク一覧 |
| セッション終了 | `ldd_logger.py session_stop` | セッション終了マーカー |

### 手動記録（推奨）

| タイミング | 記録すべき内容 |
|-----------|---------------|
| タスク着手時 | intent と plan |
| 実装完了時 | implementation の要約 |
| exec verify 完了時 | verification の結果 |
| エスカレーション時 | エスカレーション理由と対応 |

---

## 5. 設定

### config.yaml

```yaml
ldd:
  enabled: true
  log_dir: .ai/logs
  format: "YYYY-MM-DD.md"
  sections:
    - intent
    - plan
    - implementation
    - verification
```

### settings.json（Hooks 登録）

LDD フックは `.claude/settings.json` の `SessionStart` と `Stop` イベントに登録されています。

```json
{
  "SessionStart": [
    {
      "command": "python3 .claude/hooks/ldd_logger.py session_start"
    }
  ],
  "Stop": [
    {
      "command": "python3 .claude/hooks/ldd_logger.py session_stop"
    }
  ]
}
```

---

## 6. 活用方法

### セッション復旧

セッションが中断された場合、直近のログファイルから以下を復元できます：

1. 最新の `.ai/logs/YYYY-MM-DD.md` を読む
2. `codex_prompt_chain` から目的と計画を復元
3. `tool_invocations` から進捗を確認
4. 未完了の作業を特定して再開

### パターン学習

過去のログから以下のパターンを抽出できます：

- 頻出するエラーパターンとその解決方法
- 効率的なタスク実行順序
- exec verify で頻繁に失敗するチェック項目
- バジェット消費の傾向

### チーム間共有

CLIモードでは複数エージェントのログが同じファイルに蓄積されるため、チーム全体の活動を俯瞰できます。

---

## 関連ドキュメント

- [exec verify プロトコル](../claude/instructions.md) - 検証ループとの統合
- [セッション復旧](./14_session_recovery.md) - 復旧手順の詳細
- [Guardian](../.github/GUARDIAN.md) - エスカレーション時のログ記録
