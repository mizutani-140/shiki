# Guardian — 人間の最終権限者

**Version**: 1.0.0

---

## Guardian とは

Guardian はプロジェクトの**人間の最終権限者**です。
3層権限モデル（coordinator / executor / monitor）の**上位**に位置し、
AIエージェントが自律的に判断できない事項の最終決定権を持ちます。

```
guardian（人間）
  └── coordinator（計画・調整）
        ├── executor（実装）
        └── monitor（監視）
```

---

## 権限

| 権限 | 説明 |
|------|------|
| **憲法修正** | CLAUDE.md / AGENTS.md の変更承認 |
| **バジェット承認** | 予算上限の変更、緊急停止解除 |
| **ゴール変更** | GOAL.md の受入条件変更の最終承認 |
| **権限モデル変更** | roles/roles.yaml の変更承認 |
| **デプロイ承認** | 本番環境へのデプロイ最終承認 |
| **緊急停止** | 全エージェントの即時停止命令 |
| **紛争解決** | coordinator 間の判断の不一致を裁定 |

---

## エスカレーション条件

以下の場合、エージェントは Guardian にエスカレーション**しなければならない**：

### 即時エスカレーション（Critical）

1. **セキュリティ脆弱性** — 本番環境に影響する脆弱性の発見
2. **バジェット緊急超過** — 150%を超えるトークン使用
3. **権限モデル違反** — エージェントが権限外の操作を実行
4. **データ損失リスク** — 破壊的操作の必要性が生じた場合
5. **exec verify 最大反復到達** — 自動修正で解決不可能

### 標準エスカレーション（High）

6. **GOAL.md 変更提案** — 受入条件の追加/変更/削除
7. **アーキテクチャ変更** — 設計の根本的変更が必要
8. **外部サービス連携** — 新しいAPI/サービスの導入判断
9. **バジェット警告超過** — 80%を超えるトークン使用

### 報告（Medium/Low）

10. **θフェーズ完了報告** — 各フェーズの完了と次フェーズ開始の承認
11. **週次レポート** — 進捗サマリーの提出

---

## エスカレーション手順

### CLIモード

```
coordinator → SendMessage で Guardian に報告
Guardian は Claude Code セッション外で判断を下す
Guardian の判断を coordinator が受け取り、チームに伝達
```

### GitHubモード

```
coordinator → GitHub Issue 作成（ラベル: guardian-review）
Guardian が Issue にコメントで判断を記載
ワークフローが判断を読み取り、処理を再開
```

### エスカレーションテンプレート

```markdown
## Guardian エスカレーション

**重大度**: Critical / High / Medium / Low
**タスク**: T-XXXX
**θフェーズ**: θ₄ EXECUTE
**発生日時**: YYYY-MM-DD HH:MM

### 状況
[現在の状況の説明]

### 影響範囲
- ファイル: [影響を受けるファイル]
- タスク: [影響を受けるタスク]
- バジェット: [影響額]

### 推奨アクション
[エージェントが推奨する対応策]

### 判断が必要な項目
- [ ] [具体的な判断項目1]
- [ ] [具体的な判断項目2]
```

---

## Guardian の理念

> **"Guardian の役割は制御ではなく、ガイドすること。"**
>
> **"エージェントは95%の時間、自律的に動作すべき。"**
>
> **"人間の介入は障害モードであり、機能ではない。"**

### 原則

1. **信頼と検証**: エージェントの自律性を信頼するが、行動は監査可能にする
2. **最小介入**: Guardian が介入するのは上記エスカレーション条件の場合のみ
3. **透明性**: 全ての Guardian 判断は GitHub 上で文書化する
4. **段階的自動化**: 手動承認を段階的に自動化していく

### 目標メトリクス

| メトリクス | 目標値 | 根拠 |
|-----------|--------|------|
| 人間介入率 | 5%以下 | 自律性の最大化 |
| エスカレーション応答時間 | Critical: 24h, High: 72h | 開発速度の維持 |
| 誤エスカレーション率 | 10%以下 | エスカレーション基準の適切さ |

---

## Guardian 設定

`.shiki/config.yaml` に以下を追加して Guardian を設定：

```yaml
guardian:
  enabled: true
  github_username: "your-github-username"
  escalation_label: "guardian-review"
  budget_emergency_threshold_pct: 150
  auto_approve:
    theta_phase_transitions: false  # θフェーズ遷移の自動承認
    budget_increase: false          # バジェット増額の自動承認
    deploy_staging: true            # staging デプロイの自動承認
    deploy_production: false        # 本番デプロイの自動承認
```

---

**このファイルはプロジェクトの Guardian 権限を定義します。変更には Guardian 自身の承認が必要です。**
