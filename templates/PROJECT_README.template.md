# {{PROJECT_NAME}}

> TODO: プロジェクトの説明を追加してください

---

## Development

このプロジェクトは [Shiki（式）](https://github.com/mizutani-140/shiki) フレームワークを使用した AI 自律開発プロジェクトです。

### Prerequisites

- Python 3.10+
- Claude Code CLI (`claude --version`)
- tmux（CLI モード推奨）

### Start a Development Session

```bash
# 開発セッションを開始
./bin/shiki start

# または手動で:
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
claude
```

### Project Goal

[GOAL.md](./GOAL.md) を参照してください。

### Project Structure

```
GOAL.md             プロジェクトのゴールと受け入れ条件
CLAUDE.md           AI エージェント憲法
.shiki/             ブリッジ層（タスク・契約・DAG・状態・スキーマ）
.claude/            Claude Code 設定（エージェント・フック・スキル）
.github/            GitHub 連携（ワークフロー・Issue テンプレート）
roles/              チームロール定義（4層権限モデル）
scripts/            自動化スクリプト
templates/          テンプレートファイル
```

### Useful Commands

```bash
./bin/shiki status    # プロジェクト状態表示（θフェーズ・タスク）
./bin/shiki start     # 開発セッション起動
./bin/shiki doctor    # セットアップ健全性チェック
```

---

## License

TODO: ライセンスを指定してください。
