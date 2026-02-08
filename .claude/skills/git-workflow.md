# Git Workflow

## 概要

Gitワークフローとコンベンションのガイド。Conventional Commits、ブランチ命名規則、git worktreeの活用、PRコンベンションを通じて、チーム開発における一貫性と効率性を確保する。

## 適用場面

- 日常的なコミット・プッシュ操作
- ブランチ戦略の選定・運用
- PR作成・マージ
- リリース管理

## ベストプラクティス

### 1. Conventional Commits

- コミットメッセージのフォーマット: `type(scope): description`
- 主要な type: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`
- scope はモジュール名やディレクトリ名を使用する
- description は命令形で、50文字以内に収める
- Breaking changeは `!` を付記する: `feat(api)!: remove deprecated endpoint`
- bodyに詳細な説明を記述する（必要に応じて）

### 2. ブランチ命名規則

- `feature/#123-short-description` -- 新機能
- `fix/#456-bug-description` -- バグ修正
- `refactor/#789-target-description` -- リファクタリング
- `docs/#012-topic` -- ドキュメント
- `chore/#345-task` -- その他メンテナンス
- ブランチ名は英小文字とハイフンのみ使用する

### 3. git worktree の活用

- 複数ブランチでの並行作業に `git worktree` を使用する
- レビュー中のPRの確認: `git worktree add ../review-branch origin/feature-branch`
- 緊急のhotfix対応: `git worktree add ../hotfix main`
- 不要になったworktreeは `git worktree remove` で削除する
- worktreeごとに独立した作業ディレクトリを持てる利点を活用する

### 4. PR コンベンション

- PRタイトルはConventional Commits形式に準拠する
- PRテンプレートを用意し、変更概要・テスト方法・影響範囲を記載する
- draft PRで早期にフィードバックを得る
- 1 PR の変更は300行以内を目安にする（大きい場合は分割する）
- PRにはスクリーンショットやテスト結果を添付する

### 5. マージ戦略

- Squash mergeを原則とする（クリーンな履歴を維持）
- 大きなfeatureブランチはmerge commitを使用する
- rebase は個人ブランチでのみ使用する
- mainブランチへの直接コミットは禁止する
- マージ後はリモートブランチを削除する

## チェックリスト

- [ ] コミットメッセージがConventional Commitsに準拠している
- [ ] ブランチ名にIssue番号が含まれている
- [ ] PRの変更量が適切なサイズに収まっている
- [ ] draft PRで早期フィードバックを得ている
- [ ] マージ後にリモートブランチを削除している
- [ ] mainブランチが常にデプロイ可能な状態を維持している

## 関連スキル

- [Issue駆動開発](./issue-driven-development.md)
- [コードレビュー](./code-review.md)
- [CI/CD](./ci-cd.md)
