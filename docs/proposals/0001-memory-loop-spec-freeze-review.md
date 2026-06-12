# 提案 0001: 記憶ループ（Memory Loop）— Spec Freeze レビュー文書

> **ステータス**: 凍結前レビュー（操作者承認待ち）。
> この文書は Requirements Definition セッション（2026-06-12、/shiki Goal モード）の
> 全決定を凍結承認のために提示するものです。承認後は plan JSON の `spec_freeze`
> ブロックと PRD Issue に引き継がれ、本文書は提案アーカイブとして残ります。
> スコープ変更は以後 Spec Amendment（操作者承認の範囲限定再グリル）のみ。

## 1. Goal

**Shiki プラットフォーム（mizutani-140/shiki）に記憶ループとスコアカードを実装する。**

Lance Martin “Designing loops with Fable 5” の Fail → Investigate → Verify →
Distill → Consult を、既存の制御プレーンに統合する。現状の対応表:

| 記事の概念 | 現状 |
| --- | --- |
| Goal / Rubric | ✅ Goal + チェックリスト + acceptance checks |
| 独立 Verifier | ✅ CCA（実装者と判定者の分離） |
| 自己修正ループ | ✅ `shiki loop`（0.2.0、bounded repair） |
| **記憶ループ** | ❌ ledger は生イベント追記のみ。検証済み事実→ルール蒸留→次回参照が無い |
| **スコアカード** | ❌ goal complete レポートは pass/fail のみ |

**解決する実害**: 0.2.0 開発中、同型の失敗が Goal を跨いで再発した
（ロック宣言漏れ ×3、CCA レース ×2）。学習は操作者個人のメモに残ったのみで、
プラットフォーム自身は次の Goal で同じ壁に当たる。

### 名前衝突の注記（重要）

外部 AI 生成の参考レポートは **shikijs/shiki（公開 OSS のシンタックスハイライター）**
を分析対象にしていた。本 Goal の対象は **本リポジトリ（制御プレーン）** であり、
レポートの具体的改善箇所（packages/core、issue #1275 等）は**一切対象外**。
採用するのは記事由来の概念（記憶ループ・スコアカード・蒸留）のみ。

## 2. インタビューで確定した決定（6 件）

| # | 論点 | 決定 |
| --- | --- | --- |
| 1 | 対象 | mizutani-140/shiki プラットフォーム（shikijs/shiki ではない） |
| 2 | 記憶の所在と形状 | `.shiki/memories/` 新設。エントリは `raw → investigated → verified → distilled` のステータス昇格モデル |
| 3 | 昇格権限 | **3 段階分離**: raw = 全自動捕捉 / verified = 証拠参照の機械検証（バリデータ強制）/ distilled ルール = **操作者承認のみ**（Spec Amendment と同格の HITL） |
| 4 | Consult 注入点 | ハンドオフ自動注入（新チェックリスト項目 H-12）+ `/shiki`・CLAUDE.md のセッション開始読込。**CCA 判定基準は不変**（ルールと checklists の二重権威を作らない） |
| 5 | 蒸留トリガー | **Goal 完了時**にスコアカード同梱 + raw failure から蒸留候補ルールを生成して操作者に提示（承認分のみ distilled へ） |
| 6 | スコアカード | goal complete レポートに機械集計を同梱（タスク数 / repair 回数 / CCA rerun 回数 / stop 回数 / ロック修正回数 等） |

## 3. 仕様詳細

### 3.1 記憶エントリ（`.shiki/memories/MEM-*.json`）

```json
{
  "id": "MEM-20260612T143733349559Z-fe72ae6a",
  "status": "raw | investigated | verified | distilled",
  "area": "mergegate | cca | locks | runner | loop | planning | other",
  "claim": "短い事実の主張（1 文）",
  "evidence": [".shiki/ledger/L-....json", "PR #126 check 'MergeGate metadata check'"],
  "rule": "（distilled のみ）一般化された開発ルール 1 文",
  "source": {"kind": "repair | loop_stop | cca_fail | manual", "goal_id": "G-...", "task_id": "T-..."},
  "approved_by": "（distilled のみ）操作者名",
  "last_verified": "ISO-8601"
}
```

- **ID プレフィックスは `MEM`**（`M-` は migration ID と衝突するため不可）。
  `new_control_id("MEM")` は無変更で動作確認済み。
- スキーマは `.shiki/schemas/memory-entry.schema.json` を新設
  （バリデータの bounded JSON Schema サブセット内で構造を定義し、
  クロスファイル検証は Python 側 `validate_memory` で行う — ledger evidence と同じ流儀）。

### 3.2 状態クラスと配管（原子的に同一 PR で）

- `.shiki/memories` は **mirror クラスを再利用**（新クラス新設は state_classes /
  policies / README / state-classes.md など 7 ファイル連鎖のため回避）。
- 同一 PR で必須の lockstep 変更:
  1. `.shiki/manifest.json` の `directories` + `install.create_directories`
  2. `scripts/shiki_contracts.py` の `TARGET_STATE_DIRECTORIES`
     （**順序敏感のタプル等値比較** — 両ファイルで同一位置に追加）
  3. `.shiki/README.md` のレイアウトブロック再生成（byte-match 検証あり）
  4. migration `M-20260612-0002-memories` の**登録 + 適用**
     （バリデータは pending migration を fail-closed）
- ledger とは異なり**昇格のための in-place 更新を許容**する
  （append-only ではない。filename==id 検証とは両立）。

### 3.3 自動捕捉（Fail）— 失敗許容

捕捉フックは**例外を握りつぶしループを巻き込まない**（capture failure は warning）。

| 捕捉点 | 場所 | 記録内容 |
| --- | --- | --- |
| repair packet 作成 | `create_repair_packet`（ガード通過後） | failing items / attempt / task / goal |
| loop の stop | `execute_action` の**実行後 result**（merge 失敗・unblock 失敗の事後 stop 変換も捕捉） | stop 種別 / reason |
| CCA fail | loop の checks 取得時に `CCA verdict: fail` を観測した時点 | check 状態 / rerun 回数 |
| runner 失敗 | `record_runner_result`（returncode != 0、runner claude / codex / execute の共通チョークポイント） | EXEC 記録参照 |

### 3.4 昇格（Investigate / Verify / Distill）

```
raw ──(調査メモ追記)──> investigated ──(機械検証)──> verified ──(操作者承認)──> distilled
```

- `shiki memory promote MEM-x --to verified`: **evidence の各参照が実在する**
  （ledger ファイル / PR / チェック名）ことをバリデータが強制。証拠なき昇格は拒否。
- `shiki memory distill MEM-x --rule "..." --approve`: 操作者実行のみ。
  `approved_by` を記録し、承認 ledger エントリを生成。
  バリデータは「distilled エントリに approved_by + 承認 ledger 参照が無ければ fail」。
- CLI: `shiki memory capture|list|promote|distill`（新モジュール `scripts/shiki_memory.py`、
  CLI モジュール境界契約 MB-01..05 に準拠）。

### 3.5 Consult — 参照の機械化

- `write_task_handoff` が **area 一致の distilled ルール**を `## Distilled Rules`
  セクションとして埋め込む。**該当ゼロでもセクションを常時出力**（"none applicable"）。
- **ハンドオフはディスパッチ時に常時再生成**（現状の「無ければ書く」キャッシュでは
  初回ディスパッチ後に蒸留されたルールが届かない — C&I で特定した欠陥の解消）。
- checklists.md に **H-12**: 「適用可能な蒸留ルール（または該当なしの明示）が
  ハンドオフに含まれている。」**blocking when applicable**
  （過去のハンドオフを遡及で落とさない）。
- `/shiki` コマンドと CLAUDE.md の Session Start に
  「`.shiki/memories/` の distilled ルールを読む」を追加。
- **CCA の判定基準・プロンプトは不変**。

### 3.6 スコアカード + 蒸留提案（Goal 完了時）

- `cmd_goal_complete` がレポート（`R-*.json`）に `scorecard` を同梱:
  `{tasks, repairs, cca_reruns, loop_stops, lock_amendments, duration}` 等の機械集計。
- 同時にその Goal の raw メモリから**蒸留候補**を生成し操作者に提示
  （承認フローは 3.4 の `distill --approve`）。
- ループの stdout 契約を壊さない: スコアカードは**レポートファイル内**に書き、
  追加の JSON ドキュメントを stdout に流さない
  （`json_get_last` 互換性 — C&I で特定）。

### 3.7 用語集（CONTEXT.md 追加、3 語）

- **Memory** — 失敗・調査・検証を経た知識のエントリ。ledger（生イベント）と区別。
- **Distilled Rule** — 操作者承認済みの一般化ルール。ハンドオフに自動注入される。
- **Scorecard** — Goal 完了時の機械集計。蒸留の入力。

## 4. スコープ棚卸し（SF-02）

| 項目 | 内容 |
| --- | --- |
| 新規外部権限 | **なし**（既存の gh auth / claude / python3 / git のみ） |
| 新規シークレット | なし |
| Guardian レビュー | 必要（contracts / validator / mergegate は CODEOWNERS パス、アーキテクチャゲート該当）— 各 PR で操作者が承認 |
| 操作者の手動操作 | マージ後の `shiki install-global` 再実行（インストール済み `/shiki` コマンドの更新。エージェントの自己変更制限により自動実行不可） |
| ワークフロー YAML | **変更なし**（required checks 不変） |

## 5. タスク分解案（凍結後に確定・自律実行）

```
T1: 状態基盤
    manifest + TARGET_STATE_DIRECTORIES + README 再生成 + migration（登録+適用）
    + MEM プレフィックス（shiki_tasks の 2 つの dict）+ memory-entry スキーマ
    + validate_memory（昇格強制含む）+ shiki_memory.py + CLI 配線
    + モジュール境界/インストーラ/テスト同期
T2: 捕捉 + スコアカード（T1 依存）
    4 捕捉点フック（失敗許容）+ goal complete スコアカード + 蒸留候補生成
T3: Consult（T1 依存、T2 と並列可）
    ハンドオフ常時再生成 + Distilled Rules 注入 + H-12 + セッション開始読込
    + CONTEXT.md 用語 3 語 + docs/agents メモリ文書
```

各タスク = 1 PR、`shiki loop run` で自律駆動（risk: medium、自動マージ対象）。

## 6. Out of Scope

- CCA 判定基準・プロンプトの変更
- shikijs/shiki（公開 OSS）への作業
- 過去 Goal の遡及スコアカード（stop イベントは捕捉開始後のみ集計可能）
- bypassPermissions bounding（別 Goal、ADR 0008 記載のフォローアップ）
- 既存ターゲット（Shopify_Create）への適用（0.2.0 適用と同様、別 Goal）

## 7. 主要リスクと対応（C&I スイープより）

| リスク | 対応 |
| --- | --- |
| タプル順序不一致・pending migration で Validate が fail-closed | T1 で lockstep 変更を単一 PR に集約。ローカルで `migrate apply --execute` 後に push |
| 不正な raw エントリが無関係 PR をブロック | 捕捉コードは書き込み前にスキーマ検証し、不正なら警告ログのみ（fail-open at capture, fail-closed at promotion） |
| coordinator が書いた memory が PR ヘッドに乗らない | raw 捕捉はループ実行側の checkout に書き、クローズアウト/reconcile PR で運搬（既存慣行）。distilled 昇格は専用コミット |
| `shiki:state` セマンティックロック拡張の影響半径 | 拡張せず、各タスクの明示 `path:.shiki/memories/**` ロックで対応 |
| 蒸留候補生成が goal complete をクラッシュ | スコアカード/候補生成は failure-tolerant（例外時はレポートのみ生成） |

## 8. 検証面（acceptance の基礎）

- `python3 scripts/validate_shiki.py`（manifest タプル / README レイアウト /
  migration / MEM ID / memory スキーマ / 昇格強制）
- 新規 `scripts/test_shiki_memory_loop.sh`（capture→promote→distill→handoff 注入の
  エンドツーエンド + 証拠なき昇格の拒否 + 未承認 distilled の拒否）
- `tests/test_memory_promotion.py`（昇格状態機械の単体テスト）
- 既存スイート全緑（特に goal_loop / control_plane / manifest / state_classes /
  migrations / module_boundaries / validator_hardening）
- 各 PR は PR-12（pre-PR code-review 証跡）+ スキル証跡 ledger を満たす

## 9. 凍結チェックリスト（SF ファミリー自己評価）

| 項目 | 状態 |
| --- | --- |
| SF-01 操作者の明示承認 | ⏳ **本文書のレビュー後、承認待ち** |
| SF-02 スコープ棚卸し | ✅ §4 |
| SF-03 freeze の plan + ledger 記録 | ⏳ 承認後に plan JSON へ記録 |
| SF-04 凍結後の変更は Amendment のみ | ✅ 合意済み（決定 7、ADR 0009） |
| SF-05 Assumption Log 運用 | ✅ 合意済み |
