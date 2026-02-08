# 通信パターン（MCP / File Bridge / GitHub Actions）

## 1) MCP（同期・低遅延）
- すぐ結果が欲しい（関数実装、テスト生成、デバッグ）
- Claude Code から Codex を「ツール」として呼び出す

メリット：
- 即時性
- 対話的に反復できる

デメリット：
- ローカル環境に依存（実行環境の差異）
- セッションが落ちると中断

---

## 2) File Bridge（非同期・堅牢）
- `.shiki/` を通じた協調
- 状態がGitに残るので復旧が簡単

メリット：
- 再現性（状態がファイル）
- エージェントの再起動に強い

デメリット：
- 即時性は落ちる

---

## 3) GitHub Actions（CI/CD統合）
- Issue/PRを起点に完全非同期で回す
- 監査ログ（Actions logs）が残る

メリット：
- 実行が自動
- CIと直結

デメリット：
- APIキー運用が前提になりやすい
- ループするとコストが増える（max turns などで抑制）

---

## おすすめの使い分け
- 大設計/議論：Claude Agent Teams（ローカル）
- 実装/テスト反復：Codex（MCPまたはActions）
- 安定運用：GitHub Actions（段階導入）

