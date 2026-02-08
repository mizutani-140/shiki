# CLIモード運用ガイド

CLIモードは、Claude Code Agent Teams をローカル環境で直接操作するモードです。tmux または iTerm2 を使ってチームメンバーを並列に可視化しながら、対話的に開発を進めます。

---

## 目次

1. [表示レイアウトのセットアップ](#1-表示レイアウトのセットアップ)
2. [Delegate Mode（リーダーの動作モード）](#2-delegate-modeリーダーの動作モード)
3. [Member Plan Mode ワークフロー](#3-member-plan-mode-ワークフロー)
4. [Self-Claim（自己割当）](#4-self-claim自己割当)
5. [P2P コミュニケーション](#5-p2p-コミュニケーション)
6. [セッション復旧](#6-セッション復旧)
7. [バジェット監視](#7-バジェット監視)
8. [Dual Engine（Claude + Codex 連携）](#8-dual-engineclaude--codex-連携)
9. [θフェーズ追跡](#9-θフェーズ追跡)

---

## 1. 表示レイアウトのセットアップ

### 設定ファイル

`.shiki/config.yaml` の `cli` セクションで表示モードを指定します：

```yaml
cli:
  display: tmux          # tmux | iterm2 | in-process
  delegate_mode: true    # Leader を Delegate Mode で起動
  self_claim: true       # メンバーの自己割当を許可
  plan_mode_required: true  # メンバーは Plan Mode で起動
```

### tmux レイアウト

tmux は最も推奨される表示モードです。`start_cli_session.sh` を使って自動セットアップできます。

```bash
./scripts/start_cli_session.sh
```

#### レイアウト構成

```
┌─────────────────────┬─────────────────────┐
│                     │    Member 1          │
│    Leader           │    (Executor)        │
│    (Coordinator)    ├─────────────────────┤
│                     │    Member 2          │
│                     │    (Executor/Monitor) │
└─────────────────────┴─────────────────────┘

ウィンドウ 2: [monitor] — セッション状態の監視
```

#### tmux 操作チートシート

| 操作 | キーバインド |
|---|---|
| ペイン間移動 | `Ctrl+B` → 矢印キー |
| ウィンドウ切替 | `Ctrl+B` → `n`（次）/ `p`（前） |
| ペインをズーム | `Ctrl+B` → `z` |
| ペイン一覧 | `Ctrl+B` → `w` |
| 新規ペイン（垂直） | `Ctrl+B` → `%` |
| 新規ペイン（水平） | `Ctrl+B` → `"` |
| セッション切断 | `Ctrl+B` → `d`（再接続: `tmux attach -t shiki`） |

#### ペインの追加（メンバー増員時）

チームサイズに応じてペインを動的に追加できます：

```bash
# 新しい Executor ペインを追加
tmux split-window -v -t shiki:leader
tmux select-pane -T "Member 3 (Executor)"

# 新しいペインで Claude Code を起動
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
claude
```

### iTerm2 レイアウト

macOS で iTerm2 を使用する場合、手動でペインを分割するか、AppleScript で自動化します。

#### 手動セットアップ

1. iTerm2 を開く
2. `Cmd+D` で垂直分割（Leader | Member1）
3. 右ペインで `Cmd+Shift+D` で水平分割（Member1 / Member2）
4. 各ペインで以下を実行：
   ```bash
   export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
   cd /path/to/project
   claude
   ```

#### AppleScript 自動化

`start_cli_session.sh` を `display: iterm2` で実行すると、コピペ用の AppleScript が表示されます：

```bash
# config.yaml で display: iterm2 に設定後
./scripts/start_cli_session.sh
```

### in-process モード

ペイン分割なしで、単一ターミナルで動作するモードです。チーム表示の切り替えは Claude Code が内部的に管理します。

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
claude
```

**注意**: in-process モードではメンバーの並列可視化ができません。小規模チーム（2〜3名）での使用を推奨します。

---

## 2. Delegate Mode（リーダーの動作モード）

### 概要

Delegate Mode は Leader（coordinator）が実装に潜ることを防ぎ、計画・調整・承認に専念させるモードです。

### 制約

Delegate Mode の Leader は以下のツールのみ使用可能です：

| ツール | 用途 |
|---|---|
| `TeamCreate` | チーム作成 |
| `SendMessage` | メンバーへの指示・フィードバック |
| `TaskCreate` | 新規タスク作成 |
| `TaskList` | タスク一覧確認 |
| `TaskUpdate` | タスク状態更新 |
| `EnterPlanMode` | メンバーを Plan Mode に戻す |
| `ExitPlanMode` | メンバーの Plan 承認（Standard Mode 移行） |

### 禁止されるツール

- `Edit` / `Write` — ファイル変更
- `Bash`（破壊的コマンド） — 直接的なコマンド実行

### Leader の動作フロー

```
1. GOAL.md を読み、ゴールを理解する
2. roles/roles.yaml を参照してチームを編成する（TeamCreate）
3. タスクを分解し、メンバーに割り当てる（TaskCreate → SendMessage）
4. メンバーの Plan を受け取り、レビュー → 承認する（ExitPlanMode）
5. 進捗を監視し、問題があれば調整する（TaskList → SendMessage）
6. 完了したタスクのレポートを確認する
7. θフェーズの exit_criteria を評価し、フェーズを進行させる
```

### エージェント定義ファイル

Leader の詳細な動作定義は `.claude/agents/coordinator.md` に記載されています。

---

## 3. Member Plan Mode ワークフロー

### 概要

メンバー（executor）は Plan Mode で起動し、実行計画を Leader に提出してから実装を開始します。

### ワークフロー

```
[起動] → [Plan Mode] → [計画提出] → [Leader レビュー] → [承認] → [Standard Mode] → [実装] → [完了報告]
```

### 各段階の詳細

#### Stage 1: Plan Mode（起動直後）

- コードベースの読み取りと分析を行う
- タスクの要件と受け入れ条件を理解する
- 関連するファイル、依存関係、契約を確認する
- **Edit/Write は使用不可**

#### Stage 2: 計画提出

以下の形式で実行計画を Leader に `SendMessage` で送信します：

```markdown
## タスク分析
- タスクID: T-XXXX
- 現状の理解: [調査結果]

## 実装アプローチ
- 方針: [具体的な実装方法]
- 変更対象ファイル:
  - `path/to/file.ts` — [変更内容]

## 依存関係とブロッカー
- 依存: [T-YYYY 完了待ち]
- ブロッカー: [なし]

## 見積もり
- 推定トークン: [概算]

## 受け入れ条件の検証計画
- [ ] テスト実行: npm test
- [ ] リンタ: npm run lint
```

#### Stage 3: Leader レビュー

Leader が計画を確認し、以下のいずれかを行います：
- **承認**: `ExitPlanMode` で Standard Mode に移行
- **差し戻し**: `SendMessage` で修正要求を送信

#### Stage 4: Standard Mode（承認後）

- ファイル所有権スコープ内で Edit/Write が可能
- Bash でテスト・ビルド・リンタの実行が可能
- 実装 → テスト → 修正のサイクルを回す

#### Stage 5: 完了報告

- 受け入れ条件をすべてクリアする
- `.shiki/reports/<TASK_ID>.md` にレポートを作成する
- `TaskUpdate` で status を `review` に変更する
- Leader に `SendMessage` で完了を報告する

---

## 4. Self-Claim（自己割当）

### 概要

`self_claim: true` の設定時、メンバーは未割当のタスクを自ら引き受けることができます。これにより、Leader のボトルネックを軽減し、チームの自律性を高めます。

### 手順

1. **タスク一覧の確認**
   - `TaskList` で `claimed_by: null` のタスクを確認する

2. **適格性チェック**
   - タスクの `authority_layer` が自分の層と一致するか
   - ファイル所有権スコープが自分の担当範囲と重ならないか
   - 依存タスクが完了しているか

3. **申請**
   ```
   SendMessage → coordinator:
   "T-XXXX の自己割当を申請します。
    理由: 私のスコープ (src/api/) に含まれるファイルの変更が必要です。
    現在のタスク負荷: T-0001（完了）、T-0003（進行中）"
   ```

4. **承認待ち**
   - coordinator が `TaskUpdate` で `claimed_by` を更新
   - 承認メッセージを受信

5. **作業開始**
   - Plan Mode で計画を提出 → 承認後に実装

### 注意事項

- 他のメンバーが同じタスクを申請している可能性があるため、coordinator の判断を待つ
- 自分の authority_layer を超えたタスクは申請できない（executor が coordinator タスクを取れない）
- バジェット状況を考慮し、過度な引き受けを避ける

---

## 5. P2P コミュニケーション

### 概要

メンバー間で `SendMessage` を使った直接通信が可能です。coordinator を経由せずに技術的な調整を行うことで、コミュニケーションの効率を上げます。

### 適切な使用場面

| 場面 | 例 |
|---|---|
| **依存タスクの進捗確認** | "T-0003 の API 定義は完了しましたか？" |
| **インターフェース調整** | "この関数のレスポンス型を確認したいです" |
| **技術的な質問** | "このライブラリの使い方について相談" |
| **問題の共有** | "共通の依存でバグを発見しました" |

### 不適切な使用（coordinator 経由にすべき）

| 場面 | 理由 |
|---|---|
| **スコープ変更の依頼** | coordinator の承認が必要 |
| **新規タスクの提案** | coordinator が優先度を判断する必要がある |
| **ブロッキング問題の報告** | coordinator が全体調整を行う必要がある |
| **θフェーズの進行判断** | coordinator の権限 |

### コミュニケーションルール

1. **重要な決定は coordinator にも報告する** — P2P で合意した内容でも、coordinator が把握している必要がある
2. **ブロードキャスト（全員宛）は最小限** — 全員のコンテキストウィンドウを消費するため、必要な相手だけに送信する
3. **契約変更は P2P だけで決めない** — contract の変更は coordinator と architect の承認が必要

---

## 6. セッション復旧

### 概要

CLIセッションが中断（ターミナルクローズ、ネットワーク切断、マシン再起動等）した場合、`.shiki/state/session-*.json` から状態を復旧できます。

### 自動復旧

```bash
# --resume フラグで自動リジューム
./scripts/start_cli_session.sh --resume
```

`start_cli_session.sh` は起動時に復旧ファイルの有無を確認し、見つかった場合はリジュームを提案します。

### 手動復旧

```bash
# 復旧プロンプトの生成
python3 scripts/recover_session.py

# Claude Code にパイプ
python3 scripts/recover_session.py | claude --prompt -

# 特定のセッションファイルを指定
python3 scripts/recover_session.py .shiki/state/session-20250101T120000.json
```

### 復旧プロンプトに含まれる情報

1. **セッション概要** — ID、モード、中断時刻、θフェーズ
2. **Git 状態** — ブランチ、コミット、未コミットファイル
3. **チーム再構築指示** — メンバー一覧と役割
4. **タスク状況** — 進行中/ブロック中/未着手/レビュー待ち
5. **θフェーズ状態** — 現在フェーズと exit_criteria
6. **バジェット状態** — 使用量と残量
7. **申し送り事項** — 前回セッションからのメモ

### 復旧後の手順

1. 復旧プロンプトの内容を確認する
2. `TeamCreate` でチームを再構築する
3. 進行中だったタスクのメンバーを再割当する
4. ブロック中タスクのブロッカーを解決する
5. θフェーズの exit_criteria 達成に向けて作業を継続する

### 詳細手順

詳しくは `docs/14_session_recovery.md` を参照してください。

---

## 7. バジェット監視

### 設定

`.shiki/config.yaml` でバジェット上限を設定します：

```yaml
github:
  budget:
    max_tokens_per_task: 100000
    max_tokens_per_session: 500000
    warn_threshold_pct: 80
```

**注意**: `budget` 設定は CLI/GitHub 両モードで共通です。

### 監視方法

#### coordinator の義務
- 各タスクの `budget.estimated_tokens` と `budget.actual_tokens` を追跡する
- セッション全体のトークン使用量を把握する
- 80%（`warn_threshold_pct`）に達したらチームに警告する

#### メンバーの義務
- タスク着手時に `budget.max_tokens` を確認する
- 上限に近づいたら最小限の変更で完了を目指す
- 超過しそうな場合は即座に coordinator に報告する

### バジェット超過時の対応フロー

```
1. メンバーが超過を報告（SendMessage → coordinator）
2. coordinator がタスク一覧を確認（TaskList）
3. 残りタスクの優先度を再評価
4. 低優先度タスクを blocked に変更（TaskUpdate）
5. ユーザーにバジェット追加の承認を求める
```

---

## 8. Dual Engine（Claude + Codex 連携）

### 概要

CLI モードでは Codex MCP Server を通じて、Claude Agent Teams と Codex の Dual Engine 実行が可能です。タスクの特性に応じて最適なエンジンを自動選択し、Fallback Chain で耐障害性を確保します。

### セットアップ

Codex CLI がインストール・認証済みであれば、`.claude/mcp.json` に定義済みの Codex MCP Server が自動的に利用可能になります。

```bash
# 1. Codex CLI のインストール
npm i -g @openai/codex

# 2. 認証（いずれか一方）
codex login                    # 方式A: Pro/Plus plan（推奨・サブスク枠内）
export OPENAI_API_KEY="sk-..." # 方式B: API key（従量課金）

# 3. MCP サーバーの登録（プロジェクト単位で追加する場合）
claude mcp add --transport stdio --scope project codex -- codex mcp-server

# 4. 認証状態の確認
codex login status
```

MCP サーバーは `codex login` の認証情報（`~/.codex/`）を自動継承します。Pro plan 使用時は `OPENAI_API_KEY` の設定は不要です。

### エンジン振分

coordinator が θ₃ ALLOCATE フェーズでエンジンを振り分けます：

```bash
# 全タスクに最適エンジンを自動割当
python3 scripts/engine_router.py --all

# 特定タスクの割当確認
python3 scripts/engine_router.py .shiki/tasks/T-0001.json

# ドライランで結果を確認（ファイル更新なし）
python3 scripts/engine_router.py --all --dry-run
```

### エンジン選択の基準

| タスク特性 | エンジン | 理由 |
|-----------|---------|------|
| Contract が明確に定義済み | Codex | 仕様→実装が機械的 |
| 単一ファイル・関数実装 | Codex | スコープが狭い |
| テスト生成 | Codex | パターン生成が得意 |
| 複数ファイル横断の変更 | Claude | 文脈理解が必要 |
| リファクタリング | Claude | 判断力が必要 |
| 設計判断を含む | Claude | トレードオフ評価 |

簡易ルール: **「仕様が Contract に書ける → Codex」「判断・議論が必要 → Claude」**

### executor の Codex MCP 委託フロー

executor は `engine=codex` のタスクを受け取った場合、Codex MCP 経由で実装を委託します：

```
1. タスク仕様を整理（title, acceptance, contract_ref, target_files）
2. Codex MCP にプロンプトを送信
3. Codex の出力をレビュー
4. exec verify で受け入れ条件を検証
5. 失敗 → executor が自分で直接実装にフォールバック
```

### Fallback Chain

Primary engine が失敗した場合、自動的に Secondary engine で再試行します：

```
codex 失敗 → executor が Claude で直接実装
claude 失敗 → Codex MCP に委託
両方失敗   → coordinator にエスカレーション
```

`execution_path` フィールドに "primary" または "fallback" が記録され、どちらのエンジンで完了したかを追跡できます。

---

## 9. θフェーズ追跡

### 6フェーズの概要

| フェーズ | 名称 | 主な活動 |
|---|---|---|
| θ₁ | UNDERSTAND | ゴール理解、スコープ確定 |
| θ₂ | GENERATE | 設計、タスク分解、契約策定 |
| θ₃ | ALLOCATE | DAG構築、ロール割当 |
| θ₄ | EXECUTE | 実装・テスト |
| θ₅ | VERIFY | レビュー、セキュリティ検証 |
| θ₆ | INTEGRATE | 統合、マージ、リリース |

### CLI モードでの追跡方法

1. **coordinator がフェーズを管理**
   - 現在のフェーズをチームに共有（`SendMessage`）
   - exit_criteria の達成状況を定期的に評価
   - フェーズ遷移時にレポートを作成

2. **メンバーがフェーズを意識**
   - タスクの `theta_phase` フィールドを確認
   - レポートに現在フェーズを記録
   - フェーズに適さない作業を検出したら報告

3. **monitor がフェーズ整合性を検証**
   - exit_criteria 未達でのフェーズ進行を検出
   - フェーズ逆行の適切性を確認

### フェーズ進行の判断基準

各フェーズの `exit_criteria` は `.shiki/config.yaml` の `convergence` セクションに定義されています。すべての criteria を満たした場合にのみ次フェーズに進行できます。

品質不足の場合は前フェーズに戻り再反復します（詳細は `docs/13_convergence_model.md` を参照）。

### tmux での状態表示

tmux の monitor ウィンドウ（ウィンドウ2）では、セッション状態ファイルの内容がリアルタイムで表示されます。θフェーズの進行状況もここで確認できます。

```bash
# monitor ウィンドウに切替
Ctrl+B → n   # 次のウィンドウへ
```
