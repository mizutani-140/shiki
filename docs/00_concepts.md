# コンセプト：Shiki（式） の設計思想

## 目的
「LLMを使って開発する」ではなく、**開発そのものを“プロセス化”し、再現性を上げる**ことが目的です。

- ユーザー：ゴール（何を達成したいか）と制約（何をしてはいけないか）を明文化
- LLM：Plan → Team → Execute を、**状態機械**と**成果物（ファイル/PR/CI）**で管理
- GitHub：状態と監査ログ（Issue/PR/Checks/Labels）
- Hooks：確定的制御（安全・品質）をLLMから切り離す
- Contracts：インターフェース合意をファイルで固定し、並列開発の破綻を防ぐ

---

## 基本原則（5つ）

1. **Goal first（ゴール起点）**  
   まず “ユーザー価値/完了条件” を明文化し、以降の判断をブレさせない。

2. **Plan before touch（触る前にPlan）**  
   いきなり編集しない。Plan段階では読み取り中心で設計を固める。

3. **Contracts over conversations（会話より契約）**  
   口頭合意でなく、契約ファイル（contracts/）で握る。

4. **Tasks are the unit of autonomy（自律の最小単位はタスク）**  
   「担当」「状態」「受け入れ条件」「出力」をタスクファイルで明確化し、勝手な逸脱を防ぐ。

5. **Deterministic guardrails（確定的ガードレール）**  
   フォーマッタ/リンタ/危険コマンドブロックなど、LLMの気分に任せない（Hooks/CIで強制）。

---

## 役割分担の基本方針

- Claude Code Agent Teams：  
  - 設計、分解、統合、レビュー、仮説競合デバッグ、クロスレイヤー調整
- Codex：  
  - 仕様が明確な実装、テスト生成、テスト実行→修正のループ
- GitHub Actions：  
  - 非同期の“自動実行”を担う（Issue→PR→CI→修正）

> Agent Teamsはローカルの対話型環境で最大効果が出ます。  
> GitHub Actions上では、ワークフロー（複数ジョブ）で「疑似チーム」を作ると安定します。

---

## 状態機械（最低限）

### Plan
- `proposed` → `approved`

### Contract
- `proposed` → `agreed` → `implemented` → `verified` → `integrated`

### Task
- `pending` → `in_progress` → `review` → `completed`
- 例外：`blocked` / `failed`

この状態は **GitHub（Issue/PR/Labels/Checks）** と **.shiki/** の両方に残し、どちらかが落ちても復旧できるようにします。

---

## 追加原則（デュアルモード）

6. **Mode Duality（モード二重性）**
   CLIモード（Agent Teams ネイティブ）とGitHubモード（Issue/Label/Worktree駆動）は共通のコア（`.shiki/`、契約、タスク状態機械、θ収束モデル）を共有しつつ、それぞれの実行環境に最適化された操作を提供する。CLIモードはローカルの対話的開発に、GitHubモードは非同期・CI/CD統合型の開発に適している。どちらのモードで作成されたタスクや契約も、もう一方のモードで参照・継続が可能である。

7. **Authority Hierarchy（権限階層）**
   3層の権限モデル（coordinator / executor / monitor）により、計画する者・実装する者・検査する者を分離する。coordinator は Delegate Mode で実装に直接触れず調整に専念し、executor は承認を得た範囲内でのみ変更を行い、monitor は読取専用で独立した品質評価を行う。この分離により、無秩序な変更を防ぎ、プロセスの予測可能性と安全性を確保する。

8. **Convergence（収束）**
   θフェーズ（θ₁ UNDERSTAND → θ₂ GENERATE → θ₃ ALLOCATE → θ₄ EXECUTE → θ₅ VERIFY → θ₆ INTEGRATE）により、開発プロセスを体系的にゴール達成へと収束させる。各フェーズには明確な exit_criteria があり、基準を満たさない場合は前フェーズに戻って再反復する。これにより、品質を犠牲にした場当たり的な進行を防ぎ、確実なゴール達成を実現する。

