# ローカルセットアップ：Codex（CLI / MCP）

Codex CLI のインストールから認証、MCP 連携までの手順。

---

## 1. Codex CLI インストール

```bash
npm i -g @openai/codex
# または
brew install codex
```

```bash
# バージョン確認
codex --version
```

---

## 2. 認証

認証方式は2つから選択。Claude と同様、サブスクリプション認証を推奨。

### 方式A: Pro/Plus プランログイン（推奨）

OpenAI Pro ($200/月) または Plus プランのサブスクリプション枠内で実行。API 従量課金が発生しない。

```bash
# ブラウザ OAuth フローで認証
codex login

# ブラウザを開けない環境（SSH/Docker/WSL）の場合
codex login --device-auth
# 表示されるコードを https://auth.openai.com/codex/device に入力

# 認証状態の確認
codex login status

# ログアウト
codex logout
```

**認証情報の保存先:**

| 設定 | 保存先 |
|------|--------|
| `auto`（デフォルト） | OS キーチェーン → `~/.codex/auth.json` にフォールバック |
| `keyring` | OS キーチェーン（macOS Keychain / Linux Secret Service） |
| `file` | `~/.codex/auth.json` |

`~/.codex/config.toml` で `cli_auth_credentials_store` を設定可能。

**重要:** `~/.codex/auth.json` にはアクセストークンが含まれます。コミット・共有しないでください。

### 方式B: API キー（従量課金）

OpenAI Platform の API キーで認証。CI/CD やサブスクリプションを使わない場合に使用。

```bash
export OPENAI_API_KEY="sk-..."

# API キーでログイン（パイプ経由、シェル履歴に残さない）
printenv OPENAI_API_KEY | codex login --with-api-key
```

### 方式の比較

| 観点 | Pro plan login | API key |
|------|---------------|---------|
| 課金 | サブスク枠内（追加費用なし） | トークン従量課金 |
| CLI モード | 対応 | 対応 |
| GitHub Actions | **非対応**（ブラウザ認証不可） | 対応 |
| Cloud 機能 | フル対応（Cloud Tasks, GitHub Reviews） | 非対応 |
| トークン管理 | 自動リフレッシュ | 期限なし（静的キー） |
| セットアップ | `codex login` | `export OPENAI_API_KEY=...` |

**推奨構成:**
- **CLI モード:** Pro plan login（サブスク枠内、追加費用なし）
- **GitHub Actions:** API key（CI ではブラウザ認証不可のため必須）

---

## 3. MCP サーバーとして起動

Codex CLI は MCP (Model Context Protocol) サーバーとして起動できます。Claude Code の executor エージェントから Codex にタスクを委託する基盤です。

```bash
# MCP サーバーとして起動（認証情報は ~/.codex/ から自動継承）
codex mcp-server
```

MCP サーバーは起動プロセスの環境と `~/.codex/` の認証情報を**自動継承**します。`codex login` 済みであれば `OPENAI_API_KEY` は不要です。

---

## 4. Claude Code から MCP として追加

プロジェクトルートで:

```bash
claude mcp add --transport stdio --scope project codex -- codex mcp-server
claude mcp list
```

これで Claude Code の executor エージェントから「codex」ツールとして呼び出せます。

`.claude/mcp.json` に以下のエントリが追加されます:

```json
{
  "codex": {
    "command": "codex",
    "args": ["mcp-server"],
    "env": {
      "CODEX_HOME": "${CODEX_HOME:-}"
    },
    "timeout": 300000,
    "disabled": false
  }
}
```

`CODEX_HOME` は `~/.codex/` のデフォルトパスを上書きする場合に設定します。通常は設定不要です。

---

## 5. 認証状態の確認

### CLI で直接確認

```bash
codex login status
# 認証方式（ChatGPT OAuth / API key）、トークン有効期限が表示される
```

### セッション起動時の自動チェック

`scripts/start_cli_session.sh` がセッション起動時に以下を自動検証します:

```
[STEP] Dual Engine 認証チェック...
[INFO] Claude CLI: OK
[INFO] Codex CLI: OK (authenticated)
```

認証されていない場合:

```
[WARN] Codex CLI: installed but not authenticated
[WARN]   Pro plan: codex login
[WARN]   API key:  export OPENAI_API_KEY="sk-..."
```

### Hook による自動注入

`session_start_inject_context.py` がセッション開始時に Codex の認証状態をコンテキストに注入します:

```
## Dual Engine
- Claude: enabled (auth=oauth)
- Codex: enabled (auth=login, status=authenticated)
```

---

## 6. GitHub Actions での Codex

GitHub Actions では `codex login` のブラウザ OAuth フローが使えないため、**API キーが必須**です。

```yaml
# GitHub Secrets に設定
OPENAI_API_KEY: sk-...
```

ワークフローでの使用:

```yaml
- uses: openai/codex-action@v1
  with:
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
    prompt: "Implement the function as specified"
    sandbox: workspace-write
```

---

## 7. Codex 設定ファイル

```
~/.codex/
  ├── config.toml         # ユーザー設定
  ├── auth.json            # 認証トークン（file モード時）
  └── auth_token.json      # 暗号化フォールバック

.codex/
  └── config.toml          # プロジェクト固有設定
```

### config.toml の主要設定

```toml
# 認証方式を固定する場合
forced_login_method = "chatgpt"   # "chatgpt" | "api"

# 認証情報の保存先
cli_auth_credentials_store = "auto"  # "auto" | "file" | "keyring"
```

---

## 8. トラブルシューティング

### Codex MCP が接続できない

```bash
# Codex CLI の確認
codex --version

# 認証状態の確認
codex login status

# MCP サーバーの動作確認
codex mcp-server --help
```

### Pro plan ログインが失敗する

```bash
# デバイスコードフローを試す
codex login --device-auth

# ログアウトしてから再ログイン
codex logout && codex login
```

### GitHub Actions で Codex が動かない

- `OPENAI_API_KEY` が GitHub Secrets に設定されているか確認
- Pro plan ログインは CI では使用不可。API キーが必要

### `~/.codex/auth.json` が見つからない

認証情報が OS キーチェーンに保存されている場合、ファイルは存在しません（正常動作）。`codex login status` で認証状態を確認してください。
