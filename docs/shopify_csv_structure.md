# Shopify CSV 出力仕様書 (バリエーション対応版)

## 概要
本プロジェクトで出力するCSVは、Shopifyのインポート仕様に準拠する。
特に「単一商品」と「バリエーション商品」で行の構成が異なる点に注意が必要である。

## 1. カラム構成 (必須項目抜粋)

| 項目名 | Shopify CSV Header | 説明 | Product/Variant |
| :--- | :--- | :--- | :--- |
| ハンドル | `Handle` | 商品を識別するID。全行で必須。 | Common |
| 商品名 | `Title` | 商品名。**親行のみ**記入。 | Product |
| 説明文 | `Body (HTML)` | **親行のみ**記入。 | Product |
| ベンダー | `Vendor` | **親行のみ**記入。 | Product |
| オプション1名 | `Option1 Name` | 例: "Color", "Size"。単一時は "Title" | Product |
| オプション1値 | `Option1 Value` | 例: "Red", "Large"。単一時は "Default Title" | Variant |
| オプション2名 | `Option2 Name` | (任意) | Product |
| オプション2値 | `Option2 Value` | (任意) | Variant |
| 価格 | `Variant Price` | バリエーションごとの価格。 | Variant |
| SKU | `Variant SKU` | バリエーションごとのSKU。 | Variant |
| 在庫数 | `Variant Inventory Qty` | 在庫数。 | Variant |
| 重量 | `Variant Grams` | 重量。 | Variant |
| 画像SRC | `Image Src` | 画像URL。 | Image |
| 画像位置 | `Image Position` | 画像の表示順。 | Image |

---

## 2. 出力パターン

### パターンA: 単一商品 (現在)
バリエーションを持たない商品。Shopifyの仕様上、内部的には「Default Title」という1つのバリエーションを持つ扱いとなる。

| 行 | Handle | Title | Option1 Name | Option1 Value | Variant Price | Variant SKU | Image Src |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `pen-001` | 高級万年筆 | `Title` | `Default Title` | 10000 | `MER-12345` | `https://.../1.jpg` |
| 2 | `pen-001` | (空) | (空) | (空) | (空) | (空) | `https://.../2.jpg` |

※ 2行目以降は画像のみの行。

### パターンB: 複数バリエーション (新規実装)
例: 色（Red, Blue）とサイズ（S, M）がある場合。
**1行目が「親商品情報 兼 バリエーション1」**となり、2行目以降はバリエーション情報のみを記述する。

| 行 | Handle | Title | Option1 Name | Option1 Value | Option2 Name | Option2 Value | Variant Price | Variant SKU | Image Src |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `t-shirt` | ロゴTシャツ | `Color` | `Red` | `Size` | `S` | 3000 | `TS-R-S` | `.../red.jpg` |
| 2 | `t-shirt` | (空) | (空) | `Red` | (空) | `M` | 3000 | `TS-R-M` | (空) |
| 3 | `t-shirt` | (空) | (空) | `Blue` | (空) | `S` | 3000 | `TS-B-S` | `.../blue.jpg` |
| 4 | `t-shirt` | (空) | (空) | `Blue` | (空) | `M` | 3000 | `TS-B-M` | (空) |
| 5 | `t-shirt` | (空) | (空) | (空) | (空) | (空) | (空) | (空) | `.../detail.jpg` |

#### 注意点
1. **Option Name**: 1行目に必ず記入する（`Option1 Name`="Color", `Option2 Name`="Size"）。2行目以降は空欄でもShopifyは理解するが、埋めても良い。
2. **Option Value**: 該当するバリエーションの行には必ず記入する。
3. **画像**: バリエーションに紐付かない追加画像（例: 詳細カット）は、バリエーション情報が空の行として追加する（行5）。

---

## 3. アプリケーション側の対応

CSV生成ロジック (`export_shopify`) を以下のように変更する。

1. **ループ単位の変更**:
   - 現状: `for product in products:` (1商品1行)
   - 変更後: `for product in products:` の中で、さらに `for variant in product.variants:` を回す。
2. **1行目の判定**:
   - `loop.first` の場合のみ `Title`, `Body`, `Vendor`, `Tags` などを出力する。
   - 2行目以降はこれらのフィールドを空文字にする。
