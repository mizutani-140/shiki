# セキュリティとガードレール（最重要）

自律開発は強力ですが、事故も高速化します。
このフレームワークは「最初から自動マージ/自動デプロイ」ではなく、段階導入を推奨します。

---

## 1) Key/Secrets を絶対に出さない
- ローカル：`.env` を使うなら必ず `.gitignore`
- GitHub：Secrets にのみ保存
- hooks（PreToolUse）で `printenv` / `env | grep KEY` / `echo $OPENAI_API_KEY` をブロックする

---

## 2) 危険コマンドをブロック
- `rm -rf /`
- `git push --force main`
- `curl | bash`（必要なら明示許可）
- データ破壊系（DB drop 等）

---

## 3) 自動マージ/デプロイは「条件付き」にする
- 変更範囲が限定（docs/テスト追加/フォーマット等）
- CIが全て成功
- セキュリティチェック成功
- CODEOWNERS or Environments approval を通過

---

## 4) 監査ログを残す
- `.shiki/reports/` を必ず残す
- GitHub Actions のログも残る（いつ、誰が、何をしたか）

---

## 5) “勝手に仕様変更”を防ぐ
- Contracts のステータス機械を守る
- `status=proposed` の変更は、人間または別レビューを必須にする

---

## 6) 失敗は前提、復旧を用意する
- task に `lease_expires_at` を持たせ、放置検知して回収する
- Plan/Contracts/Tasks を Git に置いておき、セッションが落ちても復旧できるようにする

