# UI/UX 横断分析レポート（2026-08-13）

> **修正状況（2026-08-13 更新）**
>
> **全26件を対応済み**です。回帰テストは `tests/test_ui_regressions.py`（57件）にあり、
> いずれも修正前のコードに対して失敗することを確認しています。
>
> **P3-24**（公開カタログ）は、価格フィルタを表示通貨に追従させる方針を採りました。
> 入力値は選択中の通貨として解釈し、内部で円に換算して比較します。通貨を切り替えても
> 入力済みの数値は書き換えず、見出しの単位表示（`Price (USD)`）だけが変わります。
> あわせて通貨選択の `localStorage` 保存、レート表示の書式統一、英訳が無い商品名への
> `lang="ja"` 付与を行いました。
>
> **P3-26**（デザイントークン）は、**セマンティック色のみ**を対象にしました。
> 危険・成功・警告の64箇所を「淡い面 / 枠 / 文字」の3点セットのトークンに統一しています
> （同じ「危険」に4種類の赤が使われていたため、見た目も揃う方向に変わります）。
> 装飾用の残り164箇所は、統一しても得るものが少なく差分だけが膨らむため据え置き、
> 代わりに**新規のハードコード色が増えたらテストが落ちる**上限チェックを入れました。
> ファイル別の上限は下げる方向にのみ更新できます。
>
> **P0-3 の訂正**: 当初「利益上乗せ率の欄が完全に死んでいる」と報告しましたが、
> これは誤りでした。連動処理は `bindMarkupPair()`（`index.html`）が
> `addEventListener` で実装しており、**入力の同期自体は動作していました**。
> 実害は、未定義の `oninput` 属性がキー入力のたびに `ReferenceError` を投げていた点
> （コンソールのノイズ）に留まります。補足文「価格倍率と連動します」も正確でした。
> 現在は死んだ `oninput` 属性を削除し、`addEventListener` 経由に一本化しています。

## 対象と手法

- **対象**: `templates/`（30ファイル）, `static/css/`（style.css / catalog.css）, `static/js/`（5ファイル）, および UI に接続する `routes/`
- **手法**: コードベースの静的解析。テンプレート横断のパターン照合（重複ID・未定義ハンドラ・ラベル関連付け・フォーム構造）をスクリプトで機械的に検出し、各件をソースで裏取り
- **前回監査**（`UI_UX_AUDIT.md`, 2026-01-24）の指摘事項—`aria-current`、スキップリンク、ショップ切替の「適用」ボタン—は**対応済み**であることを確認。本レポートはその後に残る、より具体的な不具合を扱う

検出した項目は **「壊れている（P0）」→「レイアウト/レスポンシブ（P1）」→「アクセシビリティ（P2）」→「一貫性・微調整（P3）」** の順に並べています。

---

## P0: 機能が壊れている

### 1. ログイン画面のパスワード「表示」ボタンが完全に無反応

`templates/login.html:34` に `data-toggle-password="#password"` のトグルボタンがあります。この属性を処理するのは `static/js/app_ui.js` の `bindPasswordToggles()` のみです。

しかし `login.html` は **`base.html` を継承しておらず、`<script>` タグが1つもありません**（確認済み: `grep -c '<script' templates/login.html` → `0`）。`app_ui.js` は読み込まれないため、ボタンは押しても何も起こりません。

```
templates/login.html:34   <button ... data-toggle-password="#password" ...>表示</button>
static/js/app_ui.js:530   function bindPasswordToggles() { ... }   ← 読み込まれない
```

**修正案**: `login.html` の `</body>` 直前に `<script src="{{ url_for('static', filename='js/app_ui.js') }}"></script>` を追加する。ダイアログ等を使わないなら、10行程度のインラインスクリプトでも可。

---

### 2. 商品一覧のエクスポート設定で、PC の入力値が黙って捨てられている

`templates/index.html` の 155〜429行が**単一の `<form method="GET">`** で、その中にモバイル用（157行〜）と PC 用（191行〜）のエクスポート設定が**両方**入っています。切り替えは CSS のみです。

```css
/* static/css/style.css:2797 */
.mobile-only  { display: none; }
.desktop-only { display: block; }
```

`display: none` の入力欄は**送信されます**（送信対象から外れるのは `disabled` のみ）。したがって、どの画面幅でもクエリは常にこうなります:

```
?markup=<モバイル値>&markup=<PC値>&qty=<モバイル値>&qty=<PC値>
```

受け取り側は先頭の値だけを見ます:

```python
# routes/export.py:62,65
markup = request.args.get("markup", type=float) or 1.0
qty    = request.args.get("qty", type=int) or 1
```

DOM 上でモバイル用ブロックが先にあるため、**PC で「価格倍率」「一括設定する在庫数」を変更しても、常に隠れたモバイル側の初期値が使われます**。ユーザーには成功したように見えて、出力 CSV の価格・在庫だけが意図と違う—発見の難しい種類の不具合です。

**修正案**: 設定ブロックを1つに統合し、レイアウトのみ CSS で出し分ける（前回監査の「フィルタUIの include 化」と同じ方針）。暫定対応なら、送信時に非表示側へ `disabled` を付ける。

---

### 3. `oninput` ハンドラ4つが未定義（訂正あり — 冒頭の注記を参照）

> **訂正**: 見出しで「完全に死んでいる」としていましたが、連動処理は
> `bindMarkupPair()` が `addEventListener` で実装しており動作していました。
> 実害は下記のうち「キー入力のたびに `ReferenceError`」の一点です。
> 「価格倍率と連動します」の補足文は正しく、`name` が無いのも設計どおりでした
> （`profit_margin` は `markup` を書き換えるための入力欄で、送信対象ではない）。

全テンプレートのインラインイベント属性を機械的に走査したところ、未定義の関数呼び出しは `index.html` の4件のみでした:

| 呼び出し箇所 | 関数名 |
|---|---|
| `index.html:167` | `updateProfitMarginMobile()` |
| `index.html:173` | `updateMarkupMobile()` |
| `index.html:197` | `updateProfitMargin()` |
| `index.html:203` | `updateMarkup()` |

いずれも `templates/` `static/js/` のどこにも定義がありません。結果として:

- 「利益上乗せ率 (%)」欄はキー入力のたび `ReferenceError` を投げるだけで、**`name` 属性も無いため送信もされません**。完全な飾りです
- 補足文の **「価格倍率と連動します」（`index.html:204`）は事実に反します**
- 「価格倍率」欄も入力のたびに例外を投げます（値自体は送信されます）

**修正案**: 倍率 ⇄ 利益率の相互変換（`markup = 1 + margin/100`）を実装するか、実装しないなら入力欄と誤解を招く補足文ごと削除する。

---

### 4. TinyMCE の初期化が無防備で、CDN 障害時にページの JS が全滅する

`tinymce.init()` が **`DOMContentLoaded` ハンドラの最初の文**として、ガード無しで呼ばれています。

```js
// templates/product_detail.html:2025-2026
document.addEventListener('DOMContentLoaded', function () {
    tinymce.init({ ... });        // ← CDN 失敗時 ReferenceError
    imageGrid = document.getElementById('imageSortGrid');   // 以降すべて未実行
    ...
    setupTagPillField(); setupImageLightbox(); ...
});
```

TinyMCE は `https://cdnjs.cloudflare.com/...` から読み込まれます（`product_detail.html:1995`）。広告ブロッカー・社内プロキシ・CDN 障害などで失敗すると例外でハンドラが中断し、**画像グリッド・タグ入力・ライトボックス・変更検知・保存状態バッジ・並べ替えがすべて初期化されません**。アプリで最も複雑な編集画面が、無言で操作不能になります。

同じファイルの `2085行` と `2728行` では `typeof tinymce !== 'undefined'` でガードしているので、**ガードが必要なことは認識されていて、init 側だけ漏れている**状態です。

同じ問題: `pricelist_edit.html:250`, `manage_templates.html:84`。

**修正案**: init 呼び出しを `typeof tinymce !== 'undefined'` で囲み、失敗時は素の `<textarea>` として使える旨をトーストで伝える。

---

### 5. 一括操作ダイアログで Enter を押すとページが飛ぶ

`index.html:755` で作られる一括操作ダイアログの中身は `<form class="batch-modal-form">` ですが、**`submit` ハンドラがありません**。

`app_ui.js` の `promptDialog()` は同じ構造で、きちんと対策しています:

```js
// static/js/app_ui.js:426
wrapper.addEventListener("submit", function (event) {
    event.preventDefault();      // ← batch-modal-form にはこれが無い
    closeActiveDialog("confirm");
});
```

「タイトルの先頭に文字を追加」「末尾に追加」は入力欄が1つだけなので、HTML の暗黙的送信が発動します。**文字を打って Enter を押すと、フォームが GET でカレント URL に送信され、ページが再読み込みされて一括操作が消えます**。日本語入力の確定 Enter でも起きうるため、実際の遭遇率は高めです。

**修正案**: `promptDialog` と同じ `submit` ハンドラを付け、確定操作に繋ぐ。

---

### 6. 価格ルール名にアポストロフィが入ると「編集」ボタンが壊れる

```html
<!-- templates/pricing.html:81 -->
<button onclick="editRule({{ rule.id }}, '{{ rule.name }}', ...)">編集</button>
```

Jinja のオートエスケープは `'` を `&#39;` に変換しますが、**HTML パーサが属性値をデコードしてから JS が評価される**ため、`'` として復活します。ルール名が `太郎's ルール` だと生成される JS は:

```js
editRule(1, '太郎's ルール', 30, 0, 0)   // SyntaxError
```

となり、**そのルールの編集ボタンだけが無反応**になります。原因表示は無し。同じ経路で `', alert(1), '` のような値を入れれば任意 JS を実行でき、自己 XSS の余地もあります。

**修正案**: 値を `data-*` 属性（Jinja のエスケープが正しく効く）に載せ、`addEventListener` から `dataset` 経由で読む。

---

### 7. 公開カタログ: 為替 API がハングすると通貨切替が永久に無反応

```js
// templates/catalog.html:402
const resp = await fetch('https://open.er-api.com/v6/latest/JPY');
```

タイムアウトがありません。API が応答を返さない（エラーではなく無応答の）場合、`catch` は発火せず `useFallbackRates()` にも到達しません。すると:

- ステータスバッジは **「⏳ Updating exchange rates...」のまま固定**
- `ratesReady` が `false` のままなので `switchCurrency()` は先頭で `return` する → **通貨セレクタを操作しても何も起きず、エラーも出ない**

これは購入者が見る公開画面なので、影響が社外に出ます。

**修正案**: `AbortController` で 3〜5 秒のタイムアウトを設け、超過時は `useFallbackRates()` へ。あわせて、レート未取得の間はセレクタを `disabled` にして状態を可視化する。

---

## P1: レイアウト / レスポンシブ

### 8. ゴミ箱のテーブルだけ横スクロール対応が抜けている

テーブルを持つ15テンプレートのうち、**`trash.html` だけが `.table-responsive` で包んでいません**。

```
admin_dashboard  2/2      pricelist_analytics 3/3
archive          1/1      pricelist_items     1/1
dashboard        2/2      pricing             1/1
...
trash            1/0   ←  ここだけ 0
```

`.table-responsive` は `overflow-x: auto` と、モバイル時の「← スクロール →」ヒント（`style.css:583`）を提供します。ゴミ箱は5列あるので、モバイルでページ全体が横に破綻します。`archive.html` は同等のテーブルで正しく包んでいるため、単純な実装漏れです。

**修正案**: `trash.html` の `<table class="product-table">` を `<div class="table-responsive">` で包む。

---

### 9. ブレークポイントの1px ずれで、iPad の標準幅が「どっちつかず」になる

`style.css` にはモバイル境界が2系統混在しています:

| 境界 | 使用回数 |
|---|---|
| `max-width: 1023px` | 10 |
| `max-width: 1024px` | **1** ← `style.css:1020` |
| `min-width: 1024px` | 7 |
| `max-width: 767px` | 11 |
| `max-width: 768px` | **4** |
| `min-width: 768px` | 3 |

**ちょうど 1024px 幅（iPad 横向き）** では `min-width:1024px` の PC レイアウト（サイドバー表示・ボトムナビ非表示）が適用される一方で、`max-width:1024px` のモバイル用ルールも同時に発火し、`.classification-seo-section` が `grid-template-columns: 1fr !important` で1列に潰されます。

**ちょうど 768px 幅（iPad 縦向き）** も同様に、`max-width:768px` 系と `min-width:768px` 系が二重適用されます。

JS 側は `matchMedia("(max-width: 1023px)")`（`scrape_tracker.js:32`）と `innerWidth < 1024`（`base.html:313`）で 1023/1024 に統一されているため、**CSS の `1024px` 表記だけが浮いています**。

**修正案**: `style.css:1020` を `max-width: 1023px` に、`max-width: 768px` の4箇所を `767px` に統一する。1行ずつの修正で、iPad 実機の見た目が安定します。

---

### 10. 画像ライトボックスがスクレイプ追跡パネルの下に潜る

`:root` に z-index トークンが整備されているのに、実際には生の数値が混在しています。

| 要素 | 値 | トークン経由 |
|---|---|---|
| `.image-lightbox` | **1100** | ✗ 生値 |
| スクレイプ追跡 | 1200 | ✓ `--z-scrape-tracker` |
| 追跡シート | 1201 | ✓ |
| サイドバー | 1301 | ✓ |
| `.action-sheet-overlay` | 2000 / 2001 | ✗ |
| `.esp-toast-viewport` | 2100 | ✗ |
| `.esp-dialog-overlay` | 2200 | ✗ |
| `.loading-overlay` | 9999 | ✗ |

`.image-lightbox`（`style.css:1121`）は `aria-modal="true"` の全画面モーダルですが **1100 < 1200** なので、抽出ジョブ実行中に商品画像を拡大すると、**追跡パネルが全画面画像の上に浮いたまま**になります。モーダルを名乗りながら外側の要素が上に来る状態です。

**修正案**: `--z-image-lightbox` をトークンに追加し（追跡パネルより上、ダイアログより下）、ついでに 2000/2100/2200/9999 もトークン化する。既にスケールがある以上、生値は将来の重なりバグの温床です。

---

## P2: アクセシビリティ

### 11. タグ削除ボタンにフォーカスリングが出ず、タップ領域も 17px

```css
/* static/css/style.css:1096 */
.tag-pill-chip-remove:hover,
.tag-pill-chip-remove:focus-visible {
    color: #1d4ed8;
    outline: none;        /* ← 代替リング無し */
}
```

グローバルのフォーカスリング（`style.css:4570`）は `:is(a, button, ...)` を使っており詳細度は (0,1,1)。上のルールは (0,2,0) で**勝ってしまう**ため、`outline: none` だけが残ります。破壊的操作（タグ削除）のボタンに視覚的フォーカス表示がありません。しかも hover と focus の見た目が同一で区別もつきません。

加えて `padding: 0; font-size: 1.1rem` で実寸 **約17px** です。同じ CSS 内にはタップ領域 44px を保証するルールが20箇所以上あるのに、ここだけ基準から外れています。

**修正案**: `outline: none` を外し（グローバルのリングに任せる）、`min-width/min-height: 24px` 程度＋`::before` で 44px の当たり判定を確保する。

---

### 12. 抽出トラッカーが2秒ごとに読み上げを繰り返し、フォーカスも奪う

`_scrape_tracker.html:9` で外枠に `aria-live="polite"` が付いており、`scrape_tracker.js` は 2秒間隔のポーリングごとに中身を**全消去して再構築**します:

```js
// static/js/scrape_tracker.js:426, 453
listEl.innerHTML = "";        // ← 2秒ごと
mobileListEl.innerHTML = "";
```

カードには「経過 N 秒」（`scrape_tracker.js:404`）が含まれるため中身は毎回変化し、**スクリーンリーダーは2秒おきにトラッカー全体を読み上げ続けます**。同時に、カード内の「閉じる」ボタンにフォーカスを置いていたキーボード操作者は、2秒ごとにフォーカスを body へ飛ばされ、操作を完了できません。

**修正案**: `aria-live` を「状態が遷移した時だけ更新する要約要素」に限定し、カードは差分更新（job_id で既存ノードを再利用）に変える。少なくとも DOM 再構築中はフォーカス位置を復元する。

---

### 13. 抽出シートのヘッダが `role="button"` の中にボタンを2つ抱えている

```html
<!-- templates/_scrape_tracker.html:47-53 -->
<div role="button" tabindex="0" aria-label="商品抽出状況を閉じる">
    ... <p>商品抽出</p> <p id="...Count">0件</p> ...
    <button id="...SheetDismissAll">終わったものを閉じる</button>
    <button id="...SheetClose">閉じる</button>
</div>
```

ボタンの中にボタンを入れる（インタラクティブ要素の入れ子）は ARIA 的に不正で、支援技術によっては内側のボタンが到達不能になります。またヘッダ全体がクリックで閉じるため、タイトルや件数を読もうとしたタップでシートが閉じる挙動になります（JS 側は `stopPropagation` で内側ボタンだけ守っている状態）。

**修正案**: ヘッダから `role="button"` / `tabindex` / クリックハンドラを外し、閉じる操作は既にある「閉じる」ボタンとバックドロップに任せる。

---

### 14. `for` 属性のない `<label>` が約30件

機械検出の結果（入力要素を内包していない、かつ `for` 無しのラベル）:

| ファイル | 件数 | 例 |
|---|---|---|
| `index.html` | 10 | サイト / 最低価格 / 価格倍率 / 在庫数 |
| `product_detail.html` | 7 | オプション1の値 / 共通の販売価格（円） |
| `pricelist_edit.html` | 5 | 画面の色 / 商品の見せ方 / 💱 為替レート |
| `pricing.html` | 4 | ルール名 / 利益上乗せ率 (%) ほか（**編集モーダル内**） |
| `manage_templates.html` | 2 | テンプレート名 / テンプレート内容 |
| `base.html` | 1 | ショップ選択（サイドバー側） |
| `catalog.html` | 1 | Price |

特に目立つもの:

- **`pricing.html`**: 新規作成フォーム（22〜35行）は `for` を正しく付けているのに、**編集モーダル（108〜121行）は同じ項目で `for` が無い**。同一ファイル内での不整合
- **`base.html:166`**: サイドバーのショップ選択ラベルに `for` が無い。モバイルヘッダー側（`base.html:32`）は `for="mobile-shop-select"` で正しく実装済み。`<select>` に `id` を振れば揃う
- **`catalog.html:67`**: 「Price」の下の Min / Max 入力は `placeholder` だけが手掛かり。入力を始めると消えるので、支援技術にも視覚的にも識別子が残らない

ラベル関連付けは支援技術だけの話ではなく、**ラベルをタップして入力欄にフォーカスできる**ようになるため、モバイルの実用性に直結します。

---

### 15. `prefers-reduced-motion` に一切対応していない

```
$ grep -c prefers-reduced-motion static/css/style.css static/css/catalog.css
style.css:0
catalog.css:0
```

一方でスピナー、サイドバーのスライド、トーストのスライドイン、Back-to-top のスムーススクロール（`base.html:330`）など、アニメーションは多用されています。前庭障害のあるユーザーへの配慮として、また WCAG 2.3.3 の観点から、まとめて無効化するブロックを1つ足すのが定石です。

**修正案**:
```css
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
    }
}
```

---

### 16. その他の小さなアクセシビリティ欠落

- **`base.html:229`**: Back-to-top ボタンの中身が「↑」だけで `aria-label` が無い。読み上げは「上矢印、ボタン」
- **`catalog.html:84`**: 絞り込み結果件数 `#resultsSummary` に `aria-live` が無い（空状態の `#catalogEmptyState` には付いている）。検索しても結果件数の変化が読み上げられない
- **`catalog.html:30-31`**: テーマ切替ボタンは `aria-label` と `title` の両方を持ち、JS は `title` だけを更新する（`catalog.html:264`）。`aria-label` が優先されるため、**スクリーンリーダーには常に汎用文言しか届かない**。`aria-pressed` も無い

---

## P3: 一貫性・微調整

### 17. 確認ダイアログが2系統に分かれている

アプリには `ESPUI.confirm()` と `data-confirm-message` による統一ダイアログ（`app_ui.js:548`）がありますが、**4テンプレートがブラウザ標準の `confirm()` を使い続けています**:

| ファイル | 箇所 | 文言 |
|---|---|---|
| `trash.html:29` | 完全削除 | 選択した商品を完全に削除しますか？この操作は取り消せません。 |
| `pricelist_items.html:122` | 価格表から削除 | 選択した商品を価格表から削除しますか？ |
| `pricing.html:86` | ルール削除 | **削除しますか？** |
| `manage_templates.html:44` | テンプレ削除 | 本当に削除しますか？ |

`data-confirm-message` を使っているのは `shops.html` と `pricelist_list.html` の2件だけです。結果として、**同じアプリ内で削除確認の見た目が画面によって変わります**。`pricing.html` の「削除しますか？」は対象名すら示さず、前回監査の「危険操作の文言に対象名を含める」指摘も未反映です。

**修正案**: 4箇所を `data-confirm-message` / `data-confirm-variant="danger"` に置き換える。宣言的な属性なので機械的に移行できます。

---

### 18. アーカイブ復元は「無選択」で無言のまま、エラーは生の例外を表示

```python
# routes/archive.py:71
product_ids = request.form.getlist('ids')
if not product_ids:
    return redirect(url_for('archive.archive_list'))    # ← flash 無し
```

何も選ばずに「選択した商品を復元」を押すと、ページが再読み込みされるだけで**理由の説明がありません**。同じ状況で `routes/trash.py:75` は `flash('商品を選択してください', 'warning')` を出します。ユーザーは「ボタンが壊れている」と受け取ります。

同じファイルで例外時に `flash(f'エラー: {e}', 'error')`（`archive.py:63, 87`）としており、SQLAlchemy の生のスタック文言が画面に出ます。他のルートは例外を送出してエラーページに委ねているので、ここも不統一です。

**修正案**: 空選択時に `flash('商品を選択してください', 'warning')` を追加。例外時は定型文言に置き換える。

---

### 19. 「価格不明」の表示が画面ごとに違う

```jinja
{# templates/archive.html:41 #}
¥{{ "{:,}".format(p.last_price or 0) }}     → 価格不明でも「¥0」と表示

{# templates/trash.html:52 #}
{% if item.last_price %}¥{{ ... }}{% endif %}  → 価格不明なら空欄
```

アーカイブ画面では価格未取得の商品が**「¥0」（無料）として表示されます**。仕入れ管理ツールとしては誤解を招く表示です。

**修正案**: `_money.html` に「不明」表示のマクロを追加し、両画面で `—` などに統一する。

---

### 20. 同じボタンの名前が画面幅で変わる

`index.html` のエクスポートボタンは、モバイルと PC で**同じ処理に別の名前**が付いています:

| 送信先 | モバイル（183-185行） | PC（213-216行） |
|---|---|---|
| `export_shopify` | Shopify CSV | 出品用データ |
| `export_stock_update` | 在庫更新CSV | 在庫だけ更新 |
| `export_price_update` | 価格更新CSV | 価格だけ更新 |
| `export.export_images` | **（無し）** | 📷 画像ZIP |

サポート時に「出品用データを押してください」と伝えてもモバイルには存在せず、手順書やスクリーンショットも端末ごとに食い違います。**画像ZIP はモバイルから実行できません**（意図的な制限なら、その旨の表示が要ります）。

**修正案**: P0-2 のブロック統合と同時に解消する。

---

### 21. ログインと新規登録で作りが揃っていない

| 項目 | `login.html` | `register.html` |
|---|---|---|
| パスワード表示トグル | あり（**ただし動かない** → P0-1） | 無し |
| エラーの `role="alert"` | あり（22行） | **無し**（15行） |
| `autofocus` | あり | 無し |
| パスワード確認欄 | — | 無し |
| ルールの即時検証 | — | `minlength="12"` のみ |

新規登録では「12文字以上で、英字と数字を含めてください」と案内しつつ、**英字・数字の混在チェックはサーバ側だけ**です。`aaaaaaaaaaaa` はブラウザ検証を通過してから弾かれます。確認欄も表示トグルも無いので、打ち間違いに気付く手段がありません。パスワード表示トグルが最も要るのはこちらの画面です。

また `<p class="help-text">` が `aria-describedby` で入力欄に紐付いていないため、支援技術には要件が伝わりません。

---

### 22. 外部 CDN 依存にフォールバックも SRI も無い

| ライブラリ | 読み込み元 | `integrity` |
|---|---|---|
| TinyMCE 6.8.3 | cdnjs | 無し（3ファイル） |
| Chart.js 4.4.1 | jsdelivr | 無し |
| Sortable 1.15.6 | cdnjs | 無し |
| Inter（Google Fonts） | fonts.googleapis.com | — |

`integrity` 属性が無いため改ざん検知が効きません。また失敗時の挙動が不揃いです—Chart.js は `typeof Chart === 'undefined'` でガード済み（`pricelist_analytics.html:148`）、Sortable もガード済み（`product_detail.html:2086`）ですが、**どちらも「読み込めなかった」旨をユーザーに伝えません**。並べ替えようとしても動かず、グラフは空欄のまま—原因が分からないまま放置されます。TinyMCE に至ってはガードすら無い（P0-4）。

なお公開カタログの Google Fonts は、購入者の閲覧情報が Google に渡る構成でもあります。

**修正案**: `integrity` + `crossorigin` を付ける。ガード済みの箇所では失敗時に一言表示する。

---

### 23. 送信ボタンの busy 化に2つの副作用

`app_ui.js:559` は送信時にボタンを `disabled` にして「処理中...」へ差し替えます。

1. **`disabled` にすると、そのボタンの `name`/`value` が送信内容から落ちます。** HTML の仕様上、フォームデータの構築は `submit` イベントのハンドラ実行**後**に行われ、無効化された要素は除外されます。現状 `data-loading-label` を持つボタンに `name` 付きのものは無いので実害は出ていませんが、複数の送信ボタンを `name` で区別するフォームを今後追加すると、静かに壊れます

2. **ブラウザの「戻る」で押せないボタンが残ります。** bfcache から復帰したページは DOM 状態がそのままなので、ボタンは `disabled` かつ「処理中...」表示のまま固定されます。`pageshow` で `setButtonBusy(btn, false)` に戻す処理がありません

**修正案**: 無効化ではなく `aria-disabled` + 送信ガードにする。あわせて `pageshow`（`event.persisted`）で busy 状態を解除する。

---

### 24. 公開カタログの細かい引っかかり

- **通貨の選択が保存されない**: テーマは `localStorage`（`catalog.html:272`）に保存されるのに、通貨は保存されません。再訪のたびに JPY に戻ります
- **価格フィルタが通貨に追従しない**: Min/Max は常に**円**で判定されます（`filterProducts()` は `data-jpy-price` を直接比較）。USD 表示に切り替えた購入者が「50」と入力すると 50円以下で絞り込まれ、商品がほぼ全て消えます。入力欄に単位表示もありません
- **レート表示の書式が2通り**: 初回取得時は `1 USD = ¥157`（410行）、通貨切替後は `Rate: 1 USD = ¥157`（480行）。同じバッジが経路によって別の見た目になります
- **`<html lang="en">`** ですが、出品者の商品名は日本語です。スクリーンリーダーの読み上げ言語が合いません

---

### 25. エラーページが行き止まり

`error.html` のリンクは `<a href="/">トップページへ戻る</a>` の1本のみで、URL もハードコードです（他は全て `url_for`）。深い階層で 404 / CSRF エラーに当たると、ナビゲーションが一切無い画面に落ちて文脈を失います。

**修正案**: `url_for('index')` に変更し、「前のページへ戻る」と主要導線（商品一覧・商品抽出）へのリンクを追加する。

---

### 26. デザイントークンが定着していない

`:root` には配色・余白・z-index の体系が整備されています（`style.css:1-40`）。しかしテンプレート側では:

| ファイル | ハードコードされた色 | インライン `style=` |
|---|---|---|
| `product_detail.html` | 130 | — |
| `pricelist_edit.html` | 34 | 27 |
| `import.html` | 19 | 29 |
| `pricelist_analytics.html` | 16 | 21 |
| `pricelist_items.html` | 14 | 14 |

`trash.html` の削除ボタンは `background: #c62828` を直書きしていますが、これはトークンの `--danger-color: #dc3545` とも `.btn-danger` とも違う赤です。同じ「危険な操作」が画面ごとに別の色で出ています。前回監査の指摘（Medium: デザインシステムの芯がCSSに表現されていない）が未着手のまま残っている領域です。

---

## 推奨する着手順

**まず直すべき（体験が壊れている / 出力が間違う）**

1. ログインのパスワードトグルを動かす（P0-1）— `<script>` 1行
2. エクスポート設定の重複入力を解消（P0-2）— PC の設定が効かない
3. 未定義ハンドラ4件を実装または削除（P0-3）
4. `tinymce.init` をガード（P0-4）— 3ファイル
5. 一括操作ダイアログの Enter 対策（P0-5）
6. `editRule` を `data-*` 属性経由に（P0-6）
7. 為替 fetch にタイムアウト（P0-7）

**次に（見た目の破綻）**

8. `trash.html` に `.table-responsive`（P1-8）— 1行
9. ブレークポイントを 1023/767 に統一（P1-9）— 5行
10. ライトボックスの z-index をトークン化（P1-10）

**その後（品質の底上げ）**

11. `prefers-reduced-motion` ブロックを追加（P2-15）— 1ブロックで全体に効く
12. `outline: none` の削除とタップ領域拡大（P2-11）
13. 確認ダイアログを `data-confirm-message` に統一（P3-17）
14. `for` 属性の付与（P2-14）— 特に `pricing.html` と `base.html`
15. トラッカーを差分更新に（P2-12）

---

## 補足

- 本レポートは静的解析に基づきます。実機ブラウザでの確認により、余白・折り返し・スクロール挙動について追加の発見が出る可能性があります
- P0-1 / P0-2 / P0-3 / P0-5 は再現手順が明確なので、リグレッションテストを書ける対象です
- 前回監査で指摘された「フィルタ UI の重複」は未解消で、本レポートの P0-2 はその重複が実害（誤ったエクスポート出力）に至った例です。include 化はスタイルの問題ではなく機能の問題として優先度を上げる価値があります
