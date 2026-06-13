# 提案 0001: 記憶ループ（Memory Loop）— Spec Freeze レビュー文書

> **ステータス**: 凍結前レビュー v2（操作者承認待ち）。
> v1 への操作者レビュー（Request changes、2026-06-12）の freeze blocker B1–B10 と
> major comment M1–M5 を全件反映済み。承認後は plan JSON の `spec_freeze` ブロックと
> PRD Issue に引き継がれ、本文書は提案アーカイブとして残る。
> スコープ変更は以後 Spec Amendment（操作者承認の範囲限定再グリル）のみ。

## 改訂履歴

| 版 | 日付 | 内容 |
| --- | --- | --- |
| v1 | 2026-06-12 | 初版（Requirements Definition 出力） |
| v2 | 2026-06-13 | 操作者レビュー反映: 状態別スキーマ（B1）/ 遷移規則と memory_transition ledger（B2）/ 構造化 evidence と local 必須（B3）/ HITL 実効境界（B4）/ 蒸留候補は suggestion に限定（B5）/ rule の失効・supersede（B6）/ Consult の決定的選択と MEM id 出力（B7）/ CCA 表現の精密化（B8）/ scorecard 算出定義（B9）/ redaction（B10）/ T1 自動マージ除外（M1）/ mirror 採用理由（M2）/ CCA fail 構造化検出（M3）/ area 拡張 + tags（M4）/ fail-open 範囲の限定（M5）/ acceptance checks 12 件追加 |

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

**設計の最重要原則**（操作者レビューより）:
記憶ループは一度凍結すると**誤学習を永続化しやすい領域**である。したがって
「未検証の仮説を distilled rule にしない」「間違った rule を失効できる」を
スキーマと遷移規則のレベルで機械的に保証する。

### 名前衝突の注記（重要）

外部 AI 生成の参考レポートは **shikijs/shiki（公開 OSS のシンタックスハイライター）**
を分析対象にしていた。本 Goal の対象は **本リポジトリ（制御プレーン）** であり、
レポートの具体的改善箇所は**一切対象外**。採用するのは記事由来の概念のみ。

## 2. インタビューで確定した決定（6 件）

| # | 論点 | 決定 |
| --- | --- | --- |
| 1 | 対象 | mizutani-140/shiki プラットフォーム（shikijs/shiki ではない） |
| 2 | 記憶の所在と形状 | `.shiki/memories/` 新設。`raw → investigated → verified → distilled` のステータス昇格モデル |
| 3 | 昇格権限 | **3 段階分離**: raw = 全自動捕捉 / verified = 証拠参照の機械検証 / distilled = **操作者承認のみ** |
| 4 | Consult 注入点 | ハンドオフ自動注入（H-12）+ セッション開始読込。CCA の独立性は不変（§3.5 の精密化を参照） |
| 5 | 蒸留トリガー | Goal 完了時にスコアカード + **蒸留候補 suggestion**（status は変更しない、§3.6） |
| 6 | スコアカード | goal complete レポートに ledger 由来の機械集計を同梱（§3.6 スキーマ） |

## 3. 仕様詳細

### 3.1 記憶エントリ（`.shiki/memories/MEM-*.json`）

Memory entry は `schema_version: 1` を持つ。**Memory file は current-state document**
であり、状態遷移の監査履歴は ledger の `memory_transition` イベントに記録する
（M2: ledger が append-only log、memories が current state、という役割分担。
`filename == id` はバリデータが強制する）。

```json
{
  "id": "MEM-20260612T143733349559Z-fe72ae6a",
  "schema_version": 1,
  "status": "raw | investigated | verified | distilled",
  "area": "mergegate | cca | locks | runner | loop | planning | memory | contracts | manifest | migrations | handoff | validator | docs | other",
  "applies_to": ["locks", "mergegate", "planning"],
  "tags": ["handoff", "lock-declaration"],
  "claim": "短い事実の主張（1 文）",
  "evidence": [
    { "kind": "ledger", "path": ".shiki/ledger/L-....json" },
    { "kind": "report", "path": ".shiki/reports/R-....json" },
    { "kind": "exec",   "path": ".shiki/runner/EXEC-....json" },
    { "kind": "pr_check", "pr": 126, "check": "MergeGate metadata check", "required": false }
  ],
  "source": { "kind": "repair | loop_stop | cca_fail | runner_fail | manual", "goal_id": "G-...", "task_id": "T-..." },
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "investigation": { "summary": "調査内容", "refs": [] },
  "verification": { "verified_at": "ISO-8601", "validator": "validate_memory", "evidence": [] },
  "last_verified": "ISO-8601",
  "rule": "（distilled のみ）一般化された開発ルール 1 文",
  "approved_by": "（distilled のみ）操作者名",
  "approved_at": "ISO-8601",
  "approval_ledger": ".shiki/ledger/L-....json",
  "active": true,
  "supersedes": [],
  "superseded_by": null,
  "revoked_at": null,
  "revoked_by": null,
  "revocation_ledger": null,
  "redaction": { "status": "clean | redacted | skipped", "notes": "" }
}
```

**status 別の必須 / 禁止フィールド（B1）**:

| status | 必須 | 禁止 |
| --- | --- | --- |
| raw | `id`, `schema_version`, `status`, `area`, `claim`, `source`, `created_at`, `updated_at`, `redaction` | `rule`, `approved_by`, `approved_at`, `approval_ledger`, `last_verified` |
| investigated | raw の必須 + `investigation.summary`, `investigation.refs` | `rule`, `approved_by`, `approved_at`, `approval_ledger` |
| verified | investigated の必須 + `verification.verified_at`, `verification.evidence`, `last_verified` | `rule`, `approved_by`, `approved_at`, `approval_ledger` |
| distilled | verified の必須 + `rule`, `approved_by`, `approved_at`, `approval_ledger`, `active` | なし |

- **ID プレフィックスは `MEM`**（`M-` は migration ID と衝突）。`new_control_id("MEM")` 動作確認済み。
- スキーマは `.shiki/schemas/memory-entry.schema.json` を新設。バリデータの bounded
  JSON Schema サブセットで構造を定義し、status 別必須/禁止・遷移・evidence 実在の
  クロスファイル検証は Python 側 `validate_memory` が fail-closed で行う。
- `area` は粗い分類（enum、上記 14 値）、`tags` は細かい分類（自由文字列配列）、
  `applies_to` は Consult 対象 area の配列（M4）。

### 3.2 状態クラスと配管（原子的に同一 PR で）

- `.shiki/memories` は **mirror クラスを再利用**。理由（M2）: memories は
  append-only log ではなく **current-state document** として扱い、監査証跡は
  ledger の `memory_transition` イベントが担保する。新 state class の新設は
  7 ファイル連鎖（state_classes / policies / README / state-classes.md / 検証 needle）
  のため回避する。
- 同一 PR で必須の lockstep 変更:
  1. `.shiki/manifest.json` の `directories` + `install.create_directories`
  2. `scripts/shiki_contracts.py` の `TARGET_STATE_DIRECTORIES`（**順序敏感タプル等値** — 両ファイル同一位置）
  3. `.shiki/README.md` レイアウトブロック再生成（byte-match 検証）
  4. migration `M-20260613-0001-memories` の**登録 + 適用**（pending は fail-closed）
  5. **ledger 契約の拡張**: `memory_transition` を `LEDGER_TYPES`（validate_shiki）と
     `.shiki/schemas/ledger.schema.json` の type enum に**同時追加**
     （`validate_contract_schema_consistency` が集合等値を強制。
     既存ターゲットへは再インストール/migration で展開 — 本 Goal は platform のみ）

### 3.3 自動捕捉（Fail）— 失敗許容と redaction

**fail-open の範囲は「書き込み失敗がループを止めない」ことに限定する（M5）**:

```text
- capture は書き込み前にスキーマ検証し、不正な entry は書かない（warning のみ）
- バリデータは、存在する memory file の schema violation を fail-closed にする
- promotion は schema + 遷移規則 + evidence 実在を fail-closed にする
```

**redaction policy（B10）**:

```text
- 自動 capture は stdout/stderr の本文・環境変数・secret-like token を memory にコピーしない
- memory には ledger / EXEC / report への参照（structured evidence）と短い claim のみ保存する
- secret-like pattern は claim 生成前に redact し、redaction.status を記録する
- redaction 不能と判断した場合、memory file を書かず warning のみ出す
```

| 捕捉点 | 場所 | 記録内容 |
| --- | --- | --- |
| repair packet 作成 | `create_repair_packet`（ガード通過後） | failing items への参照 / attempt / task / goal |
| loop の stop | `execute_action` の**実行後 result**（merge 失敗・unblock 失敗の事後 stop 変換も捕捉） | stop 種別 / reason |
| CCA fail | loop の checks スナップショット（**`gh pr checks --json` の structured bucket を source of truth とする。文字列パースは fallback のみ** — M3） | check 状態 / rerun 回数 |
| runner 失敗 | `record_runner_result`（returncode != 0 の共通チョークポイント） | EXEC 記録への参照 |

### 3.4 昇格（Investigate / Verify / Distill）— 遷移規則と HITL 境界

**許可される状態遷移（B2）**:

```text
raw -> investigated
investigated -> verified
verified -> distilled

status skipping は禁止する。
raw から distilled / verified への直接昇格は禁止する（validator が拒否）。
各状態遷移は memory_transition ledger イベントを必ず生成する。
in-place update 時は updated_at を必ず更新する。
```

**evidence 規則（B3）**:

```text
verified 昇格には、少なくとも 1 つの local evidence（kind: ledger | report | exec）が必須。
GitHub の pr_check evidence は補助証拠であり、remote evidence のみでは verified に昇格できない。
validate_memory は local evidence の path 実在を fail-closed で検証する。
```

**HITL 実効境界（B4）**:

```text
autonomous loop / runner / CCA は distilled rule を作成・変更・revoke してはならない。
distill / revoke / supersede は operator approval ledger イベントを要求する。
shiki memory distill --approve は autonomous execution context（runner / loop が設定する
実行コンテキスト環境変数）では拒否する。
status=distilled, rule, approved_by, approval_ledger, active, superseded_by を変更する
PR は自動マージ対象外とする（goal loop は当該 diff を検出した場合 stop_guardian で停止する）。
```

CLI: `shiki memory capture | list | investigate | promote | distill | revoke | supersede`
（新モジュール `scripts/shiki_memory.py`、CLI モジュール境界契約 MB-01..05 に準拠）。

### 3.5 Consult — 決定的な参照（B6/B7/B8）

**注入対象の選択（決定的セマンティクス）**:

```text
Consult は active=true かつ superseded_by=null かつ revoked_at=null の distilled rule のみ注入する。
revoked / superseded rule は注入しない。
選択は task.area / goal.area / applies_to / tags の一致で決定的に行う。
順序は last_verified desc, id asc の安定ソートとする。
ハンドオフには rule text と MEM id を併記する（注入されたルールの追跡可能性）。
```

- `write_task_handoff` が `## Distilled Rules` セクションを**常時出力**
  （該当ゼロなら "none applicable" を明示）。
- **ハンドオフはディスパッチ時に常時再生成**（キャッシュ陳腐化の解消）。
- `/shiki` と CLAUDE.md の Session Start に distilled ルール読込を追加。

**CCA との関係（B8 — v1 の「判定基準は不変」を精密化）**:

```text
CCA の独立性・実装者との分離・core prompt は変更しない。
ただし checklists.md には H-12 を追加する（blocking when applicable）:
  「ハンドオフに Distilled Rules セクションが存在し、applicable / none applicable が明示されている。」
CCA は H-12 について、セクションの存在と applicable/none applicable の明示のみを確認する。
distilled rule の内容自体は CCA の acceptance criteria にしない
（ルールと checklists の二重権威を作らない）。
```

### 3.6 スコアカード + 蒸留候補 suggestion（Goal 完了時）

**蒸留候補は suggestion に限定（B5 — 状態機械との整合）**:

```text
Goal complete 時の蒸留候補生成は report 内の suggestions 配列に留める。
suggestion は memory の status を変更しない。
distilled rule として保存するには、対象 MEM が investigated -> verified を通過し、
verified 状態でなければならない（raw / investigated からの直接 distill は validator が拒否）。
suggestion は「この raw/verified メモリ群からこのルールが蒸留できそうだ」という
操作者向け提案であり、採用には通常の investigate -> promote -> distill フローを要する。
```

**scorecard スキーマと算出規則（B9）**:

```json
{
  "goal_id": "G-...",
  "generated_at": "ISO-8601",
  "window": { "from": "ISO-8601", "to": "ISO-8601" },
  "tasks": { "total": 0, "completed": 0, "failed": 0 },
  "repairs": { "total": 0, "by_area": {} },
  "cca_reruns": { "total": 0 },
  "loop_stops": { "total": 0, "by_reason": {} },
  "lock_amendments": { "total": 0 },
  "duration_ms": 0,
  "warnings": [],
  "suggestions": [
    { "from_memories": ["MEM-..."], "proposed_rule": "...", "note": "採用には verified 経由の distill が必要" }
  ]
}
```

```text
scorecard は ledger / task / report の既存 state からのみ算出する。
raw memory は scorecard の source of truth にしない（循環参照の回避）。
重複排除キーは ledger イベント id とする。
算出不能な項目は null ではなく 0 とし、missing source は warnings に記録する。
repair の per-goal 集計は packet の goal_id でフィルタする。
```

- スコアカードは**レポートファイル内**に書き、stdout に追加 JSON を流さない
  （`json_get_last` 互換維持）。生成は failure-tolerant（例外時はレポートのみ + warning）。

### 3.7 用語集（CONTEXT.md 追加、3 語）

- **Memory** — 失敗・調査・検証を経た知識の current-state エントリ。ledger（追記専用の生イベント）と区別。
- **Distilled Rule** — 操作者承認済みの一般化ルール。active な間だけハンドオフに自動注入される。
- **Scorecard** — Goal 完了時の ledger 由来機械集計。蒸留 suggestion の入力。

## 4. スコープ棚卸し（SF-02）

| 項目 | 内容 |
| --- | --- |
| 新規外部権限 | **なし**（既存の gh auth / claude / python3 / git のみ） |
| 新規シークレット | なし |
| Guardian レビュー | 必要（contracts / validator / mergegate は CODEOWNERS、アーキテクチャゲート該当） |
| 操作者の手動操作 | T1〜T3 の PR マージ承認（§5 のマージ方針）/ マージ後の `shiki install-global` 再実行 |
| ワークフロー YAML | **変更なし**（required checks 不変） |
| 機密情報 | §3.3 redaction policy により stdout/stderr 本文・secret-like token は memory に保存しない |

## 5. タスク分解案（凍結後に確定・自律実行）

```
T1: 状態基盤
    manifest + TARGET_STATE_DIRECTORIES + README 再生成 + migration（登録+適用）
    + LEDGER_TYPES/ledger schema への memory_transition 追加 + MEM プレフィックス
    + memory-entry スキーマ + validate_memory（状態別必須/禁止・遷移・evidence・HITL 境界）
    + shiki_memory.py + CLI 配線 + モジュール境界/インストーラ/テスト同期
T2: 捕捉 + スコアカード（T1 依存）
    4 捕捉点フック（redaction 付き・失敗許容）+ goal complete スコアカード
    + 蒸留候補 suggestion 生成
T3: Consult（T1 依存、T2 と並列可）
    ハンドオフ常時再生成 + Distilled Rules 注入（MEM id 付き決定的順序）+ H-12
    + セッション開始読込 + CONTEXT.md 用語 3 語 + docs/agents メモリ文書
```

**マージ方針（M1 — v1 の「risk: medium、自動マージ対象」を撤回）**:

```text
各タスクは shiki loop run で自律駆動可能（実装・PR 作成・CCA・修理まで）。
ただし T1〜T3 の PR はすべて自動マージ対象外とし、操作者がマージを承認する
（T1 は contracts/validator/mergegate を含む制御プレーン基盤変更で
 アーキテクチャゲート + Guardian レビュー必須。T2/T3 も goal complete /
 handoff / checklists に触れるため初回は operator merge とする）。
将来の運用においても、distilled rule を作成・変更・revoke する PR は
恒久的に自動マージ対象外とする（§3.4）。
```

## 6. Out of Scope

- CCA の独立性・core prompt の変更
- shikijs/shiki（公開 OSS）への作業
- 過去 Goal の遡及スコアカード（stop イベントは捕捉開始後のみ集計可能）
- bypassPermissions bounding（別 Goal、ADR 0008 記載のフォローアップ）
- 既存ターゲット（Shopify_Create）への適用（別 Goal。memory_transition の
  ledger enum 追加は再インストール/migration で届く）

## 7. 主要リスクと対応

| リスク | 対応 |
| --- | --- |
| **誤学習の永続化**（本領域の最重要リスク） | 遷移 skipping 禁止 + verified の local evidence 必須 + distilled の操作者承認 + active/supersede/revoke による失効（§3.1/3.4/3.5） |
| タプル順序不一致・pending migration・ledger enum 不整合で Validate fail-closed | T1 で lockstep 変更（§3.2 の 5 点）を単一 PR に集約。ローカルで `migrate apply --execute` 後に push |
| 不正な raw entry が無関係 PR をブロック | capture は書き込み前検証で不正 entry を書かない。validator は既存不正ファイルを fail-closed（M5） |
| coordinator が書いた memory が PR ヘッドに乗らない | raw 捕捉はループ実行側 checkout に書き、クローズアウト/reconcile PR で運搬。distilled 昇格は専用コミット + operator merge |
| `shiki:state` セマンティックロック拡張の影響半径 | 拡張せず、明示 `path:.shiki/memories/**` ロックで対応 |
| 蒸留候補生成が goal complete をクラッシュ | suggestion/スコアカード生成は failure-tolerant |
| 機密情報の memory への混入 | §3.3 redaction policy（本文コピー禁止・参照のみ・redact 不能なら書かない） |

## 8. 検証面（acceptance の基礎）

- `python3 scripts/validate_shiki.py`（manifest タプル / README レイアウト / migration /
  MEM ID / memory スキーマ / 遷移・evidence・HITL 強制 / ledger enum 整合）
- 新規 `scripts/test_shiki_memory_loop.sh` + `tests/test_memory_promotion.py`
- 既存スイート全緑（goal_loop / control_plane / manifest / state_classes /
  migrations / module_boundaries / validator_hardening / adversarial_state）
- 各 PR は PR-12 + スキル証跡 ledger を満たす

**acceptance checks（操作者レビューの 12 件を採用）**:

```text
- raw -> distilled の直接昇格が拒否される
- raw -> verified の直接昇格が拒否される
- verified で local evidence が 0 件の場合は拒否される
- distilled に approval_ledger が無い場合は拒否される
- autonomous context で distill --approve が拒否される
- revoked / superseded distilled rule が handoff に注入されない
- active distilled rule は handoff に MEM id 付きで deterministic order で注入される
- applicable rule が無い場合も "none applicable" が常時出力される
- goal complete の stdout が json_get_last 互換を維持する
- scorecard は ledger 由来で重複排除される
- capture が invalid memory を生成しようとした場合、warning のみで file を書かない
- runner stdout/stderr の raw body が memory にコピーされない
```

## 9. 凍結チェックリスト（SF ファミリー自己評価）

| 項目 | 状態 |
| --- | --- |
| SF-01 操作者の明示承認 | ⏳ **v2 のレビュー後、承認待ち** |
| SF-02 スコープ棚卸し | ✅ §4（redaction を含む） |
| SF-03 freeze の plan + ledger 記録 | ⏳ 承認後に plan JSON へ記録 |
| SF-04 凍結後の変更は Amendment のみ | ✅ 合意済み（ADR 0009） |
| SF-05 Assumption Log 運用 | ✅ 合意済み |
