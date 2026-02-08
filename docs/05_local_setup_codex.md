# ローカルセットアップ：Codex（CLI / MCP）

## 1) Codex CLI インストール
```bash
npm i -g @openai/codex
```

## 2) 認証
環境により2通りあります。

### A) サインイン（ChatGPTのアカウント）
Codex CLI の案内に従ってサインインします。

### B) APIキー（自動化向き）
```bash
export OPENAI_API_KEY="sk-..."
```

## 3) MCP サーバーとして起動
Codex CLI は MCP サーバーとして起動できます。

```bash
codex mcp-server
```

## 4) Claude Code から MCP として追加
（プロジェクトルートで）

```bash
claude mcp add --transport stdio --scope project codex -- codex mcp-server
claude mcp list
```

これで Claude Code から「codex」ツールとして呼び出せます。

