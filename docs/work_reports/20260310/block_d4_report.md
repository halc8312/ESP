# Block D-4 実装報告

- 作業日: 2026-03-10
- 対象: 公開カタログのアクセス解析

---

## 1. 実施内容

### 1-1. アクセスログモデル

- `models.py` に `CatalogPageView` を追加
- 保存項目は以下
  - `pricelist_id`
  - `viewed_at`
  - `ip_hash`
  - `user_agent_short`
  - `referrer_domain`
  - `product_id`
- `PriceList.page_views` relationship も追加した

### 1-2. 記録処理

- `routes/catalog.py` に以下を追加
  - IP を短いハッシュへ変換する helper
  - Mobile/Desktop 判定 helper
  - referrer domain 抽出 helper
  - `record_page_view()`
- `catalog_view()` で価格表ページ表示を記録
- `catalog_product_detail()` で商品詳細モーダルの表示を記録
- 記録失敗時でも公開ページ表示は止めないようにした

### 1-3. Analytics 画面

- `GET /pricelists/<id>/analytics` を追加
- owner チェック付きで以下を集計するようにした
  - 総ページビュー
  - 推定ユニーク訪問者数
  - 直近7日 / 30日の PV
  - 商品詳細モーダル閲覧数
  - デバイス比率
  - 流入カテゴリ
  - 人気商品
  - 最近のアクセス
- Chart.js を使ってグラフ表示する `pricelist_analytics.html` を新規作成した

### 1-4. 導線

- `templates/pricelist_list.html` に `📊 解析` ボタンを追加
- `templates/pricelist_items.html` に `📊 アクセス解析` ボタンを追加

---

## 2. 既存機能への配慮

- 公開カタログの token と URL 構造は変更していない
- アクセス記録は別 session で行い、公開ページのレスポンスに影響しにくいようにした
- IP は生値を保存せずハッシュ化して保存している
- 商品詳細モーダル、レイアウト切替、価格換算処理とは責務を分けて追加した

---

## 3. 検証

### テンプレート

- `python -c "from app import app; [app.jinja_env.get_template(name) for name in ('catalog.html', 'pricelist_analytics.html', 'pricelist_list.html', 'pricelist_items.html')]; print('D4_TEMPLATES_OK')"`
  - `D4_TEMPLATES_OK`

### 追加テスト

- `pytest tests/test_e2e_routes.py -q -k "catalog_view_records_page_view or catalog_product_detail_records_product_view or pricelist_analytics or pricelist or catalog_product_detail"`
  - `11 passed`

確認内容:

- 公開カタログ閲覧で page view が記録される
- 商品詳細モーダル JSON 閲覧で `product_id` 付き記録が残る
- analytics 画面は owner のみ見られる
- analytics 画面に主要指標とグラフ要素が表示される

### 回帰テスト

- `pytest tests -q`
  - `120 passed`

---

## 4. 資料更新

- `docs/UNIFIED_ROADMAP.md` の D-4 を完了に更新
- `docs/specs/CURRENT_ARCHITECTURE.md` に以下を反映
  - `CatalogPageView`
  - `/pricelists/<id>/analytics`
  - catalog 側の記録処理
  - Block D 完了状態

---

## 5. 現時点の状態

- Block A: 完了
- Block B: 完了
- Block C: 完了
- Block D: 完了

残る主な未実施項目は、実サイト向けの自動スモークテスト整備と `datetime.utcnow()` 系の技術負債整理。
