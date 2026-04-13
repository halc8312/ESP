# execution_results

## 目次
- [実行したケース一覧](#実行したケース一覧)
- [証跡一覧](#証跡一覧)
- [SQL裏取りサマリー](#sql裏取りサマリー)
- [事実と仮説の区別](#事実と仮説の区別)

## 実行したケース一覧

| 実行日時 | ケースID | 実行環境 | 実行結果 | 実際結果 | 証跡メモ | 関連不具合ID | 再現性メモ | 未実施理由 | 次回確認ポイント | 事実 / 仮説 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-04-12 23:39 | TC-AUTO-001 | pytest | Pass | `tests/test_health_route.py` と `tests/test_auth.py` が 5件通過 | EVD-RT-001 | - | 安定 | - | UI実査と整合確認 | 事実 |
| 2026-04-12 23:39 | TC-AUTO-002 | pytest | Pass | `tests/test_e2e_routes.py` が 78件通過 | EVD-RT-002 | - | 安定 | - | 実ブラウザとの差分確認 | 事実 |
| 2026-04-12 23:41 | TC-E2E-001 | Playwright | Pass | `e2e_user_a` 登録後に `/` へ遷移しログイン状態になった | EVD-001 | - | 再現性高 | - | セッション維持時間 | 事実 |
| 2026-04-12 23:46 | TC-E2E-002 | Playwright | Pass | 誤パスワード5回は通常失敗、6回目で「ログイン試行回数が上限に達しました」表示 | EVD-002 | - | 再現性高 | - | 解除タイミング/正しい資格情報での復帰確認 | 事実 |
| 2026-04-12 23:41 | TC-E2E-003 | Playwright | 未確認 | `QA Shop A` 追加と同名重複拒否を確認、編集/削除までは未実施 | EVD-003 | - | 追加/重複は再現性高 | 編集/削除未実施 | current_shop と削除影響 | 事実 |
| 2026-04-12 23:42 | TC-E2E-004 | Playwright + SQLite | Pass | 商品登録後 `/product/1` へ遷移し shop/title/description/variant が保持された | EVD-004 | - | 再現性高 | - | 画像追加と multi-variant | 事実 |
| 2026-04-12 23:42 | TC-E2E-005 | Playwright + SQLite | Pass | 同一 `source_url` 再登録でエラー表示、件数増加なし、入力保持あり | EVD-005 | - | 再現性高 | - | 他の重複キー境界 | 事実 |
| 2026-04-12 23:42 | TC-E2E-007 | Playwright + SQLite | Pass | 商品名・EN名・variant価格/在庫/SKU 更新が詳細/一覧/DBへ反映、`selling_price=2500` 同期 | EVD-007 | - | 再現性高 | - | 画像順序/variant削除 | 事実 |
| 2026-04-12 23:43 | TC-E2E-008 | Playwright + SQLite | Pass | テンプレート作成/削除成功、保存内容は `Thanks!` にサニタイズされ script 消失 | EVD-008 | BUG-E2E-001 | 再現性高 | - | WYSIWYG無効時の入力UX | 事実 |
| 2026-04-12 23:43 | TC-E2E-009 | Playwright + SQLite | 未確認 | 価格ルールの作成/編集は成功、削除未実施 | EVD-009 | - | 作成/編集は再現性高 | 削除未実施 | 商品再計算の波及 | 事実 |
| 2026-04-12 23:44 | TC-E2E-010 | Playwright + SQLite | Pass | 価格表作成→商品追加→公開URL表示まで成功 | EVD-010 | BUG-E2E-001 | 再現性高 | - | 非公開切替、削除、複数商品順序 | 事実 |
| 2026-04-12 23:44 | TC-E2E-011 | Playwright + SQLite | Pass | notes 内 script は除去され `<b>VIP</b> buyers only` のみ保持、list layout 反映、`source_url/site` 非露出 | EVD-011 | - | 再現性高 | - | invalid layout の直接改ざん | 事実 |
| 2026-04-12 23:44 | TC-E2E-012 | Playwright + SQLite | Pass | カタログ閲覧1回 + Quick View1回が analytics に PV=2 / 商品詳細=1 として記録 | EVD-012 | - | 再現性高 | - | 参照元/ユニーク境界 | 事実 |
| 2026-04-12 23:45 | TC-E2E-015 | Playwright | Pass | `/logout` 後にブラウザ戻ると `/login?next=...` へ戻され保護画面へ再入不可 | EVD-015 | - | 再現性高 | - | キャッシュ済み画面の機微情報残存 | 事実 |
| 2026-04-12 23:45 | TC-E2E-006 | Playwright | Pass | ユーザーBで `/product/1` アクセス時に `Product not found or access denied` を返却 | EVD-006 | - | 再現性高 | - | 404文言の情報量 | 事実 |
| 2026-04-12 23:45 | TC-E2E-020 | Playwright | Pass | ユーザーBで `/pricelists/1/items` へアクセスすると `/pricelists` に遷移し他人価格表に到達不可 | EVD-020 | - | 再現性高 | - | edit/analytics 直叩きも確認したい | 事実 |
| 2026-04-12 23:46 | TC-E2E-016 | Playwright fetch | Pass | ユーザーBで `/api/products/1/inline-update` を PATCH すると 404 `{"error":"Product not found"}` | EVD-016 | - | 再現性高 | - | 未認証時のJSON応答様式 | 事実 |
| 2026-04-12 23:42-23:44 | TC-E2E-019 | Playwright console/network | Fail | `/product/1`, `/templates`, `/pricelists/create` で cdnjs 不達時に `tinymce is not defined` が発生 | EVD-019 | BUG-E2E-001 | 再現性高 | - | fallback textarea/guard 実装要否 | 事実 |
| 2026-04-12 23:44 | TC-E2E-X01 | Playwright public catalog | Pass | `open.er-api.com` 不達時に「Fallback Rate: 1 USD = ¥150」と警告表示、カタログ自体は継続利用可 | EVD-013 | - | 再現性高 | - | API復旧後の自動更新 | 事実 |
| 2026-04-12 23:46 | TC-E2E-X02 | Playwright | 未確認 | ユーザーB登録直後の index に `Please log in to access this page.` の残留フラッシュを観測 | EVD-014 | - | 要再現 | 事前の `next` 付き遷移が混在 | 新規登録直後のUX整理 | 仮説 |
| 2026-04-12 23:47 | TC-E2E-013 | - | Not Run | 商品一覧検索/フィルタは未実施 | - | - | - | 時間優先度 | 複数商品用意後に確認 | 事実 |
| 2026-04-12 23:47 | TC-E2E-014 | - | Not Run | archive/trash 状態遷移未実施 | - | - | - | 時間優先度 | 復元/完全削除まで確認 | 事実 |
| 2026-04-12 23:47 | TC-E2E-017 | - | Not Run | CSV入出力未実施 | - | - | - | サンプルCSV整備不足 | 文字化け/列ズレ | 事実 |
| 2026-04-12 23:47 | TC-E2E-018 | - | Blocked | 実サイトスクレイピング/外部連携失敗制御は安全条件不足で未実施 | - | - | - | 外部依存・安全性 | テスト用ターゲット/モック用意 | 事実 |

## 証跡一覧
| 証跡ID | 種別 | 概要 | 関連ケース | 備考 |
|---|---|---|---|---|
| EVD-RT-001 | CLIログ | auth/health pytest 5件通過 | TC-AUTO-001 | `python -m pytest tests/test_health_route.py tests/test_auth.py -x --tb=short` |
| EVD-RT-002 | CLIログ | route E2E pytest 78件通過 | TC-AUTO-002 | `python -m pytest tests/test_e2e_routes.py -x --tb=short` |
| EVD-001 | Playwright観測 | 登録後トップへ遷移、サイドバーに `e2e_user_a` 表示 | TC-E2E-001 | URL `/` |
| EVD-002 | Playwright観測 | 6回目でレート制限文言表示 | TC-E2E-002 | URL `/login` |
| EVD-003 | Playwright観測 | `QA Shop A` 追加と重複拒否メッセージ確認 | TC-E2E-003 | URL `/shops` |
| EVD-004 | Playwright + SQL | 商品編集画面に登録内容初期表示 | TC-E2E-004 | URL `/product/1` |
| EVD-005 | Playwright + SQL | `同じ元URLの商品が既に登録されています` 表示 | TC-E2E-005 | URL `/products/manual-add` |
| EVD-006 | Playwright観測 | ユーザーBで `Product not found or access denied` | TC-E2E-006 | URL `/product/1` |
| EVD-007 | Playwright + SQL | 一覧/詳細/DBで `テスト商品A 更新` と `2500/3` を確認 | TC-E2E-007 | index / product / SQLite |
| EVD-008 | Playwright + SQL | テンプレート一覧の抜粋 `Thanks!...`、削除後0件 | TC-E2E-008 | `/templates` |
| EVD-009 | Playwright + SQL | `標準E2E更新` と計算例 `¥2,300` | TC-E2E-009 | `/pricing` |
| EVD-010 | Playwright観測 | 価格表作成 → items → add-products → preview 成功 | TC-E2E-010 | `/pricelists/1/*` |
| EVD-011 | Playwright + SQL | public notes は `VIP buyers only`、layout は `List`、DB notes は `<b>VIP</b> buyers only` | TC-E2E-011 | catalog / SQLite |
| EVD-012 | Playwright + SQL | analyticsで PV=2, 商品詳細=1, recent views=2 | TC-E2E-012 | `/pricelists/1/analytics` |
| EVD-013 | Playwright観測 | `Fallback Rate: 1 USD = ¥150` と API unavailable 警告 | TC-E2E-X01 | open.er-api 不達時フォールバック |
| EVD-014 | Playwright観測 | ユーザーB初回 index にログイン要求フラッシュ残留 | TC-E2E-X02 | 再現未確定 |
| EVD-015 | Playwright観測 | ログアウト後 Back で `/login?next=...` へ戻る | TC-E2E-015 | 保護画面再入不可 |
| EVD-016 | Playwright fetch | API PATCH 404 `Product not found` | TC-E2E-016 | JSON本文取得 |
| EVD-019 | Console / Network | `tinymce is not defined` + cdnjs block | TC-E2E-019 | `/product/1`, `/templates`, `/pricelists/create` |
| EVD-020 | Playwright観測 | 他人価格表 items 直アクセスで `/pricelists` へ遷移 | TC-E2E-020 | URL変化確認 |
| EVD-SQL-001 | SQLite照会 | users=2, shops=1, products=1, price_lists=1, page_views=2 | 複数 | 専用DB直接参照 |

## SQL裏取りサマリー
### 主要件数
| テーブル | 件数 |
|---|---:|
| users | 2 |
| shops | 1 |
| products | 1 |
| variants | 1 |
| description_templates | 0 |
| pricing_rules | 1 |
| price_lists | 1 |
| price_list_items | 1 |
| catalog_page_views | 2 |

### 主要レコード抜粋
- `products.id=1`: `user_id=1`, `shop_id=1`, `last_title=テスト商品A 😀`, `custom_title=テスト商品A 更新`, `custom_title_en=Test Product A Updated`, `selling_price=2500`, `tags=qa,e2e,updated`
- `variants.id=1`: `option1_value=Default Updated`, `sku=E2E-A-001-REV1`, `price=2500`, `inventory_qty=3`
- `price_lists.id=1`: `name=QA Pricelist 1`, `layout=list`, `notes=<b>VIP</b> buyers only`, `is_active=1`
- `price_list_items.id=1`: `custom_price=3333`, `visible=1`
- `catalog_page_views`: `product_id=NULL` が1件、`product_id=1` が1件

## 事実と仮説の区別
### 事実
- 既存pytest 83件は全通過した。
- 商品登録/更新/価格表公開/analytics/所有権チェック/レート制限はローカルで成立した。
- TinyMCE を使う3画面で CDN 不達時に `tinymce is not defined` が発生した。
- open.er-api 不達時、公開カタログはフォールバックレートで継続表示した。

### 仮説
- `Please log in to access this page.` の残留フラッシュは、`next` 付き遷移や prior redirect の影響で再現する可能性がある。
- 本番でもCDN障害/CSP/社内プロキシ制約があると編集UXが同様に劣化する可能性が高い。
