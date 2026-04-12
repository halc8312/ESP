# bug_reports

## 目次
- [検出した不具合一覧](#検出した不具合一覧)
- [補足](#補足)

## 検出した不具合一覧

### BUG-E2E-001
| 項目 | 内容 |
|---|---|
| Bug ID | BUG-E2E-001 |
| タイトル | 外部CDN不達時に編集系画面で `tinymce is not defined` が発生しJSが崩れる |
| 深刻度 | Medium |
| 優先度 | High |
| ステータス | Open |
| 事象 | `/product/1`、`/templates`、`/pricelists/create` で cdnjs 上の TinyMCE 読み込み失敗後に `tinymce.init(...)` が無条件実行され、console に `ReferenceError: tinymce is not defined` が出る。 |
| 前提条件 | cdnjs への到達が制限される、または外部CDN障害がある |
| 再現手順 | 1. 外部CDN到達性がない環境でアプリを起動する。 2. ログイン後に `/product/1` を開く。 3. `/templates`、`/pricelists/create` も順に開く。 4. ブラウザコンソールを確認する。 |
| 期待結果 | 外部エディタが読み込めなくても textarea フォールバックで継続利用でき、JS例外を出さない。 |
| 実際結果 | `tinymce is not defined` が発生し、WYSIWYG前提のJSが崩れる。フォーム送信自体は今回継続できたが、編集UXは劣化する。 |
| 再現率 | 100%（今回環境） |
| 影響範囲 | 商品編集、テンプレート作成、価格表作成/編集のリッチテキスト領域 |
| 回帰懸念 | TinyMCE/Sortable/Chart.js のCDN依存箇所全般で同種の障害が波及する可能性 |
| 暫定回避策 | CDN到達可能なネットワークで利用する、または JS例外を無視してプレーンtextareaとして入力する |
| 原因仮説 | 外部 `<script src=...>` 読み込み失敗時のガードがなく、`tinymce` 存在確認なしに初期化している |
| 追加確認が必要なログ / データ / API | 本番CSP設定、CDN利用方針、セルフホスト代替の有無、監視ログ上の script load error |
| 関連テストケース | TC-E2E-008, TC-E2E-010, TC-E2E-011, TC-E2E-019 |
| 関連証跡 | EVD-019, EVD-008, EVD-010 |

## 補足
- `pricelist_analytics.html` の Chart.js は `typeof Chart === 'undefined'` でガードされており、可視化欠落はあるが致命的エラー化はしなかった。
- `open.er-api.com` 不達時の公開カタログはフォールバックレートで継続表示したため、本不具合とは切り分けた。
