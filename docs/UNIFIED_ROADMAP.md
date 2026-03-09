# ESP 統合ロードマップ

> **このドキュメントの目的**  
> `reports.md`（UI/機能開発 Phase 計画）と `docs/specs/`（Playwright 移行 Stage 計画）を  
> **一本化**したロードマップです。両計画を別々に進めると同一ファイルへの競合変更が発生するため、  
> このドキュメントを **唯一の実行計画** として使用してください。

---

## 1. 現状分析（2026-03-09 時点）

### 1-1. Playwright 移行の完了状況

| Stage | 名称                          | 状態       | 証跡                                      |
|-------|-------------------------------|------------|-------------------------------------------|
| 0     | キューシステム構築              | ✅ 完了    | `services/scrape_queue.py` 実装済み        |
| 1     | ラクマ Playwright 移行         | ✅ 完了    | `docs/specs/STAGE_1_RESULTS.md` 参照      |
| 2     | メルカリパトロール Playwright 移行 | ✅ 完了 | `_BROWSER_SITES = frozenset()`            |
| 3     | メルカリ全体 Playwright 移行   | ✅ 完了    | `mercari_db.py` に Selenium 残存なし      |
| 4a    | パトロール層 Selenium 削除      | ✅ 完了    | `monitor_service.py` driver 渡し廃止済み  |
| 4b    | DB スクレイピング層 Selenium 削除 | ❌ 未完了 | 後述「未完了タスク詳細」参照               |

### 1-2. 未完了タスク詳細（Stage 4b）

#### Selenium が残存しているファイル（優先度順）

| ファイル | 残存内容 | 影響 |
|---------|---------|------|
| `yahoo_db.py` | トップレベル `import selenium.*`、`create_driver()`、`scrape_item_detail(driver, url)` | **他ファイルが依存** (offmall, snkrdunk, yahuoku, surugaya, debug) |
| `offmall_db.py` | `from yahoo_db import create_driver`、`scrape_item_detail(driver, url)` | yahoo_db 依存 |
| `snkrdunk_db.py` | `from yahoo_db import create_driver`、`scrape_item_detail(driver, url)` | yahoo_db 依存 |
| `yahuoku_db.py` | `from yahoo_db import create_driver` | yahoo_db 依存 |
| `surugaya_db.py` | `_fetch_soup_with_selenium()`、`_should_use_selenium_fallback()` | 遅延 import で yahoo_db 依存 |
| `services/patrol/yahoo_patrol.py` | `_fetch_with_selenium()`（デッドコード） | selenium 遅延 import |
| `services/patrol/offmall_patrol.py` | `_fetch_with_selenium()`（デッドコード） | selenium 遅延 import |
| `services/patrol/snkrdunk_patrol.py` | `_fetch_with_selenium()`（デッドコード） | selenium 遅延 import |
| `services/patrol/yahuoku_patrol.py` | `_fetch_with_selenium()`（デッドコード） | selenium 遅延 import |
| `services/patrol/surugaya_patrol.py` | `_fetch_with_selenium()`（デッドコード） | selenium 遅延 import |
| `requirements.txt` | `selenium`, `webdriver-manager`, `undetected-chromedriver` | Docker イメージサイズ |
| `Dockerfile` | Google Chrome インストールブロック | Docker イメージ ~700MB 超過 |
| `debug_scrape.py` | `from yahoo_db import create_driver` | デバッグ用（削除対象） |
| `debug_children.py` | `from yahoo_db import create_driver` | デバッグ用（削除対象） |
| `debug_variant_json.py` | `import selenium.*` | デバッグ用（削除対象） |

#### 重要な発見：各 DB ファイルの構造

各 DB ファイルは既に **2 つの実装パス** を持っている：

```
scrape_item_detail(driver, url)       ← Selenium（削除対象）
scrape_item_detail_light(url)         ← Scrapling HTTP（維持）
scrape_single_item(url, headless)     ← Selenium（Scrapling に置き換え）
scrape_search_result(...)             ← Selenium（Scrapling に置き換え）
```

`scrape_item_detail_light()` は既に Scrapling で実装されており、  
`scrape_single_item()` / `scrape_search_result()` 内の Selenium 呼び出しをこれに切り替えるだけで移行完了できる見通し。

### 1-3. UI/機能開発の完了状況

`tasks.md` / `DEVELOPMENT_STATUS.md` 記載のフェーズ：

| フェーズ | 完了状況 |
|---------|---------|
| フェーズ1: 複数バリエーション対応 | ✅ 完了 |
| フェーズ2: 認証とセキュリティ     | ✅ 完了 |
| フェーズ3: 運用効率化（画像編集・一括編集・eBay連携） | ❌ 一部未実装 |

`reports.md` 記載の改善提案：

| 分類 | 機能数 | 完了数 |
|------|--------|--------|
| 商品一覧ページ (1-1 〜 1-11) | 11 | 0 |
| 商品編集ページ (2-1 〜 2-3)  | 3  | 0 |
| 商品抽出ページ (3-1 〜 3-6)  | 6  | 0 |
| 価格表管理ページ (4-1 〜 4-3) | 3  | 0 |
| **合計**                     | **23** | **0** |

---

## 2. コンフリクトリスク分析

### なぜ別々に進めると競合するのか

以下のファイルは **Stage 4b** と **reports.md Phase** の両方が変更対象となっている：

| ファイル | Stage 4b の変更 | reports.md の変更 |
|---------|----------------|------------------|
| `requirements.txt` | selenium 系削除 | 翻訳API等の新規追加 |
| `Dockerfile` | Chrome ブロック削除 | （直接の変更なし） |
| `routes/scrape.py` | Selenium import 確認 | AJAX 対応、チェックボックス登録 |
| `templates/scrape_form.html` | （直接の変更なし） | 「商品抽出」文言変更、コンパクト化 |
| `models.py` | （直接の変更なし） | `custom_title_en`、`CatalogPageView` 追加 |
| `offmall_db.py` | Selenium 除去 | （直接の変更なし） |
| `snkrdunk_db.py` | Selenium 除去 | （直接の変更なし） |

### 具体的なコンフリクトシナリオ

1. **シナリオA**: `requirements.txt` に翻訳 API を追加（reports.md Phase 2）した後、  
   `selenium` 等を削除（Stage 4b）→ マージ競合
2. **シナリオB**: `routes/scrape.py` に AJAX 対応を追加（reports.md Phase 2）した後、  
   Selenium import チェックで誤って変更→ 意図しない regression
3. **シナリオC**: Stage 4b を未完了のまま reports.md を実装すると、  
   Docker イメージが Chrome 込みのまま肥大化し続ける

---

## 3. 統合実行計画

> **原則**:  
> - Stage 4b（Selenium 削除）を **reports.md Phase より先に** 完了させる  
> - `requirements.txt` と `Dockerfile` の変更は Stage 4b として **一度だけ** 行う  
> - 各フェーズ完了後にテストを実行し、次フェーズに進む

### フェーズ全体像

```
【Block A: Selenium 完全削除】（既存計画の完結）
  A-1: デバッグスクリプト削除
  A-2: patrol 層のデッドコード削除（_fetch_with_selenium メソッド群）
  A-3: yahoo_db.py Selenium 除去（最重要：他ファイルが依存）
  A-4: offmall_db.py / snkrdunk_db.py / yahuoku_db.py Selenium 除去
  A-5: surugaya_db.py Selenium 除去
  A-6: requirements.txt・Dockerfile 最終クリーンアップ
  ↓
【Block B: UI 改善 Phase 1】（即時着手可能・リスク低）
  B-1 〜 B-8: テンプレート・CSS のみ変更（DB スキーマ変更なし）
  ↓
【Block C: 機能追加 Phase 2】（バックエンド・DB 変更あり）
  C-1 〜 C-5: 新規 API エンドポイント・DB スキーマ変更
  ↓
【Block D: 高度な機能 Phase 3】（外部 API 依存あり）
  D-1 〜 D-4: 画像管理・カタログ拡張・アクセス解析
```

---

## 4. Block A: Selenium 完全削除（Stage 4b 完結）

### A-1. デバッグスクリプト削除

**対象ファイル:**

```bash
git rm debug_scrape.py debug_children.py debug_variant_json.py
```

**確認事項:** 各ファイルに移行後も必要なロジックがないことを確認してから削除する。  
`debug_yahoo_repro.py` も Selenium フォールバックなしで動作するか確認すること。

---

### A-2. patrol 層のデッドコード削除

**背景:** `monitor_service.py` では `patrol.fetch(url)` を driver 引数なしで呼び出しているため、  
各 patrol クラスの `_fetch_with_selenium()` メソッドは **完全なデッドコード**。  
削除しても動作に影響しない。

**対象ファイルと削除内容:**

| ファイル | 削除するメソッド | 削除行数（概算） |
|---------|---------------|----------------|
| `services/patrol/yahoo_patrol.py` | `_fetch_with_selenium()` + `fetch()` の条件分岐 | ~120 行 |
| `services/patrol/offmall_patrol.py` | `_fetch_with_selenium()` + `fetch()` の条件分岐 | ~80 行 |
| `services/patrol/snkrdunk_patrol.py` | `_fetch_with_selenium()` + `fetch()` の条件分岐 | ~50 行 |
| `services/patrol/yahuoku_patrol.py` | `_fetch_with_selenium()` + `fetch()` の条件分岐 | ~70 行 |
| `services/patrol/surugaya_patrol.py` | `_fetch_with_selenium()` + `fetch()` の条件分岐 | ~60 行 |

**変更パターン（各ファイル共通）:**

```python
# 変更前
def fetch(self, url: str, driver=None) -> PatrolResult:
    if driver is None:
        return self._fetch_with_scrapling(url)
    return self._fetch_with_selenium(url, driver)

def _fetch_with_selenium(self, url: str, driver) -> PatrolResult:
    from selenium.webdriver.common.by import By
    # ... Selenium コード（削除） ...

# 変更後
def fetch(self, url: str, driver=None) -> PatrolResult:
    """driver 引数は後方互換のため保持（使用しない）"""
    return self._fetch_with_scrapling(url)
# _fetch_with_selenium メソッド 削除
```

---

### A-3. yahoo_db.py Selenium 除去（最重要）

**背景:** `yahoo_db.py` は以下の理由で **最優先** で対処する：
1. `create_driver()` が offmall_db, snkrdunk_db, yahuoku_db, surugaya_db, debug scripts から依存されている
2. トップレベルの `import selenium.*` があるため、selenium を requirements.txt から削除すると **他ファイルのインポート時にエラー** になる

**現状の関数構造:**

```python
# 現状
scrape_item_detail(driver, url)      # Selenium 必須
scrape_item_detail_light(url)        # Scrapling 実装済み ← これを維持
scrape_single_item(url, headless)    # 内部で create_driver() を呼ぶ
scrape_search_result(...)            # 内部で create_driver() を呼ぶ
create_driver(headless)              # Chrome ドライバー生成
```

**移行方針:**

1. `import selenium.*` をすべて削除（トップレベル）
2. `create_driver()` 関数を削除
3. `scrape_item_detail(driver, url)` → `driver` 引数不要な形に書き換え、  
   内部実装を `scrape_item_detail_light()` と統合（Scrapling HTTP 版）
4. `scrape_single_item()` 内の `create_driver()` 呼び出しを削除し、  
   Scrapling 版に統一
5. `scrape_search_result()` 内の Selenium コードを Scrapling 版に置き換え

**具体的な置き換えパターン:**

```python
# 変更前: scrape_single_item
def scrape_single_item(url: str, headless: bool = True):
    driver = create_driver(headless=headless)
    try:
        result = scrape_item_detail(driver, url)
    finally:
        driver.quit()

# 変更後: scrape_single_item
def scrape_single_item(url: str, headless: bool = True):
    """headless 引数は後方互換のため保持（使用しない）"""
    return scrape_item_detail_light(url)
```

---

### A-4. offmall_db.py / snkrdunk_db.py / yahuoku_db.py Selenium 除去

**背景:** A-3 完了後に実施。`yahoo_db.create_driver` への依存を断ち切る。  
各ファイルも `scrape_item_detail_light()` が既に Scrapling 版として実装されている。

**移行方針（A-3 と同様のパターン）:**

1. `from yahoo_db import create_driver` を削除
2. `from selenium.*` import を削除
3. `scrape_item_detail(driver, url)` を `scrape_item_detail_light(url)` にリダイレクト
4. `scrape_single_item()` / `scrape_search_result()` 内の driver 生成コードを削除

---

### A-5. surugaya_db.py Selenium 除去

**対象コード:**

```python
# 削除対象
def _should_use_selenium_fallback() -> bool: ...

def _fetch_soup_with_selenium(url: str, headless: bool = True, wait_seconds: int = 20):
    from yahoo_db import create_driver  # ← A-3 で create_driver 削除後は ImportError となる
    ...
```

**移行方針:**
- `_should_use_selenium_fallback()` → 削除（常に False を返す関数のため）
- `_fetch_soup_with_selenium()` → 削除（Scrapling HTTP 版のみで十分な場合）  
  または Playwright（StealthyFetcher）版に書き換え

> ⚠️ **A-3 との依存関係:** A-3 で `yahoo_db.create_driver` を削除すると、  
> `surugaya_db.py` の遅延 import `from yahoo_db import create_driver` は  
> 実行時に `ImportError` となる。**A-3 と A-5 は同一コミット内で実施**すること。

---

### A-6. requirements.txt・Dockerfile 最終クリーンアップ

**A-1〜A-5 完了後に実施。**

#### requirements.txt 変更

```diff
- selenium
- webdriver-manager
- undetected-chromedriver
```

確認コマンド（削除前に実行）:
```bash
grep -rn "selenium\|create_driver\|webdriver_manager\|undetected_chromedriver" \
    --include="*.py" . | grep -v "test_\|# " | grep -v ".git"
# → 結果が 0 件であることを確認してから削除
```

#### Dockerfile 変更

```dockerfile
# 削除するブロック全体
RUN set -eux \
    && mkdir -p /usr/share/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor --yes -o /usr/share/keyrings/google-linux-signing-keyring.gpg \
    && echo "deb [arch=amd64 ..." \
    > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*
```

また `wget`、`gnupg`、`unzip` も Chrome 専用のため apt-get install から削除する。  
`curl` は Playwright が必要とする可能性があるため**保持**する。

**期待効果:**

| 指標                    | 変更前   | 変更後   |
|-------------------------|----------|----------|
| Docker イメージサイズ   | ~1.5 GB  | ~800 MB  |
| requirements.txt 行削減 | +3 行削除 | —       |
| Render デプロイ時間     | 長い     | 短い     |

#### 検証チェックリスト（Block A 完了確認）

```bash
# 1. Selenium 残存確認（0件であること）
grep -rn "selenium\|create_driver\|webdriver_manager\|undetected_chromedriver" \
    --include="*.py" . | grep -v ".git" | grep -v "test_"

# 2. テスト全通過
python -m pytest tests/ -v

# 3. インポート確認
python -c "import yahoo_db; import offmall_db; import snkrdunk_db; print('OK')"
```

---

## 5. Block B: UI 改善 Phase 1（reports.md Phase 1 対応）

> **前提:** Block A 完了後に実施。  
> **特徴:** DB スキーマ変更なし。テンプレート・CSS・軽微な JS のみ変更。

### B-1. eBay 関連項目削除（優先度：🔴 高）

**変更ファイル:** `templates/index.html`, `routes/export.py`（任意）

- `index.html` から eBay 詳細設定 `<details>` ブロックを削除（モバイル・PC 両方）
- 「eBay File Exchange 用 CSV」ボタンを削除
- バックエンドの `export_ebay` ルートは残置可（UI だけ削除）

---

### B-2. テーブル列の整理（優先度：🔴 高）

**変更ファイル:** `templates/index.html`

以下を一括対応（変更量が少なく相互に無関係なため）：

| # | 変更内容 |
|---|---------|
| 1-3 | 「サイト」列を削除（`<th class="hide-mobile">サイト</th>` と対応 `<td>` ） |
| 1-4 | 「画像枚数」列を削除（`<th class="hide-mobile">画像枚数</th>` と対応 `<td>`） |
| 1-5 | 「ステータス」列 → 「在庫」列（列名変更＋「在庫あり/なし」バッジ表示） |
| 1-6 | 「元URL」列 → 「抽出サイト」列（列名変更＋サイト名テキストをリンク化） |

---

### B-3. 価格列二段表示（優先度：🔴 高）

**変更ファイル:** `templates/index.html`, `static/css/*.css`

- 「仕入: ¥X」/ 「販売: ¥X」の二段表示
- CSS のみで対応可能

---

### B-4. 検索フィルタ コンパクト化（優先度：🔴 高）

**変更ファイル:** `templates/index.html`

- PC 用フィルタと モバイル用フィルタを統合（重複コード排除）
- 常時表示: キーワード / ステータス / 並び順
- 折りたたみ（詳細フィルタ）: サイト / 価格帯 / 変更フィルタ

---

### B-5. 「スクレイピング」→「商品抽出」文言変更（優先度：🔴 高）

**変更ファイル:** `templates/scrape_form.html`, `templates/scrape_result.html`, `templates/base.html`

- UI テキストのみ変更（URL パス `/scrape/` はそのまま）

---

### B-6. ローディング画面追加（優先度：🔴 高）

**変更ファイル:** `templates/scrape_form.html`

- フォームサブミット時にオーバーレイを表示する JS（数行で対応）
- 既存のキューシステム（非同期ポーリング）と統合して進捗表示

---

### B-7. 検索フォーム コンパクト化（優先度：🔴 高）

**変更ファイル:** `templates/scrape_form.html`

- 方法1（URL）と方法2（検索）のカードを統合
- 全フィールドを1行横並びに
- ソート順・カテゴリIDを折りたたみへ移動

---

### B-8. 商品編集ページ コンパクト化（優先度：🟡 中）

**変更ファイル:** `templates/product_detail.html`

- 2カラムレイアウト（PC）/ アコーディオン（モバイル）
- SEO セクションをデフォルト折りたたみ

---

## 6. Block C: 機能追加 Phase 2（reports.md Phase 2 対応）

> **前提:** Block B 完了後に実施。  
> **特徴:** DB スキーマ変更・新規 API エンドポイントあり。

### C-1. 商品名英語フィールド追加（優先度：🟡 中）

**変更ファイル:** `models.py`, `app.py`（マイグレーション追加）

```python
# models.py に追加
custom_title_en = Column(String)
custom_description_en = Column(Text)
```

マイグレーション（`app.py` の `run_migrations()` に追加）:
```python
"ALTER TABLE products ADD COLUMN IF NOT EXISTS custom_title_en VARCHAR",
"ALTER TABLE products ADD COLUMN IF NOT EXISTS custom_description_en TEXT",
```

**外部依存（任意）:** DeepL API または Google Cloud Translation API  
API キー設定前でも UI は動作する（翻訳なしで手動入力のみ）

---

### C-2. インライン編集機能（優先度：🟡 中）

**変更ファイル:** `templates/index.html`, `routes/products.py`（または `routes/api.py`）

```
新規エンドポイント: PATCH /api/products/<id>/inline-update
許可フィールド: selling_price, custom_title_en
```

---

### C-3. 一括価格設定（優先度：🟡 中）

**変更ファイル:** `templates/index.html`, `routes/products.py`（または `routes/pricing.py`）

```
新規エンドポイント: POST /api/products/bulk-price
設定方式: margin（利益率）/ fixed_add / fixed / margin_plus_fixed / reset
```

**注意:** 利益率は販売価格に対する利益の割合として計算（既存 UI の定義に準拠）  
`selling_price = cost / (1 - margin/100)` — **0 ≤ margin < 100 のバリデーション必須**  
`margin` が 99 の場合 `selling_price = cost × 100`（仕入価格の100倍）となるため、  
UI 側では上限を 99（または実務的な上限値）に設定し、ユーザーに警告を表示することを推奨する。

---

### C-4. 抽出結果 同画面サムネイル表示 + 選択登録（優先度：🟡 中）

**変更ファイル:** `templates/scrape_form.html`, `routes/scrape.py`

```
新規エンドポイント: POST /scrape/register-selected
```

- AJAX でフォームサブミット → JSON 結果を同ページに描画
- チェックボックスで選択した商品のみ DB 登録

---

### C-5. 画像削除・並べ替え・追加（優先度：🟡 中）

**変更ファイル:** `templates/product_detail.html`, `routes/products.py`

- SortableJS（CDN）でドラッグ&ドロップ
- `image_urls_json` 隠しフィールドで順序を送信
- 画像アップロードは Cloudinary（A案）または後回し

---

## 7. Block D: 高度な機能 Phase 3（reports.md Phase 3 対応）

> **前提:** Block C 完了後に実施。外部 API・追加ライブラリが必要なものを含む。

### D-1. 商品手動追加（優先度：🟡 中）

**変更ファイル:** `templates/product_manual_add.html`（新規）, `routes/main.py`

---

### D-2. カタログレイアウト切替（優先度：🟢 低）

**変更ファイル:** `models.py`（`PriceList.layout` 追加）, `templates/catalog.html`

---

### D-3. 商品詳細モーダル（優先度：🟡 中）

**変更ファイル:** `templates/catalog.html`, `routes/catalog.py`

---

### D-4. アクセス解析（優先度：🟢 低）

**変更ファイル:** `models.py`（`CatalogPageView` 追加）, `routes/catalog.py`, `templates/pricelist_analytics.html`（新規）

---

## 8. 実装チェックリスト（マスターリスト）

### Block A: Selenium 完全削除

- [ ] **A-1** `debug_scrape.py`, `debug_children.py`, `debug_variant_json.py` 削除
- [ ] **A-2** 各 patrol ファイルの `_fetch_with_selenium()` デッドコード削除
  - [ ] `services/patrol/yahoo_patrol.py`
  - [ ] `services/patrol/offmall_patrol.py`
  - [ ] `services/patrol/snkrdunk_patrol.py`
  - [ ] `services/patrol/yahuoku_patrol.py`
  - [ ] `services/patrol/surugaya_patrol.py`
- [ ] **A-3** `yahoo_db.py` Selenium 完全除去（`create_driver` 削除含む）
- [ ] **A-4** `offmall_db.py`, `snkrdunk_db.py`, `yahuoku_db.py` Selenium 除去
- [ ] **A-5** `surugaya_db.py` Selenium フォールバック除去
- [ ] **A-6** `requirements.txt` から `selenium`, `webdriver-manager`, `undetected-chromedriver` 削除
- [ ] **A-6** `Dockerfile` から Chrome インストールブロック削除
- [ ] `pytest tests/ -v` 全通過確認
- [ ] `docs/specs/STAGE_4_RESULTS.md` 作成

### Block B: UI 改善 Phase 1

- [ ] **B-1** eBay 関連項目削除
- [ ] **B-2** テーブル列整理（サイト削除、画像枚数削除、ステータス→在庫、元URL→抽出サイト）
- [ ] **B-3** 価格列二段表示（仕入/販売）
- [ ] **B-4** 検索フィルタ コンパクト化（PC・モバイル統合）
- [ ] **B-5** 「スクレイピング」→「商品抽出」文言変更
- [ ] **B-6** ローディング画面追加
- [ ] **B-7** 検索フォーム コンパクト化
- [ ] **B-8** 商品編集ページ コンパクト化

### Block C: 機能追加 Phase 2

- [ ] **C-1** 英語タイトル・説明フィールド追加（DB マイグレーション）
- [ ] **C-2** インライン編集 API（selling_price, custom_title_en）
- [ ] **C-3** 一括価格設定 API
- [ ] **C-4** 抽出結果 同画面表示 + 選択登録
- [ ] **C-5** 画像削除・並べ替え（SortableJS）

### Block D: 高度な機能 Phase 3

- [ ] **D-1** 商品手動追加機能
- [ ] **D-2** カタログレイアウト切替
- [ ] **D-3** 商品詳細モーダル
- [ ] **D-4** アクセス解析機能

---

## 9. 旧計画ドキュメントとの対応表

| 旧ドキュメント | 対応する本ロードマップの項目 | 状態 |
|--------------|---------------------------|------|
| `docs/specs/STAGE_0_QUEUE_SYSTEM.md` | 完了済み | ✅ |
| `docs/specs/STAGE_1_RAKUMA_PLAYWRIGHT.md` | 完了済み（STAGE_1_RESULTS.md 参照） | ✅ |
| `docs/specs/STAGE_2_MERCARI_PATROL.md` | 完了済み | ✅ |
| `docs/specs/STAGE_3_MERCARI_FULL.md` | 完了済み | ✅ |
| `docs/specs/STAGE_4_SELENIUM_REMOVAL.md` | Block A に統合 | ❌ |
| `reports.md` Phase 1 / 優先度🔴 | Block B に統合 | ❌ |
| `reports.md` Phase 2 / 優先度🟡 | Block C に統合 | ❌ |
| `reports.md` Phase 3 / 優先度🟢 | Block D に統合 | ❌ |
| `tasks.md` フェーズ1〜2 | 完了済み | ✅ |
| `tasks.md` フェーズ3 | Block C・D に統合 | ❌ |

---

## 10. 注意事項

### `BROWSER_SITES` と `_BROWSER_SITES` の現状

```python
# services/scrape_queue.py
BROWSER_SITES = frozenset()   # 全サイトが HTTP/Playwright (Scrapling) 経由

# services/monitor_service.py
_BROWSER_SITES = frozenset()  # 全サイトがドライバーなしでパトロール可能
```

これらは **変更不要**。Block A の Selenium 削除後も同じ値を維持する。

### `_get_or_create_event_loop()` パターン

Scrapling の StealthyFetcher を ThreadPoolExecutor 内から呼び出す際は、  
`rakuma_db.py` で実装済みの `_get_or_create_event_loop()` パターンを参照すること。

### Render 本番環境の制約

- `--workers 1` 必須（インメモリシングルトンのため）
- `--max-requests 0` でバックグラウンドスレッド維持
- Playwright の `--no-sandbox`, `--disable-dev-shm-usage` フラグ必要

---

## 11. 移行完了後の最終クリーンアップ

Block A〜D が完了した段階で以下を実施する：

1. `docs/specs/STAGE_4_RESULTS.md` を作成（移行完了記録）
2. `docs/specs/MIGRATION_COMPLETE.md` を作成（全 Stage の完了サマリー）
3. `DEVELOPMENT_STATUS.md` を更新（最新の実装状況を反映）
4. `tasks.md` を本ドキュメントへの参照に置き換え（または削除）
5. `reports.md` の各機能に完了チェックマークを追記
