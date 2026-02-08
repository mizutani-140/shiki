# 役割とTeam Builder（どういうAIが必要かを決める）

## 3層権限モデル

すべての役割は3つの権限層のいずれかに属します：

| 層 | 説明 | CLIエージェント | GitHubジョブ |
|---|---|---|---|
| **coordinator** | 計画・調整・承認。実装ファイル編集不可 | coordinator.md | orchestrator/planner |
| **executor** | 実装・テスト。承認後のみStandard Mode | executor.md | worker |
| **monitor** | 読取専用。ブロッキングタスク作成権限あり | monitor.md | reviewer |

---

## まず"固定で必要な役割"（最低限）
プロジェクトの種類に関係なく、ほぼ必ず必要です。

1. **TeamLeader** (coordinator)
   - チーム統括、計画策定、タスク割当、θフェーズ管理
2. **Goal Clarifier / PM** (coordinator)
   - ゴールを「テスト可能な受け入れ条件」に落とす
3. **Architect** (coordinator)
   - 依存関係、境界（Contracts）、データモデルを設計
4. **Integrator / Release Captain** (coordinator)
   - PR統合、CI安定化、リリース手順
5. **QA / Test Engineer** (executor)
   - 受け入れテスト、エッジケース、回帰テスト
6. **Security Reviewer** (monitor)
   - 権限/認証/入力検証/秘密情報の扱い

---

## 次に"スタックで追加する役割"（例）

### Webアプリ（典型）
- Frontend Dev (executor)：UI/UX、状態管理
- Backend Dev (executor)：API、DB
- SRE/DevOps (executor)：Docker、IaC、監視

### データ/ML系
- Data Engineer (executor)：ETL、スキーマ
- ML Engineer (executor)：学習/推論、評価

### ライブラリ/SDK系
- API Designer (coordinator)：公開APIの互換性
- Documentation Writer (executor)：README/サンプル

---

## 権限層マッピング

```
coordinator (計画・調整)
├── TeamLeader     → Delegate Mode強制
├── GoalClarifier  → Plan Mode
├── Architect      → Plan Mode
├── ReleaseCaptain → Plan Mode
└── APIDesigner    → Plan Mode

executor (実装・テスト)
├── Implementer    → Plan→Standard Mode
├── FrontendDev    → Plan→Standard Mode
├── BackendDev     → Plan→Standard Mode
├── QA             → Plan→Standard Mode
└── DevOps         → Plan→Standard Mode

monitor (監視・検査)
└── SecurityReviewer → Plan Mode（常時読取専用）
```

---

## Team Builder の実装（おすすめの決め方）

### A) ルールベース（堅い）
リポジトリをスキャンし、存在するファイルで推定します。

- `package.json` → Frontend/Node
- `pyproject.toml` / `requirements.txt` → Python
- `Dockerfile` / `compose.yml` → DevOps
- `terraform/` / `helm/` → Infra
- `openapi.yaml` → API Designer

このZipには簡易スクリプトが入っています：
- `scripts/detect_stack_and_roles.py`（authority_layer自動割当対応）

### B) Planベース（柔らかい）
Planモードで「このゴールを達成するのに必要な役割」をLLMに提案させる方法です。
ただし、増えすぎやすいので **上限を決める**のがコツです。

例：最大5名まで、というルールを入れる。

---

## ファイルオーナーシップ（競合を避ける）
役割ごとに触る領域（ディレクトリ）を決めると、Agent Teams の並列性が活きます。

例：
- Frontend：`web/` `src/ui/`
- Backend：`api/` `src/server/`
- Tests：`tests/` `e2e/`
- Infra：`infra/` `.github/`

このルールは `roles/roles.yaml` に書きます。
