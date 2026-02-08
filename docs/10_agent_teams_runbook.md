# Agent Teams 運用手順書（CLIモード対話型）

Agent Teams をCLIモードで運用するための包括的な手順書です。Delegate Mode、Plan Mode、P2P通信、Self-Claim、バジェット管理、θフェーズ追跡、トラブルシューティングをカバーします。

---

## 目次

1. [有効化と起動](#1-有効化と起動)
2. [Delegate Mode 運用ガイド](#2-delegate-mode-運用ガイド)
3. [P2P コミュニケーションガイドライン](#3-p2p-コミュニケーションガイドライン)
4. [tmux 管理](#4-tmux-管理)
5. [Self-Claim ワークフロー](#5-self-claim-ワークフロー)
6. [Plan Mode → Standard Mode 移行](#6-plan-mode--standard-mode-移行)
7. [バジェット監視](#7-バジェット監視)
8. [θフェーズ報告](#8-θフェーズ報告)
9. [Dual Engine 運用（Claude + Codex）](#9-dual-engine-運用claude--codex)
10. [トラブルシューティング](#10-トラブルシューティング)

---

## 1. 有効化と起動

### 環境変数

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

### 起動スクリプト

```bash
# 通常起動（tmux セッション自動構築）
./scripts/start_cli_session.sh

# 前回セッションから自動リジューム
./scripts/start_cli_session.sh --resume
```

### 設定ファイル

`.shiki/config.yaml` の `cli` セクション：

```yaml
cli:
  display: tmux          # tmux | iterm2 | in-process
  delegate_mode: true    # Leader を Delegate Mode で起動
  self_claim: true       # メンバーの自己割当を許可
  plan_mode_required: true  # メンバーは Plan Mode で起動
```

### 直接起動

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
cd /path/to/project
claude
```

---

## 2. Delegate Mode 運用ガイド

### 概要

Delegate Mode は Leader（coordinator 層）が実装に潜ることを防ぎ、計画・調整・承認に専念させるモードです。大規模チーム（3名以上）では特に重要です。

### Leader の許可ツール

| ツール | 用途 |
|---|---|
| `TeamCreate` | チーム作成 |
| `SendMessage` | メンバーへの指示・フィードバック送信 |
| `TaskCreate` | 新規タスク作成 |
| `TaskList` | タスク一覧確認 |
| `TaskUpdate` | タスク状態更新 |
| `EnterPlanMode` | メンバーを Plan Mode に戻す |
| `ExitPlanMode` | メンバーの Plan 承認（Standard Mode 移行） |

### Leader の禁止ツール

- `Edit` — ファイル編集
- `Write` — ファイル書き込み
- `Bash`（破壊的コマンド）

### Delegate Mode の典型的な1日

```
朝（セッション開始）:
1. GOAL.md とタスク一覧を確認
2. 前日の進捗を把握
3. 今日の優先タスクを決定
4. メンバーにタスクを割り当てる

午前（実行管理）:
5. メンバーの Plan 提出を受領
6. Plan をレビューし、承認/差し戻し
7. ブロッキング問題を解決

午後（監視と調整）:
8. 進捗を TaskList で確認
9. monitor からのレビュー結果を処理
10. 修正タスクを作成（必要に応じて）

夕方（まとめ）:
11. θフェーズの exit_criteria を評価
12. セッション状態を保存
13. バジェット消費を報告
```

### コーディネーションのコツ

- **朝の計画を丁寧に**: 最初の指示が明確であるほど、メンバーの Plan 提出の質が上がる
- **ブロック解除を優先**: メンバーがブロックされている時間はバジェットの無駄遣い
- **ブロードキャスト節約**: 全員宛のメッセージは本当に必要な時だけ。個別 SendMessage を基本とする
- **monitor の報告を尊重**: monitor が Critical を上げたら即対応

---

## 3. P2P コミュニケーションガイドライン

### 目的

メンバー間で直接 `SendMessage` を使い、coordinator を経由せずに技術的な調整を行うことで、コミュニケーションの効率を上げます。

### P2P が適切な場面

| 場面 | 例 |
|---|---|
| 依存タスクの進捗確認 | "T-0003 の API は使える状態になりましたか？" |
| インターフェースの詳細確認 | "getUser の戻り値の型を確認させてください" |
| 共通問題の相談 | "TypeScript の型エラーで困っています。同様の経験ありますか？" |
| コードの参照確認 | "このユーティリティ関数の使い方を教えてください" |

### P2P が不適切な場面（coordinator 経由にすべき）

| 場面 | 理由 |
|---|---|
| スコープ変更の依頼 | coordinator の承認が必要 |
| 新規タスクの提案 | coordinator が優先度を判断 |
| ブロッキング問題の報告 | coordinator が全体調整を行う |
| 契約変更の提案 | coordinator と architect の承認が必要 |
| θフェーズの進行 | coordinator の権限 |

### P2P 通信のルール

1. **coordinator への報告義務**: P2P で合意した重要事項は coordinator にも伝える
2. **ブロードキャスト禁止**: P2P の内容を全員にブロードキャストしない
3. **記録義務**: 契約に関わる合意はファイルに記録する
4. **節度**: 不要な通信でメンバーのコンテキストウィンドウを消費しない

---

## 4. tmux 管理

### セッション構成

```
セッション名: shiki
├── ウィンドウ 1: leader
│   ├── ペイン 0: Leader (Coordinator)
│   ├── ペイン 1: Member 1 (Executor)
│   └── ペイン 2: Member 2 (Executor/Monitor)
└── ウィンドウ 2: monitor
    └── セッション状態の監視
```

### ペインナビゲーション

| 操作 | キーバインド |
|---|---|
| ペイン移動 | `Ctrl+B` → 矢印キー |
| ペインをズーム（フルスクリーン） | `Ctrl+B` → `z` |
| ペインズーム解除 | `Ctrl+B` → `z`（トグル） |
| ウィンドウ切替（次） | `Ctrl+B` → `n` |
| ウィンドウ切替（前） | `Ctrl+B` → `p` |
| ウィンドウ一覧 | `Ctrl+B` → `w` |

### ペインの追加（メンバー増員）

```bash
# 新しい Executor ペインを追加
tmux split-window -v -t shiki:leader
tmux select-pane -T "Member 3 (Executor)"
tmux send-keys "export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 && cd /path/to/project && claude" C-m
```

### セッション管理

```bash
# セッション切断（バックグラウンド継続）
Ctrl+B → d

# セッション再接続
tmux attach-session -t shiki

# セッション一覧
tmux list-sessions

# セッション終了（注意: 全ペインが終了）
tmux kill-session -t shiki
```

### 監視ウィンドウの活用

ウィンドウ2（monitor）ではセッション状態ファイルがリアルタイムで表示されます：

```bash
# monitor ウィンドウに切替
Ctrl+B → n

# 5秒ごとに状態を更新
watch -n 5 'cat .shiki/state/session-*.json 2>/dev/null | python3 -m json.tool'
```

---

## 5. Self-Claim ワークフロー

### 前提条件

`.shiki/config.yaml` で `self_claim: true` が設定されていること。

### ワークフロー

```
メンバー                          coordinator
  │                                    │
  ├─ TaskList で未割当タスクを確認 ──────┤
  │                                    │
  ├─ 適格性チェック（authority_layer, │
  │   スコープ, 依存関係）             │
  │                                    │
  ├─ SendMessage: 自己割当申請 ──────→ │
  │                                    ├─ 申請内容を確認
  │                                    ├─ 競合チェック
  │                                    ├─ TaskUpdate: claimed_by 更新
  │  ←───── SendMessage: 承認 ─────────┤
  │                                    │
  ├─ Plan Mode で計画作成              │
  ├─ SendMessage: Plan 提出 ──────→    │
  │                                    ├─ Plan レビュー
  │  ←───── ExitPlanMode: 承認 ────────┤
  │                                    │
  ├─ Standard Mode で実装             │
  ├─ 受け入れ条件クリア                │
  ├─ TaskUpdate: status → review       │
  ├─ SendMessage: 完了報告 ────────→   │
  │                                    │
```

### 自己割当申請のテンプレート

```
T-XXXX の自己割当を申請します。

適格性:
- authority_layer: executor（タスクと一致）
- スコープ: src/api/**（タスクの対象ファイルと合致）
- 依存タスク: T-YYYY（完了済み）

現在の負荷:
- T-0001: completed
- T-0003: in_progress（残り30%）

見積もり:
- 推定トークン: 50,000
- 推定所要時間: 2時間
```

---

## 6. Plan Mode → Standard Mode 移行

### Plan Mode（起動時のデフォルト）

#### 許可される操作
- ファイルの読み取り（Read, Grep, Glob）
- コードベースの分析
- 実行計画の作成
- coordinator への SendMessage

#### 禁止される操作
- ファイル編集（Edit, Write）
- 破壊的コマンド実行

### 移行プロセス

```
1. [Plan Mode] コードベースを調査・分析
2. [Plan Mode] 実行計画を作成
3. [Plan Mode] coordinator に Plan を SendMessage で提出
4. [待機] coordinator がレビュー
5. [判定]
   ├─ 承認: coordinator が ExitPlanMode → Standard Mode に移行
   └─ 差し戻し: 修正要求を受け取り、Plan を修正して再提出
6. [Standard Mode] 実装開始
```

### Plan 提出のテンプレート

```markdown
## タスク分析
- タスクID: T-XXXX
- タイトル: [タスクタイトル]
- 現状の理解: [コードベースの調査結果]

## 実装アプローチ
- 方針: [具体的な実装方法]
- 変更対象ファイル:
  - `path/to/file1.ts` — [変更内容の概要]
  - `path/to/file2.ts` — [変更内容の概要]

## 依存関係とブロッカー
- 依存タスク: [T-YYYY が完了していること]
- 契約参照: [contract_id があれば]
- ブロッカー: [なければ「なし」]

## 見積もり
- 推定トークン: [概算]

## 受け入れ条件の検証計画
- [ ] テスト: npm test
- [ ] リンタ: npm run lint
- [ ] 型チェック: npx tsc --noEmit
```

### Standard Mode（承認後）

#### 許可される操作
- ファイル編集（Edit, Write） — **所有権スコープ内のみ**
- テスト/ビルド/リンタの実行（Bash）
- タスク状態の更新（TaskUpdate）
- メンバー/coordinator への SendMessage

#### 完了条件
- 受け入れ条件（acceptance）をすべてクリア
- `.shiki/reports/<TASK_ID>.md` にレポート作成
- `TaskUpdate` で status を `review` に変更

---

## 7. バジェット監視

### バジェット設定の確認

```yaml
# .shiki/config.yaml
github:
  budget:
    max_tokens_per_task: 100000      # タスクあたりの上限
    max_tokens_per_session: 500000   # セッションあたりの上限
    warn_threshold_pct: 80           # 警告閾値（%）
```

### coordinator のバジェット管理義務

| タイミング | アクション |
|---|---|
| セッション開始時 | バジェット上限を確認し、チームに共有 |
| タスク割当時 | タスクのバジェット見積もりを設定 |
| 進捗確認時 | 使用トークン量を概算で追跡 |
| 80% 到達時 | チーム全体に警告を送信 |
| 超過見込み時 | 低優先度タスクをブロックし、ユーザーに報告 |

### メンバーのバジェット意識

| タイミング | アクション |
|---|---|
| タスク着手時 | `budget.max_tokens` を確認 |
| 実装中 | 概算でトークン消費を意識 |
| 80% 接近時 | 最小限の変更で完了を目指す |
| 超過見込み時 | coordinator に即座に報告 |

### バジェット超過時の対応フロー

```
メンバー: "T-XXXX のバジェットが80%に達しました"
    ↓
coordinator: タスク一覧と残バジェットを確認
    ↓
判断:
├── A) バジェット追加: ユーザーに承認を求める
├── B) タスク分割: 残り作業を新タスクに分離
├── C) スコープ縮小: 低優先度の要件をスキップ
└── D) 中止: タスクを blocked にして理由を記載
```

---

## 8. θフェーズ報告

### フェーズ一覧

| フェーズ | 名称 | exit_criteria |
|---|---|---|
| θ₁ | UNDERSTAND | acceptance_criteria_defined, scope_bounded |
| θ₂ | GENERATE | plan_created, tasks_decomposed, contracts_drafted |
| θ₃ | ALLOCATE | dag_valid, roles_assigned, dependencies_resolved |
| θ₄ | EXECUTE | all_tasks_completed, acceptance_passed |
| θ₅ | VERIFY | review_approved, security_passed, contracts_verified |
| θ₆ | INTEGRATE | ci_passed, merged, deployed_or_tagged |

### 報告フォーマット

coordinator はフェーズ完了時に以下の形式で報告します：

```markdown
## θフェーズ報告

### 現在フェーズ: θ₄ EXECUTE
### 進捗状況

exit_criteria:
- [x] all_tasks_completed: 10/10 タスク完了
- [x] acceptance_passed: 全タスクの受け入れ条件クリア

### 判定: θ₅ VERIFY に進行

### バジェット状況
- 使用: 320,000 / 500,000 トークン（64%）
- 残りフェーズ見積もり: 120,000 トークン

### 備考
- T-0007 の修正で1回の再反復が発生（θ₄ 内）
- 契約 contract-002 の変更提案あり（θ₅ で検証予定）
```

### 再反復の報告

品質不足でフェーズを戻す場合：

```markdown
## θフェーズ再反復報告

### 再反復: θ₅ VERIFY → θ₄ EXECUTE

### 理由
- monitor が Critical セキュリティ問題を検出（BLOCK-0001）
- 入力バリデーション不足により SQL インジェクションのリスクあり

### 影響範囲
- T-0003: セキュリティ修正が必要（新タスク T-0011 作成）
- T-0005: 関連する入力処理の見直し（新タスク T-0012 作成）

### バジェット影響
- 追加見積もり: 40,000 トークン
- 残りバジェット: 100,000 トークン（対応可能）

### 対応方針
- T-0011, T-0012 を優先実行
- 完了後 θ₅ VERIFY を再実行
```

---

## 9. Dual Engine 運用（Claude + Codex）

### 概要

Agent Teams では Codex MCP Server を通じて、タスクの特性に応じたエンジン振分を行います。coordinator が θ₃ でエンジンを割り当て、executor が実行時にエンジンを切り替えます。

### coordinator のエンジン管理

#### θ₃ ALLOCATE でのエンジン振分

```bash
# 全タスクに最適エンジンを自動割当
python3 scripts/engine_router.py --all

# ドライランで確認
python3 scripts/engine_router.py --all --dry-run
```

#### θ₄ EXECUTE でのエンジン指示

| タスクの engine | coordinator の指示 |
|----------------|-------------------|
| `codex` | "Codex MCP 経由で委託してください" |
| `claude-team` | "直接実装してください" |
| `auto` | "engine_router.py の判定に従ってください" |

#### Fallback 発生時の対応

executor が Fallback を報告した場合：
1. 失敗原因を確認する
2. タスク仕様の明確化が必要なら contract を修正する
3. 繰り返し失敗する場合はエンジン固定（`engine: claude-team`）を検討する

### executor のエンジン切替

#### Codex MCP 委託の判断基準

以下に該当する場合は Codex 委託が効果的：
- 受け入れ条件がテストコマンドで明確に定義されている
- 変更が 1-2 ファイルに収まる
- Contract が存在し、インターフェースが確定している

#### 委託時のプロンプト構成

```
タスクID: T-XXXX
対象ファイル: src/api/handler.ts
受け入れ条件: npm test -- --filter=handler
契約: contract-003（ApiHandler インターフェース）
バジェット: 50,000 tokens

上記の仕様に基づいて最小限の実装を行ってください。
```

#### Fallback の報告

```
coordinator 宛:
"T-XXXX: Codex MCP 委託が失敗しました（テスト 2/8 不合格）。
 直接実装にフォールバックし、exec verify を再実行します。
 execution_path: fallback"
```

### トラブルシューティング: Codex MCP 接続

```bash
# Codex CLI の確認
codex --version

# MCP サーバーの動作確認
codex mcp-server --help

# 環境変数の確認
echo $OPENAI_API_KEY | head -c 8
```

Codex MCP が利用不可の場合、executor は全タスクを Claude で直接実装します（graceful degradation）。

---

## 10. トラブルシューティング

### 問題: タスク状態の更新漏れ

**症状**: タスクの実態と `.shiki/tasks/` の状態が一致しない

**原因**: メンバーが `TaskUpdate` を忘れている

**対策**:
1. `post_task_sync.py` hook を有効化し、自動検出する
2. coordinator が定期的に `TaskList` で状態を確認する
3. メンバーに完了報告時の `TaskUpdate` を徹底する

```bash
# 手動で状態を同期
python3 scripts/sync_agent_teams_state.py --dry-run
python3 scripts/sync_agent_teams_state.py
```

---

### 問題: 同一ファイルの競合編集

**症状**: 複数メンバーが同じファイルを編集し、変更が衝突する

**原因**: ファイル所有権スコープの重複

**対策**:
1. タスク割当時にファイル所有権スコープを明確に定義する
2. 所有権の重複がないか coordinator が事前に確認する
3. 共有ファイルは1人が担当し、他はその完了を待つ

```
coordinator → member:
"src/shared/utils.ts は Member 1 のスコープです。
変更が必要な場合は Member 1 に SendMessage で依頼してください。"
```

---

### 問題: ブロードキャスト乱用

**症状**: 全員宛のメッセージが多すぎて、コンテキストウィンドウが消費される

**原因**: `SendMessage` の宛先を全員にしている

**対策**:
1. 基本は個別 `SendMessage` を使用する
2. 全員に共有が必要な場合のみブロードキャストする
3. coordinator がメンバーに注意喚起する

---

### 問題: Plan Mode から抜けられない

**症状**: メンバーが Plan を提出したが、coordinator の承認が来ない

**原因**: coordinator が Plan を見落としている、または他の作業に集中している

**対策**:
1. メンバーは一定時間後に再送信する
2. coordinator は Plan 提出通知を優先的に処理する
3. 複数メンバーの Plan が溜まっている場合は順番に処理する

---

### 問題: セッションが中断した

**症状**: ターミナルがクローズ、ネットワーク切断等

**対策**:
```bash
# tmux セッションが生存していれば再接続
tmux attach-session -t shiki

# セッションが失われた場合は復旧
./scripts/start_cli_session.sh --resume

# 手動復旧
python3 scripts/recover_session.py | claude --prompt -
```

詳細は `docs/14_session_recovery.md` を参照。

---

### 問題: バジェットが枯渇した

**症状**: トークン上限に達し、作業が続行できない

**対策**:
1. coordinator が残りタスクの優先度を再評価
2. 低優先度タスクを `blocked` にする
3. ユーザーにバジェット追加の承認を求める
4. 承認を受けたら `.shiki/config.yaml` の `budget` を更新

---

### 問題: θフェーズの exit_criteria が満たせない

**症状**: フェーズを進行できず、停滞している

**原因**: 基準が厳しすぎる、前フェーズの成果物に問題がある

**対策**:
1. 未達の criteria を具体的に特定する
2. 前フェーズに戻るべきか判断する（再反復）
3. ユーザーに基準の緩和を提案する（最終手段）
4. 再反復する場合はバジェット影響を評価してから決定する

---

### 問題: monitor が大量の問題を報告してくる

**症状**: Critical/High の問題が多すぎて処理が追いつかない

**対策**:
1. 問題を重大度順にソートする
2. Critical を最優先で対処（ブロッキングタスク）
3. High を次に対処
4. Medium/Low は次のイテレーションに回す
5. 根本原因がある場合は設計レベルの修正を検討する（θ₂ に再反復）

---

### 問題: sync_agent_teams_state.py でエラーが出る

**症状**: Agent Teams と `.shiki/` の同期が失敗する

**対策**:
```bash
# ドライランで差分を確認
python3 scripts/sync_agent_teams_state.py --dry-run --verbose

# 一方向のみ同期
python3 scripts/sync_agent_teams_state.py --direction to-shiki
python3 scripts/sync_agent_teams_state.py --direction to-teams

# チーム名を明示的に指定
python3 scripts/sync_agent_teams_state.py --team-name my-project
```
