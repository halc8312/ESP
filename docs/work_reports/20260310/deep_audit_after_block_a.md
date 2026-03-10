# 2026-03-10 深掘り監査報告書

## 目的
Block A 完了後に、やり残し・更新漏れ・将来の事故要因がないかを深く調査した結果をまとめる。

## 実施した確認
- `pytest tests -q` 実行
- `pytest test_scrapling.py test_mercari_html.py test_mercari_speed.py test_single_csv.py -q` 実行
- `python -m compileall .` 実行
- `rg` による残存参照・未実装機能・文言残り・資料更新漏れの検索
- `docs/UNIFIED_ROADMAP.md` と現行コードの突合

## 結論
- **コードとしての Stage 4b（Block A）は概ね完了している**
- **重大な回帰不具合は今回の監査では検出していない**
- ただし、**資料更新漏れ**、**Phase C/D の未着手**、**UI 文言の取り残し**、**手動ライブ確認未実施** は残っている

## 実行結果
- `pytest tests -q` → **92 passed**
- `pytest test_scrapling.py test_mercari_html.py test_mercari_speed.py test_single_csv.py -q` → **1 passed**
  - 備考: ここで通ったのは pytest 形式のもののみ。`test_scraping_real.py` などはスクリプトであり自動収集対象ではない
- `python -m compileall .` → 構文エラーなし

## Findings

### 1. Stage 4b 完了後のドキュメント更新が未完了
**重要度: 高**

根拠:
- `docs/specs/README.md:61` に Stage 4b が **未完了** のまま残っている
- `docs/UNIFIED_ROADMAP.md:21` でも 4b が **未完了** 表記のまま
- `docs/UNIFIED_ROADMAP.md:542` と `docs/UNIFIED_ROADMAP.md:620` で要求されている `docs/specs/STAGE_4_RESULTS.md` が未作成
- `docs/specs/CURRENT_ARCHITECTURE.md:86`, `:87`, `:92`, `:117` に Selenium / webdriver-manager / undetected-chromedriver / Google Chrome の現役前提が残っている

影響:
- 次の作業者やエージェントが「まだ Stage 4b は終わっていない」と誤認しやすい
- すでに削除した Selenium/Chrome 前提で再設計や逆戻りを起こす危険がある

### 2. Phase C / D は未着手に近い
**重要度: 高**

ロードマップ根拠:
- `docs/UNIFIED_ROADMAP.md:426` 以降に C-1〜C-5, D-1〜D-4 が定義されている
- `docs/UNIFIED_ROADMAP.md:557-568` のマスターリストでも未完了

コード根拠:
- `models.py:53-54` には `custom_title`, `custom_description` はあるが、`custom_title_en`, `custom_description_en` は存在しない
- `app.py:37-42` の migration にも英語フィールド追加がない
- `routes/api.py:11` は `/api/scrape/status/<job_id>` のみで、`inline-update` や `bulk-price` API がない
- `routes/scrape.py` に `register-selected` は存在しない
- `templates/product_detail.html` に SortableJS / `image_urls_json` を使う画像並び替え実装がない

影響:
- ロードマップ上の「Block A 完了後に続けるべき機能」はまだかなり残っている

### 3. C-4「抽出結果 同画面表示 + 選択登録」は未実装で、現行フローは逆向き
**重要度: 中**

根拠:
- `routes/scrape.py:72-181` の各経路で、抽出直後に `save_scraped_items_to_db(...)` を呼んでいる
- `routes/scrape.py:263`, `:281` は待機ページ → 結果ページの表示だけ
- `docs/UNIFIED_ROADMAP.md:474` は `POST /scrape/register-selected` を要求しているが現状未実装

影響:
- 将来 C-4 を入れるときに、現行の「即時保存」設計を切り替える必要がある
- UI だけでなく route の責務分離が必要

### 4. B-8「商品編集ページのコンパクト化」はまだ未完了
**重要度: 中**

根拠:
- `docs/UNIFIED_ROADMAP.md:412` に B-8 が定義されている
- `templates/product_detail.html:18`, `:66`, `:180`, `:199`, `:235` のように縦積みの `content-card` 構成が中心で、
  ロードマップの「2カラムレイアウト / モバイルアコーディオン / SEO デフォルト折りたたみ」までは到達していない

影響:
- Block B は B-1〜B-7 はかなり進んでいるが、編集画面だけ未整理が残る

### 5. eBay UI は消えているが、バックエンドと index 側の残骸がある
**重要度: 低**

根拠:
- `routes/main.py:201-237` で `default_ebay_*` をまだ組み立ててテンプレートに渡している
- `routes/export.py:173-247` に `export_ebay` ルートが残っている
- `templates/index.html` 側には eBay UI は見当たらない

評価:
- ロードマップ上も「バックエンド残置可」なのでバグではない
- ただし `routes/main.py` の eBay デフォルト値組み立ては今は dead context に近い

### 6. 「スクレイピング」表記の取り残しがある
**重要度: 低**

根拠:
- `templates/scrape_waiting.html:16`, `:50` に `スクレイピング中...`
- `routes/scrape.py:324` に `スクレイピング中にエラーが発生しました`
- `templates/scrape_result.html:4` は `取得結果`
- `templates/base.html:161` の bottom nav ラベルは `取得`

評価:
- B-5 の大筋は反映済みだが、文言統一は完全ではない

### 7. 実サイトへのライブ疎通は自動テスト化されていない
**重要度: 中**

根拠:
- `test_scraping_real.py:22-214` は実サイト向けスモークテストだが、`if __name__ == "__main__":` スクリプトであり pytest 自動収集対象ではない
- `test_scraping_real.py:166` には `WebDriverManager race conditions` という旧前提コメントも残っている
- `test_scrapling.py:7` や `test_mercari_speed.py:32` も手動実行寄り

影響:
- 今回の Stage 4b はユニットテスト・E2E テストでは通っているが、
  Yahoo / Offmall / Yahuoku / Surugaya / SNKRDUNK の**実サイト応答変化**までは保証していない

### 8. ローカル `.venv` には Selenium 系パッケージがまだ残っている
**重要度: 低**

根拠:
- `.venv/Lib/site-packages/selenium` が存在
- `.venv/Lib/site-packages/webdriver_manager` が存在
- `.venv/Lib/site-packages/undetected_chromedriver` が存在

評価:
- `requirements.txt` からは削除済みなので、デプロイ成果物の問題ではない
- ただしローカル venv が古いままだと「本当は未宣言依存なのに動く」偽陽性を生みやすい

### 9. `datetime.utcnow()` の非推奨警告が広く残っている
**重要度: 低**

根拠:
- `routes/products.py:133`
- `services/monitor_service.py:109`
- `services/product_service.py:24`
- `routes/import_routes.py:235-250`
- `routes/trash.py:25`, `:53`, `:128`
- `routes/pricelist.py:113`, `:153`, `:163`, `:263`
- `models.py` の複数列 default

評価:
- 今すぐ壊れる話ではない
- ただし Python/SQLAlchemy 周辺の将来更新で順次手当てが必要

## 総評
現時点での残件は次の3種類に分かれる。
1. **本当に未完了のロードマップ項目**
   - B-8
   - C-1〜C-5
   - D-1〜D-4
2. **完了済みだが資料に反映されていないもの**
   - Stage 4b 完了表記
   - `STAGE_4_RESULTS.md`
   - `CURRENT_ARCHITECTURE.md` 等の現状反映
3. **小さな残骸・運用上の注意**
   - eBay dead context
   - 文言の取り残し
   - 手動ライブ試験未実施
   - ローカル `.venv` の古い依存

## 優先度順の次アクション提案
1. `docs/specs/STAGE_4_RESULTS.md` を作成し、`docs/specs/README.md` / `docs/UNIFIED_ROADMAP.md` / `docs/specs/CURRENT_ARCHITECTURE.md` の状態表記を更新する
2. `test_scraping_real.py` を pytest 収集可能なスモークテストへ再構成するか、少なくとも旧 Selenium コメントを除去する
3. Block B-8 と Block C のどちらを先にやるか決めて着手する
4. 必要ならローカル `.venv` を作り直して、Selenium 系が本当に不要なことを再確認する
