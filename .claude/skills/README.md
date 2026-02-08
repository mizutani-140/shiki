# Shiki（式） Skills

Shiki（式）プロジェクトで使用可能なスキル集です。各スキルは特定のタスクを効率的に実行するためのベストプラクティスとガイドラインを提供します。

## スキル一覧

### 開発スキル

| スキル | 説明 | トリガー |
|--------|------|----------|
| [typescript-development](./typescript-development.md) | TypeScript開発のベストプラクティス | `typescript`, `ts` |
| [mcp-server-development](./mcp-server-development.md) | MCP Server開発ガイド | `mcp`, `server` |
| [tdd-workflow](./tdd-workflow.md) | テスト駆動開発ワークフロー | `tdd`, `test` |
| [debugging](./debugging.md) | デバッグ・トラブルシューティング | `debug`, `error`, `fix` |

### ワークフロースキル

| スキル | 説明 | トリガー |
|--------|------|----------|
| [issue-driven-development](./issue-driven-development.md) | Issue駆動開発 | `issue`, `idd` |
| [git-workflow](./git-workflow.md) | Gitワークフロー・コンベンション | `git`, `branch`, `commit` |
| [code-review](./code-review.md) | コードレビューガイド | `review`, `pr` |
| [documentation](./documentation.md) | ドキュメント生成 | `docs`, `readme` |

### 品質・セキュリティスキル

| スキル | 説明 | トリガー |
|--------|------|----------|
| [security-audit](./security-audit.md) | セキュリティ監査 | `security`, `audit` |
| [performance](./performance.md) | パフォーマンス最適化 | `performance`, `optimize` |
| [ci-cd](./ci-cd.md) | CI/CDパイプライン | `ci`, `cd`, `deploy` |

### ビジネススキル

| スキル | 説明 | トリガー |
|--------|------|----------|
| [product-planning](./product-planning.md) | プロダクト企画 | `product`, `mvp` |
| [market-research](./market-research.md) | 市場調査 | `market`, `research` |
| [content-creation](./content-creation.md) | コンテンツ作成 | `content`, `blog` |

## 使い方

### Claude Code内で使用

Agentはタスクの種類に応じて適切なスキルを参照し、ベストプラクティスに従って作業を実行します。

### Shiki（式）との統合

- executor エージェントはタスクに応じてスキルを参照する
- coordinator エージェントはタスク割当時に推奨スキルを指定できる
- monitor エージェントはスキルの品質基準に基づいてレビューする

## ディレクトリ構造

```
.claude/skills/
  README.md
  typescript-development.md
  mcp-server-development.md
  tdd-workflow.md
  debugging.md
  issue-driven-development.md
  git-workflow.md
  code-review.md
  documentation.md
  security-audit.md
  performance.md
  ci-cd.md
  product-planning.md
  market-research.md
  content-creation.md
```

## 関連リンク

- [エージェント定義](../agents/)
- [権限モデル](../../docs/12_authority_model.md)
- [exec verify プロトコル](../instructions.md)
