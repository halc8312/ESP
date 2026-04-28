# モックを採用する場合の "絶対に残すべき" 機能 (現状からの持ち越し)

モックを正式デザイン案として採用する前提で、現状から **残さないと業務/データが壊れる** か、**残した方が明確に運用が楽になる** 機能のみを挙げます。

## A. 残さないとデータ/契約が壊れる (Must keep, 非交渉)

| # | 機能 | 理由 / 残す形 |
|---|---|---|
| A1 | **マルチバリアント table** (Default / Variant B …、SKU / 価格 / 在庫 / 重量 / 税 / 仕入価) | 既存ユーザーの DB と eBay/Shopify CSV エクスポート (`routes/export.py`) がこの構造に依存。モックは単一バリアント前提なので、**「変種が1件のときだけ section 直下に 販売価格 + 在庫数 を表示し、2件以上のときは展開可能な詳細 table に切替」というハイブリッド** にする。AGENTS.md の "user/shop/pricelist isolation" にも直結。 |
| A2 | **CSRF / Flask-WTF / 既存 form name (`title` `description` `tags` `manual_margin_rate` `manual_shipping_cost` `vendor` `handle` `seo_title` `seo_description` `published` `shop_id` `pricing_rule_id` `last_price` etc.)** | サーバー側 `routes/products.py` を変更しないため。モックの input id (`#sales_price` `#manual_profit` 等) は装飾扱いとし、`name=` 属性は現状を維持。 |
| A3 | **ショップ選択 (`shop_id` select)** | 1ユーザーが複数 shop 持てる前提で動いている。モックには無いが**右サイドカラム「商品ステータス」カードの直上に配置**して残す。 |
| A4 | **PricingRule 選択 (`pricing_rule_id` select)** | 価格再計算の基本ルール。モックの「販売設定」セクション内の `個別計算パラメータ` トグル**手前**に「適用ルール: ○○」select を置く。manual override が無い場合の自動価格はこのルールから決まるため、隠せない。 |
| A5 | **`source_url` を公開カタログに出さない既存の隔離** | AGENTS.md `Never expose source_url ... in public catalog`。新しい "仕入設定" カードは商品編集画面 (オーナー専用) の中だけに表示。 |
| A6 | **画像 URL + ファイルアップロード 両対応** | 現状の `addImageUrl()` / `image_files` 入力。スクレイピング由来の URL 画像と直アップロード画像を区別して扱うため、両方の入口を残す。モックの `drop-zone` は**ファイルアップロード相当**として再利用、URL 追加は `URLから追加` ヘッダーボタンの toggle で別 input を出す。 |
| A7 | **タグカンマ区切り保存 (`product.tags`)** | DB スキーマ。pill UI の hidden input ミラー (PR #96 のまま) は維持。 |

## B. UX 上 残した方が明確に良い (Should keep)

| # | 機能 | 残す形 |
|---|---|---|
| B1 | **画像「白抜き」の per-image preview / 反映 / 破棄 workflow** (`data-bg-status` / `data-bg-actions` / `data-bg-apply-bulk-btn`) | bg-removal はコストの高い API 呼び出しのため、結果を **一度プレビューして個別に反映/破棄を選べる現状機能** はモックの「まとめて白抜き」だけより明らかに優れている。実装そのものは残し、UI を **画像カードに重ねる小さい "プレビュー有り" バッジ + ホバーで反映/破棄ボタン** に圧縮。`まとめて白抜き` `まとめて反映` `URLから追加` の3ボタンはヘッダーに残す。 |
| B2 | **保存前チェック (5項目)** を **縮小して残す** | 画面右上の保存ボタン横に **`要 X件 / OK ○件` の小バッジ** にし、クリックで詳細ポップアップ。サイドカラムの専用カードは廃止 (モックに無いため)。完全廃止より残した方が「タグ未入力で公開」のような事故を防げる。 |
| B3 | **テンプレート適用 dropdown + 適用ボタン** | モックにもあるためそのまま採用。 |
| B4 | **タイトル翻訳 / 全文翻訳 / 自動翻訳 ボタン** | モック準拠で **各 EN フィールドのラベル横にインライン化**。現状の「英語欄へ移動」コピーは廃止 (並列レイアウトで不要)。 |
| B5 | **画像の並び順手動指定 (drag handle)** | Sortable.js (モック) に切替えるが、モバイル向け **`≡ 並べ替え` ボタンは残す** (タッチ操作で drag が暴発するため)。 |
| B6 | **`custom_handle` の手動上書き** | モックは `readonly` で英語名同期だが、SEO 担当者が手で書きたいユースケースを潰さないために **「英語名と同期」 toggle で readonly/編集可能 を切替** できるようにする。同期 ON 時は `title_en → handle` を JS で更新、OFF 時はユーザー入力を尊重。 |
| B7 | **ブランド名・販売元 (`vendor`)** | eBay CSV `Brand` 列で使われる。右サイド「分類」カード内、カテゴリーの下に小さく残す。 |
| B8 | **`status` (下書き / 公開中 / アーカイブ)** | モックにもあるため、右サイド「商品ステータス」カードに移動。現状の `published` チェックボックスは status select 化する (モック準拠)。マイグレ要否は要確認 (今は `is_published` boolean のはず)。 |
| B9 | **Internal source 情報 (`site` `source_url` `last_title` `last_price` `last_status`)** | モックの「仕入設定 (自動取得)」読み取り専用カードに集約し、現状の「内部仕入れメモ」アコーディオンは廃止。 |
| B10 | **スマホ向け sticky 保存ボタン (`.sticky-save-container`)** | 現状すでにある。モックの `lg:hidden` sticky-save と一致。維持。 |

## C. 廃止 / 統合してよい (Drop)

| # | 既存 | 理由 |
|---|---|---|
| C1 | `<details>` accordion 全廃 | モックはフラットなカード型で常時開き。 |
| C2 | 商品ヒーロー (Beginner-Friendly Flow) | モックには無し。ヘッダ + 仕入設定カードで情報量は足りる。 |
| C3 | 「英語欄へ移動」 button と促しコピー | EN フィールドが JP の隣にインライン配置されるため不要。 |
| C4 | 「内部仕入れメモ」アコーディオン | B9 の「仕入設定」カードに集約。 |
| C5 | 大きい `1枚目` `2枚目` ラベル / URL文字列 / 並べ替えボタン (PC 表示) | モック準拠で円形 hover アクションに圧縮 (B5 で言及した `≡ 並べ替え` はモバイル時のみ)。 |

## D. 新規追加が要るもの (モック由来、現状に無い)

| # | 項目 | 影響 |
|---|---|---|
| D1 | **`Product.category` カラム新規追加** + マスタ選択肢 (まずは固定リストでよい) | DB マイグレ (`ADDITIVE_STARTUP_MIGRATIONS`)、`models.Product` 拡張、`routes/products.py` 受け取り。**公開カタログ側の category フィルタとセットになるため別 PR でも可**。 |
| D2 | **英語名 → URLハンドル/ページタイトル 自動同期 JS** | 既存 JS に `title_en` の input listener を追加。 |
| D3 | **ヘッダー右の `保存する` ボタン (sticky)** + 戻るアイコン | 既存の base layout に header slot があるか確認必要。 |
| D4 | **販売価格を section 直下に出すフォールバック表示** | A1 と連動。`variants|length == 1` のときだけ表示。 |
| D5 | **画像 grid 10 列モード** (`grid-cols-5 md:grid-cols-10`) | CSS 全置換。並べ替え導線のスタイルも変わる。 |

---

## 提案: 段階的 PR 分割

破壊的変更が多いため、**5本の PR に分けて段階リリース**を推奨します。各段で本番ロールバック可能 / `pytest tests/test_e2e_routes.py -q` を毎回 green で通す。

| PR | 内容 | リスク |
|---|---|---|
| 1 | **基盤** — フォーム input の `name=` 互換を保ったまま `<details>` を `<section>` に置換、ヒーローと内部仕入れメモを廃止し**仕入設定カード**新設。保存ボタンをヘッダー右にも追加 (sticky)。**機能は変えない**。 | 低 (見た目のみ) |
| 2 | **画像セクション** — 10列グリッド + hover円形アクション + Sortable.js 導入 + ライトボックス維持 + bg-removal preview workflow を新 UI に移植。 | 中 (画像周りの JS 全面書換) |
| 3 | **販売設定の独立化** — variant table を「詳細バリエーション設定」 disclosure 内に移動、section 直下に **単変種フォールバック (`v_price_*` `v_qty_*` 1件のみ表示)** を新設。manual override をその下に再配置。pricing_rule_id select も同 section 内へ。 | 高 (CSV エクスポート互換テスト要) |
| 4 | **SEO 独立化 + 英語タイトル同期** — SEO カード分離、`title_en → handle/seo_title` 自動同期 JS 追加 (`英語名と同期` toggle で OFF 可)。 | 低 |
| 5 | **右サイドカラム + カテゴリー** — ステータス/カテゴリー/タグ pill を右カラムへ。`Product.category` カラム追加 + マイグレ + 公開カタログ側の category フィルタ。`is_published` → `status` への移行が要るならここで。 | 高 (DB マイグレ + 公開カタログとの連動) |

---

## 確認したいこと

1. 上記 A/B 群 (残す) で **絶対外せない** ものはこの粒度で OK ですか？追加で残したい既存機能はありますか？
2. C 群 (廃止) のうち反対のものはありますか？特に「保存前チェック」を完全廃止したい/残したい、のどちらでしょうか？
3. PR 分割は提案の **5本** で進めて良いですか？それとも 1 本にまとめますか？(分割推奨です)
4. どの PR から着手しますか？順番通り (PR1 から) で良ければそのまま PR1 のブランチを切って実装に入ります。
