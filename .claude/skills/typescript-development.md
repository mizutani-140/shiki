# TypeScript Development

## 概要

TypeScript開発におけるベストプラクティスと型安全性を重視したコーディングガイドラインを提供する。strict modeの活用、ESModules対応、ジェネリクスの適切な使用を通じて、堅牢で保守性の高いコードベースを実現する。

## 適用場面

- 新規TypeScriptプロジェクトの立ち上げ
- 既存JavaScriptプロジェクトのTypeScript移行
- 型定義の設計・改善
- ライブラリ・パッケージの開発

## ベストプラクティス

### 1. コンパイラ設定

- `strict: true` を必ず有効にする
- `noUncheckedIndexedAccess: true` で配列アクセスを安全にする
- `exactOptionalProperties: true` でオプショナルプロパティの曖昧さを排除する
- `moduleResolution: "bundler"` または `"node16"` を使用する
- `target` と `module` は ESNext に設定し、ビルドツールに任せる

### 2. 型安全性

- `any` の使用を禁止する。`unknown` を使い、型ガードで絞り込む
- `as` によるキャストを避け、型ガード関数を定義する
- Union型とDiscriminated Unionを活用してドメインを正確に表現する
- テンプレートリテラル型でstringの制約を表現する
- `satisfies` 演算子で型推論を保持しつつ型チェックを行う

### 3. ジェネリクスと抽象化

- ジェネリクスは具体的な制約 (`extends`) を付ける
- 不要な型パラメータを増やさない（使われないジェネリクスは削除）
- Conditional Typesは複雑になりすぎる場合、オーバーロードを検討する
- `infer` キーワードを活用した型レベルのパターンマッチング

### 4. エラーハンドリング

- Result型パターン (`{ ok: true; value: T } | { ok: false; error: E }`) を採用する
- カスタムエラークラスを定義し、エラーの種類を型で区別する
- `never` 型で網羅性チェックを実装する
- 非同期処理では必ずエラーの型を明示する

### 5. モジュール設計

- ESModules (import/export) を使用する。CommonJSは避ける
- barrel exports (index.ts) は循環依存に注意して使用する
- 副作用のないモジュール設計を心がける
- `type` キーワードを型のimport/exportに付ける (`import type { ... }`)

## チェックリスト

- [ ] tsconfig.json で strict: true が有効になっている
- [ ] any 型が使用されていない
- [ ] 全てのpublic APIに適切な型定義がある
- [ ] エラー型が明示的に定義されている
- [ ] ESModules形式でimport/exportしている
- [ ] 型ガード関数が適切に定義されている
- [ ] ジェネリクスに制約が付けられている
- [ ] 型テスト（tsd, expect-type等）が用意されている

## 関連スキル

- [テスト駆動開発](./tdd-workflow.md)
- [MCP Server開発](./mcp-server-development.md)
- [デバッグ](./debugging.md)
