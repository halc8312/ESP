# 改善提案レポート

> Request.md に記載された要望に対する詳細な実装案をまとめたドキュメントです。  
> 各セクションは「現状」「要望」「実装案」「技術的詳細」の順で記述しています。

---

## 目次

1. [商品一覧ページ（`/`）](#1-商品一覧ページ)
2. [商品編集ページ（`/product/<id>`）](#2-商品編集ページ)
3. [商品抽出ページ（`/scrape/`）](#3-商品抽出ページ旧スクレイピングページ)
4. [価格表管理ページ（`/pricelists`）](#4-価格表管理ページ)

---

## 1. 商品一覧ページ

**対象URL:** `https://esp-1-kend.onrender.com/`  
**対象ファイル:** `templates/index.html`, `routes/main.py`

---

### 1-1. 検索項目をコンパクトにおさめる

**現状:**  
- PC用フィルタとモバイル用フィルタが別々に実装されており、`<details>` による折りたたみUIとフラットなUIが重複している。  
- eBay詳細設定（カテゴリID、ConditionID、PaymentProfile、ReturnProfile、ShippingProfile、PayPalEmail）が展開可能な形でエクスポートセクション内に存在している。

**要望:**  
検索フィルタエリアを画面上部に収まる程度にコンパクトにまとめる。

**実装案:**

#### A案（推奨）: 横並び1行レイアウト + アドバンスド折りたたみ

```
[ キーワード検索 ] [ ステータス▼ ] [ 並び順▼ ] [ 価格min ] [ 価格max ] [フィルタ適用] [クリア]
```

- 最もよく使うフィールド（キーワード、ステータス、並び順）のみを常時表示  
- 「詳細フィルタ」ボタンを押すとサイト・価格帯・変更フィルタが展開  
- モバイル・PCで同じUIを使用し、重複コードを排除  

**変更ファイル:** `templates/index.html`（PC用フィルタ `div.filter-section.desktop-only` とモバイル用 `details.mobile-collapsible.mobile-only` を統合して1つのフィルタセクションに置き換え）

---

### 1-2. eBay関連項目削除

**現状:**  
- `index.html` のエクスポートセクション内に eBay詳細設定（6フィールド）が `<details>` として存在している。  
- エクスポートボタン群に「eBay File Exchange 用 CSV」ボタンがある。  
- `routes/export.py` に `export_ebay` ルートが存在している。

**要望:**  
商品一覧画面からeBay関連のUI要素をすべて削除する。

**実装案:**

1. `templates/index.html` から以下を削除：
   - `<details class="collapsible-section mt-2">` の「eBay 詳細設定」ブロック（モバイル・PC両方）  
   - `<button ... formaction="{{ url_for('export_ebay') }}">eBay File Exchange 用 CSV</button>`（モバイル・PC両方）

2. eBay CSVエクスポート機能自体（`routes/export.py` の `export_ebay` ルート）はバックエンドとして残しても良いが、UI上は非表示にする。

> ⚠️ 注意: eBayへのエクスポート機能が完全に不要であれば、`routes/export.py` の `export_ebay` 関数ごと削除することも検討してください。

---

### 1-3. 表から「サイト」列を削除

**現状:**  
- 商品一覧テーブルに `<th class="hide-mobile">サイト</th>` 列があり、PCでのみ表示されている。  
- モバイルカード表示でも `<span>{{ p.site }}</span>` がメタ情報として表示されている。

**要望:**  
表から「サイト」列を削除する。

**実装案:**

- `templates/index.html` のテーブルヘッダから `<th class="hide-mobile">サイト</th>` を削除  
- テーブルの各行の `<td class="hide-mobile">{{ p.site }}</td>` を削除  
- モバイルカードのメタ表示 `<span>{{ p.site }}</span>` を削除  
- サイト別のフィルタバッジ（`div.site-stats-bar`）は残す（フィルタリング用途として有用）

> 💡 ヒント: サイト情報は「抽出サイト」列（要望1-5参照）でリンクとして表現されるため、独立した列としては不要です。

---

### 1-4. 表から「画像枚数」列を削除

**現状:**  
- テーブルに `<th class="hide-mobile">画像枚数</th>` 列があり、`img_count` 変数で枚数を表示している。

**要望:**  
表から「画像枚数」列を削除する。

**実装案:**

- `templates/index.html` のテーブルヘッダから `<th class="hide-mobile">画像枚数</th>` を削除  
- テーブル各行の `<td class="hide-mobile">{{ img_count }}</td>` を削除  
- テンプレート変数 `img_count` の計算ロジック（`{% set img_count = ... %}`）も削除してHTMLをすっきりさせる

---

### 1-5. 「ステータス」列 → 「在庫」列に変更

**現状:**  
- テーブルに `<th>ステータス</th>` があり、`{{ p.last_status }}` をそのまま表示（値は「出品中」「売り切れ」など元サイトの文言）。

**要望:**  
- 列名を「ステータス」→「在庫」に変更  
- 表示値を「在庫あり」「在庫なし」に統一する

**実装案:**

**テンプレート変更（`templates/index.html`）:**

```html
<!-- ヘッダ変更 -->
<th>在庫</th>

<!-- セル表示変更 -->
<td>
  {% if p.last_status in ['出品中', 'active', 'on_sale', 'selling'] %}
    <span class="stock-badge stock-in">在庫あり</span>
  {% else %}
    <span class="stock-badge stock-out">在庫なし</span>
  {% endif %}
</td>
```

**CSS追加（`static/css/` 内の既存CSSに追記）:**

```css
.stock-badge {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
}
.stock-badge.stock-in {
    background: #dcfce7;
    color: #166534;
}
.stock-badge.stock-out {
    background: #fee2e2;
    color: #991b1b;
}
```

**バックエンド変更（`routes/main.py`）:**  
各サイトの `last_status` 値を「在庫あり」「在庫なし」に正規化するヘルパー関数を追加するか、テンプレート側でのフィルタで対応する。

サイト別ステータス値の対応表：
| サイト | 在庫ありと見なす値 |
|--------|------------------|
| メルカリ | `出品中`, `on_sale` |
| ヤフオク | `出品中`, `selling` |
| ラクマ | `出品中` |
| 駿河屋 | `在庫あり`, `在庫○` |
| Yahoo!ショッピング | `出品中` |
| SNKRDUNK | `販売中` |
| オフモール | `出品中` |

---

### 1-6. 「元URL」列 → 「抽出サイト」列に変更

**現状:**  
- `<th>元URL</th>` 列に `<a href="{{ p.source_url }}" target="_blank">開く</a>` として固定テキスト「開く」のリンクが表示されている。

**要望:**  
- 列名を「元URL」→「抽出サイト」に変更  
- 「開く」という固定テキストから、各サイト名のリンクテキストに変更（例：メルカリ、ヤフオク、ヤフショ、スニダン、駿河屋）

**実装案:**

```html
<!-- ヘッダ変更 -->
<th>抽出サイト</th>

<!-- セル表示変更 -->
<td>
  <a href="{{ p.source_url }}" target="_blank" rel="noopener">
    {{ p.site }}
  </a>
</td>
```

> `p.site` の値がすでにサイト名（「mercari」「yahuoku」など）として保存されているため、そのまま表示するか、以下のような表示名マッピングを使用する：

**Jinja2マクロ例:**

```jinja2
{% set site_display = {
    'mercari': 'メルカリ',
    'yahuoku': 'ヤフオク',
    'yahoo': 'ヤフショ',
    'snkrdunk': 'スニダン',
    'surugaya': '駿河屋',
    'rakuma': 'ラクマ',
    'offmall': 'オフモール'
} %}
<a href="{{ p.source_url }}" target="_blank" rel="noopener">
    {{ site_display.get(p.site, p.site) }}
</a>
```

---

### 1-7. 価格列を二段表示に変更（仕入価格 / 販売価格）

**現状:**  
- 「価格」列に `selling_price`（販売価格）を優先表示し、`last_price`（仕入価格）を小さく補足表示する部分的な二段表示がすでに実装されている。  
- ただし `selling_price` が設定されていない場合は仕入価格のみ表示される。

**要望:**  
価格列を明確な二段表示にして「仕入: ¥X」「販売: ¥X」の形式で常に両方を表示する。

**実装案:**

```html
<!-- テーブルヘッダ変更 -->
<th>価格</th>

<!-- セル表示変更 -->
<td class="price-cell">
    <div class="price-row price-cost">
        <span class="price-label">仕入:</span>
        <span class="price-value">
            {% if p.last_price is not none %}
                ¥{{ "{:,}".format(p.last_price) }}
            {% else %}
                —
            {% endif %}
        </span>
    </div>
    <div class="price-row price-sell">
        <span class="price-label">販売:</span>
        <span class="price-value" id="sell-price-{{ p.id }}">
            {% if p.selling_price is not none %}
                ¥{{ "{:,}".format(p.selling_price) }}
            {% else %}
                <span class="text-muted">未設定</span>
            {% endif %}
        </span>
    </div>
</td>
```

**CSS追加:**

```css
.price-cell { line-height: 1.6; }
.price-row { display: flex; gap: 4px; font-size: 13px; }
.price-label { color: #888; min-width: 38px; }
.price-cost .price-value { color: #333; }
.price-sell .price-value { color: #0066cc; font-weight: 600; }
```

---

### 1-8. 商品名列を二段表示に変更（日本語名 / 英語名）

**現状:**  
- 商品名列に `p.custom_title or p.last_title` のみを表示している（日本語）。  
- `Product` モデルに英語名フィールド（`custom_title_en` など）は存在しない。

**要望:**  
- 商品名を日本語と英語の二段表示にする  
- 英語名はデフォルトで自動翻訳する

**実装案:**

#### データベース変更（`models.py`）:

```python
class Product(Base):
    # ... 既存フィールド ...
    custom_title_en = Column(String)  # 英語タイトル（手動編集 or 自動翻訳）
    custom_description_en = Column(Text)  # 英語説明文
```

マイグレーション（Alembic または手動SQL）:

```sql
ALTER TABLE products ADD COLUMN custom_title_en VARCHAR;
ALTER TABLE products ADD COLUMN custom_description_en TEXT;
```

#### 翻訳機能:

**A案（推奨）: Google Cloud Translation API (無料枠あり)**  
- `services/translation_service.py` に翻訳ユーティリティを作成  
- 商品がスクレイピング/保存されたタイミングで非同期翻訳を実行  
- APIキーは環境変数 `GOOGLE_TRANSLATE_API_KEY` で管理

**B案: DeepL API (無料枠: 500,000文字/月)**  
- より自然な日本語→英語翻訳が得られる  
- 環境変数 `DEEPL_API_KEY` で管理

**C案: LibreTranslate（自己ホスト・無料）**  
- 外部APIへのコスト・依存なし  
- 翻訳品質はA/B案より劣る

**翻訳サービス実装例（B案 DeepL）:**

```python
# services/translation_service.py
import requests
import os

def translate_to_english(text: str) -> str:
    """DeepL APIを使って日本語テキストを英語に翻訳する"""
    api_key = os.getenv("DEEPL_API_KEY")
    if not api_key or not text:
        return ""
    
    response = requests.post(
        "https://api-free.deepl.com/v2/translate",
        data={
            "auth_key": api_key,
            "text": text,
            "source_lang": "JA",
            "target_lang": "EN-US",
        },
        timeout=10
    )
    if response.status_code == 200:
        return response.json()["translations"][0]["text"]
    return ""
```

#### テンプレート変更（`templates/index.html`）:

```html
<!-- テーブルヘッダ変更 -->
<th>商品名</th>

<!-- セル変更 -->
<td class="product-title-cell">
    <div class="title-ja">{{ p.custom_title or p.last_title }}</div>
    {% if p.custom_title_en %}
    <div class="title-en">{{ p.custom_title_en }}</div>
    {% else %}
    <div class="title-en text-muted">(翻訳なし)</div>
    {% endif %}
</td>
```

**CSS追加:**

```css
.title-ja { font-size: 13px; font-weight: 500; color: #333; }
.title-en { font-size: 11px; color: #888; margin-top: 2px; }
```

---

### 1-9. 販売価格・英語名のインライン編集

**現状:**  
- 商品一覧から価格や商品名を編集するには商品詳細ページに移動する必要がある。

**要望:**  
- 商品一覧の表示画面で販売価格と英語名を直接編集可能にする。

**実装案:**

#### フロントエンド（インライン編集UI）:

セルをクリックすると編集可能になる「クリックして編集（inline edit）」パターンを採用する。

```html
<!-- 販売価格セルの例 -->
<td class="price-cell">
    <div class="price-row price-sell">
        <span class="price-label">販売:</span>
        <span class="editable-price" 
              data-product-id="{{ p.id }}" 
              data-field="selling_price"
              data-value="{{ p.selling_price or '' }}"
              onclick="startInlineEdit(this)">
            {% if p.selling_price is not none %}
                ¥{{ "{:,}".format(p.selling_price) }}
            {% else %}
                <span class="text-muted editable-hint">クリックして入力</span>
            {% endif %}
        </span>
    </div>
</td>
```

```javascript
function startInlineEdit(el) {
    const field = el.dataset.field;
    const productId = el.dataset.productId;
    const currentValue = el.dataset.value;
    
    const input = document.createElement('input');
    input.type = field === 'selling_price' ? 'number' : 'text';
    input.value = currentValue;
    input.className = 'inline-edit-input';
    
    el.replaceWith(input);
    input.focus();
    input.select();
    
    input.addEventListener('blur', () => saveInlineEdit(input, productId, field));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') input.blur();
        if (e.key === 'Escape') cancelInlineEdit(input, el);
    });
}

async function saveInlineEdit(input, productId, field) {
    const value = input.value;
    try {
        const res = await fetch(`/api/products/${productId}/inline-update`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ field, value })
        });
        if (res.ok) {
            // 成功: 表示を更新
            const data = await res.json();
            // ... 表示更新処理
        }
    } catch (e) {
        alert('更新に失敗しました');
    }
}
```

#### バックエンド（新規APIエンドポイント追加）:

**`routes/products.py` に追加:**

```python
@products_bp.route('/api/products/<int:product_id>/inline-update', methods=['PATCH'])
@login_required
def inline_update_product(product_id):
    """インライン編集用の軽量PATCHエンドポイント"""
    data = request.get_json()
    field = data.get('field')
    value = data.get('value')
    
    # 許可フィールドのホワイトリスト
    ALLOWED_FIELDS = {'selling_price', 'custom_title_en'}
    if field not in ALLOWED_FIELDS:
        return jsonify({'error': 'Invalid field'}), 400
    
    session_db = SessionLocal()
    try:
        product = session_db.query(Product).filter_by(
            id=product_id, user_id=current_user.id
        ).first()
        if not product:
            return jsonify({'error': 'Not found'}), 404
        
        if field == 'selling_price':
            product.selling_price = int(value) if value else None
        elif field == 'custom_title_en':
            product.custom_title_en = value
        
        product.updated_at = datetime.utcnow()
        session_db.commit()
        return jsonify({'ok': True, 'value': value})
    finally:
        session_db.close()
```

---

### 1-10. 一括価格設定機能

**現状:**  
- 一括操作バー（`.batch-action-bar`）が存在し、タイトル接頭辞/接尾辞追加・文字列置換・アーカイブ移動・ゴミ箱移動が可能。  
- 価格に関する一括設定機能はない。

**要望:**  
チェックした商品だけ or 全体に対して一括価格設定できる機能を追加する。  
設定方法:  
- 利益率○○%（仕入価格 × (1 + 利益率) = 販売価格）  
- ○○円（固定額加算 or 固定額）  
- 利益率○○% ＋ ○○円（複合）  
- 元に戻す（selling_price を NULL にリセット）

**実装案:**

#### フロントエンド（モーダル UI）:

一括操作セレクトボックスに「価格を一括設定」オプションを追加し、選択するとモーダルが表示される。

```html
<!-- 一括操作セレクトに追加 -->
<option value="bulk_price">💰 価格を一括設定</option>

<!-- 一括価格設定モーダル -->
<div id="bulkPriceModal" class="modal" style="display:none;">
    <div class="modal-content">
        <h3>💰 一括価格設定</h3>
        <p id="bulkPriceTarget">選択中: <strong id="selectedCountForPrice">0</strong>件</p>
        
        <div class="form-group">
            <label>設定方式</label>
            <select id="bulkPriceMode" onchange="updateBulkPriceUI()">
                <option value="margin">利益率 (%)</option>
                <option value="fixed_add">固定額を加算 (円)</option>
                <option value="fixed">固定額に設定 (円)</option>
                <option value="margin_plus_fixed">利益率 (%) + 固定額 (円)</option>
                <option value="reset">元に戻す (販売価格をリセット)</option>
            </select>
        </div>
        
        <div id="bulkPriceMarginInput" class="form-group">
            <label>利益率 (%)</label>
            <input type="number" id="bulkMarginValue" min="0" max="99" step="1" value="20"
                   placeholder="例: 20 (20%の利益率)">
            <!-- 
                ※「利益率」は「販売価格に対する利益の割合」として計算します（既存UIの定義に準拠）。
                   利益率20%の場合: 利益 = 販売価格 × 0.20 → 販売価格 = 仕入価格 ÷ (1 - 0.20) = 仕入価格 × 1.25
                   ※「仕入価格に対する上乗せ率（markup）」ではありません（それは markup = 1.20 = 20%上乗せ）。
                   100%以上は設定不可（販売価格が無限大になるため）。
            -->
            <span class="text-muted text-small">利益率 = 利益 ÷ 販売価格 × 100。販売価格 = 仕入価格 ÷ (1 − 利益率/100)</span>
        </div>
        
        <div id="bulkPriceFixedInput" class="form-group" style="display:none;">
            <label>金額 (円)</label>
            <input type="number" id="bulkFixedValue" min="0" step="1" placeholder="例: 5000">
        </div>
        
        <div class="form-group">
            <label>対象</label>
            <select id="bulkPriceScope">
                <option value="selected">チェックした商品のみ</option>
                <option value="all">全商品（現在のフィルタ条件）</option>
            </select>
        </div>
        
        <div class="modal-actions">
            <button type="button" onclick="applyBulkPrice()">適用</button>
            <button type="button" onclick="closeBulkPriceModal()">キャンセル</button>
        </div>
    </div>
</div>
```

#### バックエンド（`routes/pricing.py` または `routes/products.py` に追加）:

```python
@products_bp.route('/api/products/bulk-price', methods=['POST'])
@login_required
def bulk_price_update():
    """一括価格設定"""
    data = request.get_json()
    mode = data.get('mode')          # 'margin', 'fixed_add', 'fixed', 'margin_plus_fixed', 'reset'
    scope = data.get('scope')        # 'selected' or 'all'
    product_ids = data.get('ids', [])
    margin = data.get('margin', 0)   # 利益率 (%)
    fixed = data.get('fixed', 0)     # 固定額 (円)
    
    ALLOWED_MODES = {'margin', 'fixed_add', 'fixed', 'margin_plus_fixed', 'reset'}
    if mode not in ALLOWED_MODES:
        return jsonify({'error': 'Invalid mode'}), 400
    
    # 利益率は 0以上100未満の値のみ受け付ける（100%以上は販売価格が無限大またはマイナスになるため）
    if mode in ('margin', 'margin_plus_fixed'):
        if not (0 <= margin < 100):
            return jsonify({'error': '利益率は0以上100未満の値を入力してください'}), 400
    
    session_db = SessionLocal()
    try:
        query = session_db.query(Product).filter_by(user_id=current_user.id)
        if scope == 'selected' and product_ids:
            query = query.filter(Product.id.in_(product_ids))
        
        products = query.all()
        updated = 0
        
        for p in products:
            if mode == 'reset':
                p.selling_price = None
            elif p.last_price is not None:
                cost = p.last_price
                if mode == 'margin':
                    # 「利益率」は販売価格に対する利益の割合（既存UIの定義に準拠）
                    # 利益率20% → 販売価格 = 仕入価格 ÷ (1 - 0.20) = 仕入価格 × 1.25
                    # ※利益率は必ず 0 以上 100 未満の値を受け付ける（バリデーション済み）
                    if 0 <= margin < 100:
                        p.selling_price = int(cost / (1 - margin / 100))
                elif mode == 'fixed_add':
                    p.selling_price = cost + int(fixed)
                elif mode == 'fixed':
                    p.selling_price = int(fixed)
                elif mode == 'margin_plus_fixed':
                    # 利益率部分 + 固定額上乗せ
                    if 0 <= margin < 100:
                        p.selling_price = int(cost / (1 - margin / 100)) + int(fixed)
            p.updated_at = datetime.utcnow()
            updated += 1
        
        session_db.commit()
        return jsonify({'ok': True, 'updated': updated})
    finally:
        session_db.close()
```

---

### 1-11. 商品の手動追加機能

**現状:**  
- 商品はスクレイピング経由でのみ追加可能。手動追加機能はない。

**要望:**  
自分の在庫品などを手動で商品登録できる機能を追加する。

**実装案:**

#### UI:

商品一覧ページ上部またはサイドバーに「＋ 商品を手動追加」ボタンを追加し、モーダルまたは専用ページへ遷移する。

**必要な入力フィールド:**
- 商品名（日本語）※必須
- 商品名（英語）任意
- 仕入価格 ※必須  
- 販売価格（任意）  
- 在庫状態（在庫あり/在庫なし）
- サイト名（手動入力 or 「手動登録」固定）
- 元URL（任意）
- 商品画像URL（任意、複数可）
- タグ（任意）

#### バックエンド（新規ルート追加）:

```python
@main_bp.route('/products/manual-add', methods=['GET', 'POST'])
@login_required  
def product_manual_add():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        if not title:
            flash('商品名は必須です', 'error')
            return redirect(url_for('product_manual_add'))
        
        db = SessionLocal()
        try:
            product = Product(
                user_id=current_user.id,
                site=request.form.get('site', '手動登録'),
                source_url=request.form.get('source_url', ''),
                last_title=title,
                custom_title=title,
                custom_title_en=request.form.get('title_en', ''),
                last_price=int(request.form.get('cost_price', 0) or 0),
                selling_price=int(request.form.get('selling_price') or 0) or None,
                last_status=request.form.get('status', '出品中'),
                status='active',
            )
            db.add(product)
            db.flush()
            
            # スナップショット作成
            image_urls = request.form.get('image_urls', '')
            snapshot = ProductSnapshot(
                product_id=product.id,
                title=title,
                price=product.last_price,
                status=product.last_status,
                image_urls=image_urls,
            )
            db.add(snapshot)
            
            # デフォルトバリアント作成
            variant = Variant(
                product_id=product.id,
                option1_value='Default Title',
                price=product.selling_price or product.last_price,
                inventory_qty=1,
            )
            db.add(variant)
            db.commit()
            flash(f'商品「{title}」を登録しました', 'success')
            return redirect(url_for('product_detail', product_id=product.id))
        finally:
            db.close()
    
    return render_template('product_manual_add.html')
```

---

## 2. 商品編集ページ

**対象URL:** `https://esp-1-kend.onrender.com/product/<id>`  
**対象ファイル:** `templates/product_detail.html`, `routes/products.py`

---

### 2-1. 入力エリアをコンパクトにしてスクロールが少ないレイアウト

**現状:**  
- 基本情報・バリエーション設定・分類・SEO・画像が縦に並んでいる。  
- 各セクションが独立した `content-card` になっており、スクロール量が多い。

**要望:**  
入力エリアをコンパクトにして、スクロールが少ないレイアウトにしたい。

**実装案:**

#### 2カラムレイアウト（PC）/ アコーディオン（モバイル）:

```
┌─────────────────────────┬─────────────────────┐
│ 基本情報（商品名・説明）  │  画像               │
│                         │  [画像リスト]        │
├─────────────────────────┤  [追加/削除/順番]    │
│ 価格・在庫              │                     │
├─────────────────────────┼─────────────────────┤
│ バリエーション設定       │  分類・SEO          │
└─────────────────────────┴─────────────────────┘
[保存ボタン（固定フッター）]
```

**具体的な変更内容:**

- 基本情報セクション：商品名（日本語/英語）・商品説明（日本語/英語）のみに絞る  
- 所属ショップ・ステータスはサイドバーまたはコンパクトな横並びに移動  
- SEOセクションをデフォルト折りたたみ（`<details>`）にして必要時のみ展開  
- バリエーションテーブルも折りたたみ（デフォルト展開）にして構造を維持  

---

### 2-2. 商品名・説明文に日本語/英語フィールドを追加

**現状:**  
- `custom_title`（カスタムタイトル）と `custom_description`（カスタム説明文）が1フィールドずつ存在。  
- 英語フィールドはモデルに存在しない。

**要望:**  
- 商品名と商品説明に日本語・英語の両方の入力エリアを作成  
- 日本語を修正すると自動翻訳 or 翻訳ボタンを押すと翻訳  
- 英語欄に手動入力も可能

**実装案:**

#### テンプレート変更（`templates/product_detail.html`）:

```html
<!-- 商品名エリア -->
<div class="form-group lang-pair">
    <div class="lang-field">
        <label for="title">
            商品名 🇯🇵
            <button type="button" class="translate-btn" 
                    onclick="translateField('title', 'title_en')">
                🔄 英語に翻訳
            </button>
        </label>
        <input type="text" id="title" name="title"
               value="{{ product.custom_title or product.last_title or '' }}"
               oninput="scheduleAutoTranslate('title', 'title_en')">
    </div>
    <div class="lang-field">
        <label for="title_en">
            商品名 🇺🇸
            <span class="auto-translate-indicator" id="title_en_indicator"></span>
        </label>
        <input type="text" id="title_en" name="title_en"
               value="{{ product.custom_title_en or '' }}"
               placeholder="英語名（手動入力 or 翻訳ボタンで自動入力）">
    </div>
</div>

<!-- 商品説明エリア -->
<div class="form-group lang-pair">
    <div class="lang-field">
        <label for="description">
            商品説明 🇯🇵
            <button type="button" class="translate-btn"
                    onclick="translateField('description', 'description_en')">
                🔄 英語に翻訳
            </button>
        </label>
        <textarea id="description" name="description" rows="6">
            {{- product.custom_description or (snapshot.description if snapshot else '') -}}
        </textarea>
    </div>
    <div class="lang-field">
        <label for="description_en">
            商品説明 🇺🇸
            <span class="auto-translate-indicator" id="description_en_indicator"></span>
        </label>
        <textarea id="description_en" name="description_en" rows="6"
                  placeholder="英語説明文（手動入力 or 翻訳ボタンで自動入力）">
            {{- product.custom_description_en or '' -}}
        </textarea>
    </div>
</div>
```

#### JavaScript（翻訳処理）:

```javascript
let translateTimer = null;

function scheduleAutoTranslate(sourceId, targetId) {
    // 入力停止から1.5秒後に自動翻訳（連続入力中はキャンセル）
    clearTimeout(translateTimer);
    const indicator = document.getElementById(targetId + '_indicator');
    if (indicator) indicator.textContent = '⏳ 待機中...';
    
    translateTimer = setTimeout(() => {
        translateField(sourceId, targetId);
    }, 1500);
}

async function translateField(sourceId, targetId) {
    const text = document.getElementById(sourceId).value.trim();
    if (!text) return;
    
    const indicator = document.getElementById(targetId + '_indicator');
    if (indicator) indicator.textContent = '🔄 翻訳中...';
    
    try {
        const res = await fetch('/api/translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, source: 'ja', target: 'en' })
        });
        if (res.ok) {
            const data = await res.json();
            document.getElementById(targetId).value = data.translated;
            if (indicator) indicator.textContent = '✅ 翻訳完了';
            setTimeout(() => { if (indicator) indicator.textContent = ''; }, 3000);
        }
    } catch (e) {
        if (indicator) indicator.textContent = '❌ 翻訳失敗';
    }
}
```

#### バックエンド（翻訳APIエンドポイント）:

```python
@products_bp.route('/api/translate', methods=['POST'])
@login_required
def translate_text():
    data = request.get_json()
    text = data.get('text', '')
    target = data.get('target', 'en')
    
    if not text:
        return jsonify({'error': 'No text provided'}), 400
    
    translated = translate_to_english(text)  # translation_service.py の関数
    return jsonify({'translated': translated})
```

---

### 2-3. 商品画像の削除・並べ替え・追加・白抜き機能

**現状:**  
- 商品画像は `ProductSnapshot.image_urls`（パイプ区切り文字列）として保存されている。  
- `product_detail.html` で画像を表示しているが、削除・並べ替え・追加は実装されていない。  
- 画像はURLとして保存されており（スクレイピング元のURL）、ローカルアップロード機能はない。

**要望:**  
1. 商品画像の削除  
2. 商品画像の画像順番入れ替え（ドラッグ）  
3. 商品画像の新規画像追加（アップロード）  
4. 商品画像の白抜き（背景除去）

#### 2-3-1. 画像削除・並べ替え・追加

**UIコンポーネント設計:**

```html
<div class="content-card">
    <div class="content-card-header">
        <h2>画像（<span id="imgCount">{{ images|length }}</span>枚）</h2>
        <label class="btn btn-primary" style="cursor:pointer;">
            ＋ 画像を追加
            <input type="file" name="new_images" multiple accept="image/*" 
                   style="display:none;" onchange="handleImageUpload(this)">
        </label>
    </div>
    <div class="content-card-body">
        <!-- ドラッグ可能な画像グリッド -->
        <div id="imageGrid" class="image-grid sortable">
            {% for img_url in images %}
            <div class="image-item" data-url="{{ img_url }}" draggable="true">
                <img src="{{ img_url }}" alt="商品画像" class="image-thumb">
                <div class="image-overlay">
                    <button type="button" class="img-btn img-delete"
                            onclick="removeImage(this)" title="削除">🗑️</button>
                    <button type="button" class="img-btn img-bg-remove"
                            onclick="removeBg(this)" title="背景除去">✨</button>
                </div>
                <div class="image-order-badge">{{ loop.index }}</div>
            </div>
            {% endfor %}
        </div>
        
        <!-- 変更された画像URLを送信するための隠しフィールド -->
        <input type="hidden" name="image_urls_json" id="imageUrlsJson">
    </div>
</div>
```

**SortableJS（ドラッグ&ドロップライブラリ）:**  
[SortableJS](https://sortablejs.github.io/Sortable/) は軽量で依存なし（CDN利用可能）。

```html
<!-- CDNから読み込み（base.html に追加） -->
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"></script>
```

```javascript
// ドラッグ&ドロップの初期化
const imageGrid = document.getElementById('imageGrid');
const sortable = new Sortable(imageGrid, {
    animation: 150,
    ghostClass: 'sortable-ghost',
    onEnd: function() {
        updateImageOrder();
    }
});

function updateImageOrder() {
    const items = imageGrid.querySelectorAll('.image-item');
    const urls = Array.from(items).map(item => item.dataset.url);
    document.getElementById('imageUrlsJson').value = JSON.stringify(urls);
    
    // 順番バッジを更新
    items.forEach((item, index) => {
        const badge = item.querySelector('.image-order-badge');
        if (badge) badge.textContent = index + 1;
    });
}

function removeImage(btn) {
    const item = btn.closest('.image-item');
    if (confirm('この画像を削除しますか？')) {
        item.remove();
        updateImageOrder();
    }
}
```

#### 2-3-2. 画像アップロード機能

**バックエンド設計:**

アップロード先は以下の2案を検討：

**A案（推奨）: Cloudinary（無料枠: 25GB, 25クレジット/月）**  
- 画像の保存・変換・配信をすべてCloudinaryで管理  
- 背景除去APIも提供（有料機能）  
- 環境変数: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

**B案: ローカルファイルシステム + サーバー配信**  
- `static/uploads/<user_id>/<product_id>/` に保存  
- 本番環境（Render.com）ではエフェメラルファイルシステムのためデプロイ毎にリセット → 向いていない  

**C案: AWS S3 または Google Cloud Storage**  
- 永続的なオブジェクトストレージ  
- コスト: 約 $0.023/GB/月

#### 2-3-3. 白抜き（背景除去）機能

**実装案:**

**A案: remove.bg API**  
- 専門の背景除去サービス  
- 無料枠: 50クレジット/月（1枚1クレジット）  
- 高品質な背景除去が可能

**B案: Cloudinary Background Removal**  
- Cloudinaryの有料アドオン  
- A案と同様の品質

**C案: rembg（Python ライブラリ、オープンソース）**  
- サーバーサイドで動作、APIコスト不要  
- PyTorch/ONNXに依存、サーバーのメモリ使用量が増える  
- 品質は商用サービスより若干劣る

**`services/image_service.py`（remove.bg API使用例）:**

```python
import requests
import os
import base64

def remove_background(image_url: str) -> str:
    """
    remove.bg APIを使って画像の背景を除去し、
    Base64エンコードされた結果画像を返す
    """
    api_key = os.getenv("REMOVE_BG_API_KEY")
    if not api_key:
        raise ValueError("REMOVE_BG_API_KEY is not set")
    
    response = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        data={
            "image_url": image_url,
            "size": "auto",
        },
        headers={"X-Api-Key": api_key},
        timeout=30
    )
    
    if response.status_code == 200:
        # PNG形式で背景除去済み画像が返ってくる
        return base64.b64encode(response.content).decode('utf-8')
    else:
        raise Exception(f"remove.bg API error: {response.status_code}")
```

---

## 3. 商品抽出ページ（旧：スクレイピングページ）

**対象URL:** `https://esp-1-kend.onrender.com/scrape/`  
**対象ファイル:** `templates/scrape_form.html`, `templates/scrape_result.html`, `routes/scrape.py`

---

### 3-1. 「スクレイピング」→「商品抽出」への文言変更

**現状:**  
- `scrape_form.html` のタイトル・ヘッダに「スクレイピング」という語が使用されている。  
- ボタン「スクレイピング実行」がある。

**要望:**  
「スクレイピング」という文言をすべて「商品抽出」に変更する。

**変更対象ファイルと変更箇所:**

| ファイル | 変更前 | 変更後 |
|---------|--------|--------|
| `templates/scrape_form.html` | `{% block title %}スクレイピング設定{% endblock %}` | `{% block title %}商品抽出{% endblock %}` |
| `templates/scrape_form.html` | `{% block header_title %}スクレイピング{% endblock %}` | `{% block header_title %}商品抽出{% endblock %}` |
| `templates/scrape_form.html` | `<h1>スクレイピング実行</h1>` | `<h1>商品抽出</h1>` |
| `templates/scrape_form.html` | 「方法1: URLからスクレイピング実行」ボタン | 「URLから商品を抽出」 |
| `templates/scrape_form.html` | 「スクレイピング実行」ボタン（方法2） | 「商品を抽出する」 |
| `templates/base.html` | ナビゲーションの「スクレイピング」リンクテキスト | 「商品抽出」 |
| `templates/scrape_result.html` | 結果ページ内の「スクレイピング」文言 | 「商品抽出」 |

> ⚠️ 注意: URLパス (`/scrape/`) とFlaskルート名（`scrape_run` など）は内部実装のため変更不要。UIテキストのみ変更する。

---

### 3-2. 実行中のローディング画面

**現状:**  
- フォームをサブミットするとページ遷移があり、抽出完了まで真っ白な画面またはブラウザのローディングスピナーのみ表示される。

**要望:**  
実行中はローディング画面を表示する。

**実装案:**

#### 方式A（推奨）: 非同期処理（AJAX + ポーリング）

フォームをAJAXで送信し、ジョブIDを受け取り、進捗をポーリングで確認する。

```javascript
document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', async function(e) {
        e.preventDefault();
        
        // ローディングオーバーレイを表示
        showLoadingOverlay('商品を抽出中です...');
        
        const formData = new FormData(this);
        
        try {
            const res = await fetch(this.action, {
                method: 'POST',
                body: formData,
            });
            
            if (res.redirected) {
                // 通常のリダイレクト（結果ページへ）
                window.location.href = res.url;
            } else {
                const data = await res.json();
                if (data.job_id) {
                    // ジョブIDがある場合はポーリング
                    pollJobStatus(data.job_id);
                }
            }
        } catch (e) {
            hideLoadingOverlay();
            alert('エラーが発生しました: ' + e.message);
        }
    });
});

function showLoadingOverlay(message) {
    const overlay = document.createElement('div');
    overlay.id = 'loadingOverlay';
    overlay.innerHTML = `
        <div class="loading-content">
            <div class="loading-spinner"></div>
            <p class="loading-message">${message}</p>
            <p class="loading-sub">しばらくお待ちください...</p>
        </div>
    `;
    overlay.style.cssText = `
        position: fixed; inset: 0; background: rgba(0,0,0,0.7);
        display: flex; align-items: center; justify-content: center;
        z-index: 9999;
    `;
    document.body.appendChild(overlay);
}

function hideLoadingOverlay() {
    const overlay = document.getElementById('loadingOverlay');
    if (overlay) overlay.remove();
}
```

**ローディングスピナーCSS:**

```css
.loading-content {
    background: white;
    padding: 40px;
    border-radius: 12px;
    text-align: center;
    min-width: 280px;
}

.loading-spinner {
    width: 48px;
    height: 48px;
    border: 4px solid #e5e7eb;
    border-top-color: #0066cc;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 16px;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}
```

#### 方式B（簡易）: フォームサブミット時にオーバーレイ表示

同期的なフォームサブミットのまま、サブミット直後にローディング画面を表示する。  
（抽出が完了するとページが遷移するためローディングが自動的に消える）

```javascript
document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', function() {
        showLoadingOverlay('商品を抽出中です...');
    });
});
```

**推奨は方式Bの簡易実装から始め、将来的に方式Aに移行する。**

---

### 3-3. 検索画面のコンパクト化

**現状:**  
- 方法1（URL直接指定）と方法2（検索条件）の2つの `content-card` が縦に並んでいる。  
- 方法2に7つのフォームフィールドがある。

**要望:**  
検索画面のレイアウトを画面上部に収まるぐらいコンパクトにまとめる。

**実装案:**

```
┌─────────────────────────────────────────────────────────────────┐
│  方法1: URLを貼り付けて抽出                                       │
│  [ https://... _____________________] [抽出する]                 │
├─────────────────────────────────────────────────────────────────┤
│  方法2: 条件で検索して抽出                                        │
│  [サイト▼] [キーワード___] [件数▼] [価格min] [-] [価格max] [抽出]│
│  ▼ 詳細オプション（ソート順・カテゴリID）                          │
└─────────────────────────────────────────────────────────────────┘
```

**変更のポイント:**
- カード2つを1つのカードにまとめる  
- 方法2の全フィールドを1行に横並びにする  
- ソート順・カテゴリIDを `<details>` 折りたたみに移動  
- スマートフォンでは縦積み、タブレット以上で横並び  

---

### 3-4 & 3-5. 抽出結果を同画面にサムネイル表示

**現状:**  
- スクレイピング結果は `scrape_result.html` という別ページに遷移して表示される。

**要望:**  
- 抽出結果を同じ画面内に表示する（ページ遷移なし）  
- 結果はサムネイル表示にする

**実装案:**

#### AJAX方式（推奨）:

フォームサブミットをAJAXで処理し、結果をJSON形式で受け取り、同ページに動的に描画する。

```python
# routes/scrape.py に追加（JSONレスポンス対応）
@scrape_bp.route('/scrape/run', methods=['POST'])
@login_required
def scrape_run():
    # ... 既存の抽出処理 ...
    
    # リクエストの Accept ヘッダーに応じてレスポンス形式を切り替え
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'ok': True,
            'products': [
                {
                    'id': p.id,         # 仮ID（未登録の場合はNone）
                    'title': p['title'],
                    'price': p['price'],
                    'status': p['status'],
                    'thumb_url': p['images'][0] if p['images'] else '',
                    'source_url': p['url'],
                    'site': site,
                }
                for p in scraped_products
            ]
        })
    else:
        # 従来の画面遷移（後方互換）
        return render_template('scrape_result.html', products=scraped_products)
```

**結果表示HTML（同ページに追加するセクション）:**

```html
<!-- 抽出結果エリア（初期は非表示） -->
<div id="scrapeResults" style="display:none;">
    <div class="content-card">
        <div class="content-card-header">
            <h3>抽出結果（<span id="resultCount">0</span>件）</h3>
            <div>
                <button type="button" onclick="selectAllResults(true)">すべて選択</button>
                <button type="button" onclick="selectAllResults(false)">すべて解除</button>
                <button type="button" class="btn btn-primary" onclick="registerSelected()">
                    ✅ 選択した商品を登録（<span id="checkedCount">0</span>件）
                </button>
            </div>
        </div>
        <div class="content-card-body">
            <!-- サムネイルグリッド -->
            <div id="resultGrid" class="scrape-result-grid"></div>
        </div>
    </div>
</div>
```

**商品カードテンプレート（JavaScript）:**

```javascript
function createProductCard(product) {
    return `
        <div class="scrape-product-card" data-product='${JSON.stringify(product)}'>
            <label class="scrape-card-label">
                <input type="checkbox" class="scrape-checkbox" 
                       onchange="updateCheckedCount()" checked>
                <div class="scrape-card-inner">
                    <div class="scrape-thumb-wrap">
                        ${product.thumb_url 
                            ? `<img src="${product.thumb_url}" alt="${product.title}" loading="lazy">`
                            : '<div class="scrape-thumb-placeholder">📦</div>'
                        }
                    </div>
                    <div class="scrape-card-info">
                        <div class="scrape-card-title">${escapeHtml(product.title)}</div>
                        <div class="scrape-card-price">¥${product.price?.toLocaleString() || '—'}</div>
                        <div class="scrape-card-status">${product.status || ''}</div>
                    </div>
                </div>
            </label>
        </div>
    `;
}
```

---

### 3-6. チェックボックスで選択した商品のみ登録可能

**要望:**  
抽出結果にチェックボックスをつけて、選択した商品のみ商品登録できるようにする。

**実装案:**

上記3-4&3-5の「抽出結果サムネイル表示」と合わせて実装する。

```javascript
async function registerSelected() {
    const checkedCards = document.querySelectorAll('.scrape-checkbox:checked');
    const selectedProducts = Array.from(checkedCards).map(cb => {
        return JSON.parse(cb.closest('[data-product]').dataset.product);
    });
    
    if (selectedProducts.length === 0) {
        alert('登録する商品を選択してください');
        return;
    }
    
    if (!confirm(`${selectedProducts.length}件の商品を登録しますか？`)) return;
    
    showLoadingOverlay(`${selectedProducts.length}件を登録中...`);
    
    try {
        const res = await fetch('/scrape/register-selected', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ products: selectedProducts })
        });
        const data = await res.json();
        hideLoadingOverlay();
        
        if (data.ok) {
            alert(`✅ ${data.registered}件の商品を登録しました`);
            // 登録済み商品カードにマークを付ける
            markRegistered(data.registered_ids);
        }
    } catch (e) {
        hideLoadingOverlay();
        alert('登録に失敗しました: ' + e.message);
    }
}
```

**バックエンド（新規エンドポイント）:**

```python
@scrape_bp.route('/scrape/register-selected', methods=['POST'])
@login_required
def register_selected_products():
    """選択された抽出済み商品をDBに登録する"""
    data = request.get_json()
    products = data.get('products', [])
    
    db = SessionLocal()
    registered = 0
    registered_ids = []
    
    try:
        for p_data in products:
            product = Product(
                user_id=current_user.id,
                site=p_data['site'],
                source_url=p_data['source_url'],
                last_title=p_data['title'],
                last_price=p_data.get('price'),
                last_status=p_data.get('status'),
                status='draft',
            )
            db.add(product)
            db.flush()
            
            snapshot = ProductSnapshot(
                product_id=product.id,
                title=p_data['title'],
                price=p_data.get('price'),
                status=p_data.get('status'),
                image_urls=p_data.get('thumb_url', ''),
            )
            db.add(snapshot)
            
            registered_ids.append(product.id)
            registered += 1
        
        db.commit()
        return jsonify({'ok': True, 'registered': registered, 'registered_ids': registered_ids})
    except Exception as e:
        db.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        db.close()
```

---

## 4. 価格表管理ページ

**対象URL:** `https://esp-1-kend.onrender.com/pricelists`  
**対象ファイル:** `templates/catalog.html`, `routes/catalog.py`, `routes/pricelist.py`

---

### 4-1. レイアウトの複数パターン選択

**現状:**  
- `catalog.html`（公開ページ）は固定の2カラム〜4カラムグリッドレイアウトのみ。  
- 管理者側でレイアウトを選ぶUIはない。

**要望:**  
レイアウトが何パターンか選べるといい。

**実装案:**

#### 価格表のレイアウトオプション（3パターン）:

| パターン | 説明 | 用途 |
|---------|------|------|
| `grid` | 画像大・タイトル短・グリッド表示（現在のデフォルト） | ECサイト風、視覚的訴求 |
| `list` | 横長リスト表示、画像小・情報多め | 比較しやすい価格表 |
| `table` | テーブル形式（商品名・価格・在庫を列で表示） | ビジネス向け、数量確認に最適 |

#### データベース変更:

```python
# models.py の PriceList モデルに追加
class PriceList(Base):
    # ... 既存フィールド ...
    layout = Column(String, default='grid')  # 'grid', 'list', 'table'
```

#### 管理画面の変更（`pricelist_edit.html`）:

```html
<div class="form-group">
    <label>カタログレイアウト</label>
    <div class="layout-picker">
        <label class="layout-option {{ 'selected' if pl.layout == 'grid' }}">
            <input type="radio" name="layout" value="grid" 
                   {% if pl.layout == 'grid' %}checked{% endif %}>
            <div class="layout-preview layout-grid-preview">
                <div></div><div></div><div></div><div></div>
            </div>
            <span>グリッド</span>
        </label>
        <label class="layout-option {{ 'selected' if pl.layout == 'list' }}">
            <input type="radio" name="layout" value="list"
                   {% if pl.layout == 'list' %}checked{% endif %}>
            <div class="layout-preview layout-list-preview">
                <div></div><div></div><div></div>
            </div>
            <span>リスト</span>
        </label>
        <label class="layout-option {{ 'selected' if pl.layout == 'table' }}">
            <input type="radio" name="layout" value="table"
                   {% if pl.layout == 'table' %}checked{% endif %}>
            <div class="layout-preview layout-table-preview">
                <div></div>
            </div>
            <span>テーブル</span>
        </label>
    </div>
</div>
```

#### `catalog.html` でのレイアウト切り替え:

```jinja2
{% if pricelist.layout == 'list' %}
    {% include 'catalog_layout_list.html' %}
{% elif pricelist.layout == 'table' %}
    {% include 'catalog_layout_table.html' %}
{% else %}
    <!-- デフォルト: グリッド（現在の実装） -->
    <div class="catalog-grid"> ... </div>
{% endif %}
```

---

### 4-2. 商品クリックで商品詳細表示（ECサイト的）

**現状:**  
- `catalog.html` の商品カードはクリックしても何も起きない（リンクなし）。

**要望:**  
商品をクリックすると商品詳細を見られる（決済のないECサイト的なイメージ）。

**実装案:**

#### モーダル方式（UX最優先・推奨）:

商品カードをクリックするとモーダルが展開し、詳細情報（全画像・詳細説明・価格）を表示する。ページ遷移なし。

```html
<!-- 商品カードにクリックイベントを追加 -->
<div class="product-card" onclick="openProductModal({{ item.id }})">
    ...
</div>

<!-- 商品詳細モーダル -->
<div id="productModal" class="product-modal" style="display:none;" onclick="closeModalOnBackdrop(event)">
    <div class="product-modal-content">
        <button class="modal-close" onclick="closeProductModal()">✕</button>
        
        <!-- 画像ギャラリー -->
        <div class="modal-gallery">
            <img id="modalMainImage" src="" alt="" class="modal-main-image">
            <div id="modalThumbnails" class="modal-thumbnails"></div>
        </div>
        
        <!-- 商品情報 -->
        <div class="modal-info">
            <h2 id="modalTitle" class="modal-product-title"></h2>
            <div id="modalPrice" class="modal-product-price"></div>
            <div id="modalStock" class="modal-stock-info"></div>
            <div id="modalDescription" class="modal-description"></div>
            
            <!-- 問い合わせボタン（決済なし） -->
            <button class="modal-inquiry-btn" onclick="inquireAboutProduct()">
                📧 この商品について問い合わせる
            </button>
        </div>
    </div>
</div>
```

#### 商品詳細APIエンドポイント:

```python
# routes/catalog.py に追加
@catalog_bp.route('/catalog/<token>/product/<int:product_id>')
def catalog_product_detail(token, product_id):
    """商品詳細をJSONで返す（カタログページのAJAX用）"""
    db = SessionLocal()
    try:
        pricelist = db.query(PriceList).filter_by(token=token, is_active=True).first()
        if not pricelist:
            return jsonify({'error': 'Not found'}), 404
        
        item = db.query(PriceListItem).filter_by(
            pricelist_id=pricelist.id,
            product_id=product_id
        ).first()
        if not item:
            return jsonify({'error': 'Not found'}), 404
        
        product = item.product
        snapshot = product.snapshots[0] if product.snapshots else None
        
        images = []
        if snapshot and snapshot.image_urls:
            images = snapshot.image_urls.split('|')
        
        return jsonify({
            'id': product.id,
            'title': product.custom_title or product.last_title,
            'title_en': product.custom_title_en or '',
            'price': item.custom_price or product.selling_price or product.last_price,
            'description': product.custom_description or (snapshot.description if snapshot else ''),
            'images': images,
            'in_stock': product.last_status not in ['sold', '売り切れ', '販売終了'],
        })
    finally:
        db.close()
```

---

### 4-3. ページの簡易アクセス解析機能

**現状:**  
- カタログページのアクセス数を記録する機能はない。

**要望:**  
ページの簡易アクセス解析機能を追加する。

**実装案:**

#### アクセスログモデル（`models.py` に追加）:

```python
class CatalogPageView(Base):
    __tablename__ = "catalog_page_views"
    
    id = Column(Integer, primary_key=True)
    pricelist_id = Column(Integer, ForeignKey("pricelists.id"), nullable=False)
    viewed_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # アクセス情報
    ip_hash = Column(String)              # IPアドレスのハッシュ（プライバシー配慮）
    user_agent_short = Column(String)     # デバイス種別（Mobile/Desktop）
    referrer_domain = Column(String)      # 参照元ドメイン
    product_id = Column(Integer, nullable=True)  # 商品詳細を見た場合のproduct_id
    country = Column(String)             # GeoIPによる国コード（任意）
```

#### アクセス記録（`routes/catalog.py`）:

```python
def record_page_view(pricelist_id, request_obj, product_id=None):
    """プライバシーに配慮したページビューを記録する"""
    import hashlib
    from urllib.parse import urlparse
    
    ip = request_obj.remote_addr or ''
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    
    ua = request_obj.user_agent.string
    device = 'Mobile' if 'Mobile' in ua or 'Android' in ua else 'Desktop'
    
    referrer = request_obj.referrer or ''
    ref_domain = urlparse(referrer).netloc if referrer else 'direct'
    
    db = SessionLocal()
    try:
        view = CatalogPageView(
            pricelist_id=pricelist_id,
            ip_hash=ip_hash,
            user_agent_short=device,
            referrer_domain=ref_domain,
            product_id=product_id,
        )
        db.add(view)
        db.commit()
    except Exception:
        pass  # アクセス記録の失敗でページ表示を妨げない
    finally:
        db.close()
```

#### アクセス解析ダッシュボード（`pricelist_analytics.html`）:

管理画面の価格表管理ページに「📊 アクセス解析」ボタンを追加し、以下の情報を表示する：

| 指標 | 表示内容 |
|------|---------|
| 総ページビュー | 全期間・直近7日・直近30日 |
| ユニークビジター | IPハッシュによる推定ユニーク数 |
| デバイス比率 | モバイル vs デスクトップ（円グラフ） |
| 参照元 | direct / SNS / 検索 / その他（棒グラフ） |
| 時系列グラフ | 日別/週別ページビュー推移（折れ線グラフ） |
| 人気商品 | クリックされた商品TOP5 |

**グラフライブラリ:**  
[Chart.js](https://www.chartjs.org/)（MIT License, CDN利用可能）を推奨。

---

## まとめ・実装優先度

| # | 機能 | 優先度 | 実装難易度 | 外部依存 |
|---|-----|--------|-----------|---------|
| 1-1 | 検索項目コンパクト化 | 🔴 高 | 低 | なし |
| 1-2 | eBay項目削除 | 🔴 高 | 低 | なし |
| 1-3 | 「サイト」列削除 | 🔴 高 | 低 | なし |
| 1-4 | 「画像枚数」列削除 | 🔴 高 | 低 | なし |
| 1-5 | 「ステータス」→「在庫」 | 🔴 高 | 低 | なし |
| 1-6 | 「元URL」→「抽出サイト」 | 🔴 高 | 低 | なし |
| 1-7 | 価格列二段表示 | 🔴 高 | 低 | なし |
| 1-8 | 商品名二段表示（英語翻訳） | 🟡 中 | 中 | 翻訳API |
| 1-9 | インライン価格・英語名編集 | 🟡 中 | 中 | なし |
| 1-10 | 一括価格設定 | 🟡 中 | 中 | なし |
| 1-11 | 商品手動追加 | 🟡 中 | 中 | なし |
| 2-1 | 商品編集コンパクト化 | 🟡 中 | 中 | なし |
| 2-2 | 日本語/英語フィールド追加 | 🟡 中 | 中 | 翻訳API |
| 2-3a | 画像削除・並べ替え | 🔴 高 | 中 | SortableJS |
| 2-3b | 画像アップロード | 🟡 中 | 高 | Cloudinary等 |
| 2-3c | 画像白抜き | 🟢 低 | 高 | remove.bg等 |
| 3-1 | 「商品抽出」文言変更 | 🔴 高 | 低 | なし |
| 3-2 | ローディング画面 | 🔴 高 | 低 | なし |
| 3-3 | 検索画面コンパクト化 | 🔴 高 | 低 | なし |
| 3-4,5 | 同画面サムネイル結果表示 | 🟡 中 | 中 | なし |
| 3-6 | 選択商品のみ登録 | 🟡 中 | 中 | なし |
| 4-1 | カタログレイアウト切替 | 🟢 低 | 中 | なし |
| 4-2 | 商品詳細モーダル | 🟡 中 | 中 | なし |
| 4-3 | アクセス解析 | 🟢 低 | 中 | Chart.js |

---

## 推奨実装順序

### Phase 1（即時着手・UI改善）
1. eBay関連項目削除
2. 「サイト」「画像枚数」列削除
3. 「ステータス」→「在庫」文言・表示変更
4. 「元URL」→「抽出サイト」リンク変更
5. 価格列二段表示（CSS変更で対応可能）
6. 「スクレイピング」→「商品抽出」全文言変更
7. ローディング画面追加（JS数行で対応可能）
8. 検索画面コンパクト化

### Phase 2（バックエンド・機能追加）
9. 商品名英語フィールド追加（DBマイグレーション + 翻訳API設定）
10. インライン編集機能（価格・英語名）
11. 一括価格設定機能
12. 抽出結果の同画面表示 + チェックボックス登録
13. 画像削除・並べ替え・アップロード機能

### Phase 3（高度な機能）
14. 商品手動追加機能
15. カタログレイアウト切替
16. 商品詳細モーダル
17. アクセス解析機能
18. 画像白抜き（背景除去）機能
