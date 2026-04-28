# 260322 修正依頼 — 内容整理 と 現コード調査レポート

## 1. 受領した依頼の整理

`260322修正.zip` および 4 つの添付から確認できた依頼内容は以下です。

| # | 添付ファイル | 概要 |
|---|---|---|
| A | `カタログA.html` (1475 行) | グリッド / エディトリアル切替の公開カタログ。タグ表示、検索バー(キーワード/タグ/価格レンジ/並び替え)、Quick View モーダル、ダーク基調 + 通貨切替セレクト |
| B | `カタログB.html` (526 行) | 高密度のテーブル風一覧（80px サムネ \| タイトル+タグ \| 価格 \| 在庫 \| View ボタン）。同じ検索バー、☀️ ライトモード切替 |
| C | `商品編集.html` (579 行) | サイドバー付きの商品編集 UI。画像グリッドとライトボックス、一括白抜き、仕入元情報(read-only)、JP/EN 二言語編集 + 自動翻訳ボタン、動的価格(利益率+固定利益)+手動上書き、SEO/タグ管理 |
| D | `PayPal.txt` (60 行) | **PayPal 決済 + 在庫連動ボタン制御** の実装指示書。SDK スマートボタン / 共通 Client ID / `payee.email_address` にユーザーの PayPal メールを動的代入 / 在庫切れ時はボタン非表示 / 価格はサーバーサイドで「元値 × 利益率 + 固定利益」を再計算 / フロント改ざん防止 / 通貨は USD 固定 |

---

## 2. 現コードの実装状況サマリ

### 2.1 すでにある機能 (ESP/main ブランチ)

| 依頼領域 | 該当ファイル | 状況 |
|---|---|---|
| 公開カタログ レイアウト切替 (grid / editorial / list) | <ref_snippet file="/home/ubuntu/repos/ESP/templates/catalog.html" lines="15-17" /> + `routes/catalog.py` | **実装済**。`PriceList.layout` で grid/editorial/list を切替。 |
| ダーク / ライトモード切替 | <ref_snippet file="/home/ubuntu/repos/ESP/templates/catalog.html" lines="31-31" /> | **実装済**。`themeToggleBtn` + `localStorage` で永続化。 |
| 通貨切替 (JPY/USD/EUR/GBP/AUD/CAD/CNY/KRW) | <ref_snippet file="/home/ubuntu/repos/ESP/templates/catalog.html" lines="32-44" /> | **実装済**。8 通貨 + ライブレート取得。 |
| 検索 / タグ / 価格レンジ / 並び替え | <ref_snippet file="/home/ubuntu/repos/ESP/templates/catalog.html" lines="56-63" /> + 同 264-271 | **実装済**。`tagSelect` / `priceMin` / `priceMax` / `sortSelect` / `searchInput` あり。 |
| Quick View モーダル + ギャラリー + キーボード操作 | catalog.html 167-193, 615-687 | **実装済** |
| カタログ アクセス解析 (PV / デバイス / 流入 / 人気商品 / 最近のアクセス) | <ref_file file="/home/ubuntu/repos/ESP/templates/pricelist_analytics.html" /> + `models.CatalogPageView` | **実装済** |
| 価格表編集 (レイアウト/テーマ/通貨レート/ロゴ/ノート) | <ref_snippet file="/home/ubuntu/repos/ESP/templates/pricelist_edit.html" lines="75-97" /> + `routes/pricelist.py` | **実装済** |
| 個別カスタム価格 / 表示・非表示 | <ref_snippet file="/home/ubuntu/repos/ESP/templates/pricelist_items.html" lines="71-85" /> | **実装済**。`PriceListItem.custom_price` / `visible` |
| 商品 一括追加 / 検索追加 | `pricelist.pricelist_add_products_page` | **実装済** |
| 翻訳ワークフロー (JP→EN, suggestion / apply / reject) | <ref_file file="/home/ubuntu/repos/ESP/routes/translation.py" /> + `services/translator/`, `models.TranslationSuggestion` | **実装済**。Argos バックエンド、ハッシュベースの "source updated" バッジあり。**ただし手動トリガーのみ**(編集時オートトリガーは無し)。 |
| 画像 白抜き (背景除去) — 単一 / 一括適用 | <ref_file file="/home/ubuntu/repos/ESP/routes/bg_removal.py" /> + `services/bg_remover/`, `models.ImageProcessingJob` | **実装済**。rembg + apply / apply-all / reject + HMAC 認証付きワーカーアップロード。 |
| 価格ルール (margin_rate / shipping_cost / fixed_fee) と再計算 | <ref_file file="/home/ubuntu/repos/ESP/routes/pricing.py" /> + `models.PricingRule` | **実装済**。式: `(cost + shipping) × (1 + margin_rate) + fixed_fee` |
| 商品リスト 一括価格 / 接頭辞 / 接尾辞 / 置換 / アーカイブ / 削除 | `templates/index.html` + `routes/api.py` | **実装済** |
| インライン編集 (`custom_title_en`, `selling_price`) | <ref_snippet file="/home/ubuntu/repos/ESP/templates/index.html" lines="271-282" /> | **実装済** |
| 在庫バッジ (in/out of stock) — カタログ表示 | catalog.html | **実装済** |
| ショップロゴ + ショップ別商品紐付け | <ref_file file="/home/ubuntu/repos/ESP/templates/shops.html" /> + `models.Shop` | **実装済** (`logo_url` のみ) |
| スクレイピング 選択登録 (preview → 選んで登録) | <ref_snippet file="/home/ubuntu/repos/ESP/templates/scrape_form.html" lines="20-25" /> + `static/js/scrape_form.js` | **実装済** |
| 商品 手動追加 | `routes/main.py:product_manual_add` + `templates/product_manual_add.html` | **実装済** |
| 二言語商品詳細編集 + 翻訳パネル | `templates/product_detail.html` (2195 行) | **実装済** |

つまり **カタログ A / B のレイアウト/検索/通貨/モーダル/タグ表示**、および **商品編集** の主要機能(画像 / 翻訳 / 白抜き / SEO / タグ / 価格ルール) は **既に揃っています**。

### 2.2 不足 / 未実装

| # | 依頼内容 | 現コード | 結論 |
|---|---|---|---|
| **D-1** | PayPal SDK の組み込み (スマートボタン) | `routes/`, `templates/catalog.html`, `templates/product_detail.html` のいずれにも `paypal.Buttons` / `client-id=...` の参照なし | **未実装** |
| **D-2** | ユーザー設定の PayPal メールアドレス保存欄 | `Shop` モデルは `name`, `logo_url`, `created_at` のみで `paypal_email` カラム無し。`User` モデルにも無し。`templates/settings.html` も除外キーワードのみ | **未実装** (DB マイグレーション必要) |
| **D-3** | PayPal メールの形式バリデーション + 設定 UI | 該当画面・ルートが存在しない | **未実装** |
| **D-4** | 「在庫あり時のみボタン描画 / 在庫なし時は Sold Out 表示」 | 在庫情報 (`Variant.inventory_qty` / `Product.last_status`) は揃っているが、PayPal ボタン側の制御コードは無し | **要新規** (在庫データ自体は流用可) |
| **D-5** | サーバーサイドで `calculated_price` を返すエンドポイント (USD 固定 / JPY→USD 換算込み) | `selling_price` / `PriceList.currency_rate` / クライアント側のレート換算は存在するが、**注文時に金額を再検証する API** は無し | **未実装** (フロント改ざん防止のため必須) |
| **D-6** | 注文・決済結果の永続化 (Order モデル) | `models.py` を全数確認 — `Order` / `Payment` / `Transaction` 系のモデルは存在しない | **未実装** |
| **D-7** | 公開カタログの「購入導線」(Buy ボタン → PayPal → サンクスページ) | カタログは Quick View モーダルまで。`/success` 遷移なし | **未実装** |
| **D-8** | 在庫鮮度の確認タイミング設計 (ページロード時 / クリック直前) | パトロール (`services/patrol/mercari_patrol.py`) は存在するが、決済フローとの結線は無し | **要設計** |
| **C-1** | カタログ B の高密度テーブル行レイアウト (80px サムネ \| タイトル+タグ \| 価格 \| 在庫 \| View) | 現 `layout="list"` は存在するが、添付 B のように `grid-template-columns: 80px 1fr 120px 120px 100px;` ではないため見た目が異なる可能性あり | **微調整** で対応可 |
| **A-1** | カタログのタグ チップ表示 (商品カード上に PSA10 / Pokemon などの pill) | 検索の `tagSelect` は実装済だが、**商品カード本体にタグ pill を並べる UI** は薄い (要 catalog.html の確認 / 拡張) | **小規模修正** |
| **C-2** | 商品編集での「動的価格 (利益率+固定利益) と 手動上書き」 UI | 価格ルール紐付け (`pricing_rule_id`) と `selling_price` のインライン編集はあるが、**編集画面で式と手動上書きを並べた UI** は未確認 | **小規模修正 / 既存拡張** |
| **C-3** | 編集時の自動翻訳トリガー (リアルタイム) | `/api/products/<id>/translate` は手動押下式 | **要設計** (debounce + auto-fire の場合) |
| **C-4** | 画像のライトボックス + ドラッグ並び替え + 一括白抜きの "編集 UI" | API は揃っている。`product_detail.html` 側の UI 強化が必要かは要詳細レビュー | **既存拡張** |

---

## 3. リスクと留意点

- **AGENTS.md 高リスク不変条件**: 公開カタログに `source_url` / `site` を露出させない、ユーザー/ショップ/価格表分離を保つ、Render の web/worker/DB/queue 契約を変更しない、本番では `SECRET_KEY` を `esp-web` と `esp-worker` で共有する。PayPal 連携時もこれらを順守する必要があります。
- **PayPal Client ID 共通化**: 仕様書では「運営側の Client ID を共通利用、`payee.email_address` で受取人を切替」となっているため、**Client ID は環境変数 (Render の `PAYPAL_CLIENT_ID` 等)** で持ち、`PAYPAL_ENV` (sandbox/live) で切替できる構成が望ましい。
- **改ざん防止**: フロントの `value` を信用せず、`createOrder` 直前にサーバーへ商品 ID + 数量を投げ、`(last_price + shipping) * (1 + margin) + fixed_fee` をサーバーで再計算した上で PayPal に渡すか、Webhook (`PAYMENT.CAPTURE.COMPLETED`) で金額照合 → 不一致なら refund する設計が安全です。
- **通貨**: 仕様書は USD 固定。現状の `currency_rate` (JPY→USD) はカタログ表示用なので、決済用にも同レート / または別の `payment_currency_rate` を使うかを決める必要があります。
- **公開ページに決済を載せる影響**: 現 `routes/catalog.py` はトークン認証で公開していますが、PayPal を載せると **eコマース要件**(法定表記、特商法、プライバシー、返金規約) が発生します。`docs/legal/PRIVACY_POLICY_DRAFT.md` 等の整備状況も確認が必要。

---

## 4. 推奨される次の段取り (例)

1. **DB マイグレーション**: `Shop.paypal_email` (または `User.paypal_email`) と `Order` モデル(商品 ID / 数量 / 金額 / PayPal `order_id` / status / created_at) を追加。
2. **設定 UI**: `templates/shops.html` の編集モーダルに PayPal メール欄(形式バリデーション付き) を追加。Or 新規 `/settings/payment` 画面。
3. **サーバーサイド価格 API**: `POST /api/catalog/<token>/quote` — 商品 ID を受けて `{ amount_usd, currency: 'USD', expires_at }` を返す。
4. **公開商品ページ**: `/p/<token>/<product_id>` を新設し、PayPal ボタンと「Sold Out」分岐を入れる(または既存 Quick View モーダル内に組み込む)。
5. **キャプチャ Webhook**: `POST /webhooks/paypal` を実装し、注文を確定させる。
6. **在庫鮮度**: ボタンクリック直前に `/api/catalog/<token>/<product_id>/availability` を呼んで再確認 → 在庫切れなら無効化。
7. **テスト**: `tests/test_e2e_routes.py` に PayPal モック (sandbox の代替) でのフロー追加。

---

## 5. 結論 (一言)

- **カタログ A / B / 商品編集 のビジュアル要素 と 翻訳 / 白抜き / 価格ルール / カタログ解析 は既に概ね実装済み**。差分は主に **見た目の調整 (タグ pill / list レイアウトの密度)** 程度。
- **PayPal 決済 + 在庫連動 は完全に未実装**。DB スキーマ / 設定 UI / サーバーサイド価格 API / 公開商品ページ / Webhook が必要。
