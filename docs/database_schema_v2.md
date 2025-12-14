# データベース設計書 (v2: バリエーション対応)

## 変更概要
Shopifyの「Product (親)」と「Variant (子)」の関係をデータベース上で表現するため、既存の `Product` テーブルを分割・拡張する。

---

## 1. テーブル定義

### Products テーブル (親)
商品の基本情報（全バリエーション共通の情報）を管理。

| カラム名 | 型 | 説明 | 変更点 |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK | |
| `shop_id` | Integer | FK | 複数ショップ対応用 |
| `site` | String | 仕入れ元サイト (mercari) | |
| `source_url` | String | 仕入れ元URL | |
| `title` | String | 商品名 (custom_title優先) | 旧 `last_title` / `custom_title` を統合検討だが、一旦既存維持 |
| `description` | Text | 商品説明 | |
| `vendor` | String | 販売元 | |
| `product_type` | String | 商品タイプ | |
| `tags` | String | タグ (カンマ区切り) | |
| `status` | String | active / draft / archived | |
| `option1_name` | String | オプション1名 (例: Color) | **新規** |
| `option2_name` | String | オプション2名 (例: Size) | **新規** |
| `option3_name` | String | オプション3名 | **新規** |
| `created_at` | DateTime | | |
| `updated_at` | DateTime | | |

※ 以下のカラムは `Variants` テーブルへ移動するため、**廃止(または非推奨)** とする。
- `price` (last_price, custom_price)
- `sku`
- `grams`
- `inventory_qty`
- `taxable`
- `hs_code`
- `country_of_origin`

### Variants テーブル (子)
商品のバリエーション情報を管理。`Product` と 1:N の関係。

| カラム名 | 型 | 説明 | 備考 |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK | |
| `product_id` | Integer | FK | Products.id への参照 |
| `option1_value` | String | オプション1の値 (例: Red) | |
| `option2_value` | String | オプション2の値 (例: L) | |
| `option3_value` | String | オプション3の値 | |
| `sku` | String | SKU | |
| `price` | Integer | 販売価格 | |
| `inventory_qty` | Integer | 在庫数 | |
| `grams` | Integer | 重量 (g) | |
| `taxable` | Boolean | 課税対象か | |
| `hs_code` | String | HSコード | |
| `country_of_origin` | String | 原産国 | |
| `position` | Integer | 表示順 | |

---

## 2. 移行計画 (Migration Strategy)

現在のデータは「単一バリエーション」として登録されている。これを新スキーマに適合させる必要がある。

### ステップ
1. **DBリセット**: 開発段階であり、既存データは消去可能との許可があるため、マイグレーションではなく**DBファイルの再作成**を行う。
2. **モデル再定義**: `app.py` の `Product` クラスを修正し、`Variant` クラスを追加する。
3. **ロジック修正**:
    - **スクレイピング**: スクレイピング時はまだバリエーション情報がない（メルカリは1ページ1商品）ため、**「Default Title」という値を持つVariantを1つ作成**して保存するロジックにする。
    - **商品編集画面**: 親情報の編集フォームに加え、Variantのリストを表示・編集できるUIを追加する。
    - **CSVエクスポート**: 親とVariantを結合して出力する。

## 3. SQLModel / SQLAlchemy 定義案

```python
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    # ... (共通項目)
    
    # Options
    option1_name = Column(String, default="Title")
    option2_name = Column(String)
    option3_name = Column(String)

    variants = relationship("Variant", back_populates="product", cascade="all, delete-orphan")

class Variant(Base):
    __tablename__ = "variants"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    
    option1_value = Column(String, default="Default Title")
    option2_value = Column(String)
    option3_value = Column(String)
    
    price = Column(Integer)
    sku = Column(String)
    # ...
    
    product = relationship("Product", back_populates="variants")
```
