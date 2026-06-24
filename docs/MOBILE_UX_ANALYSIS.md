# モバイル UX 分析レポート

**作成日**: 2026-03-14  
**対象リポジトリ**: halc8312/ESP  
**分析者**: Mobile-First UX Reviewer  
**分析対象**: `templates/` 全ファイル + `static/css/style.css`

---

## ✅ 改善サマリ（3行以内）

モバイルファースト設計の骨格（ボトムナビ・ドロワーサイドバー・スティッキーヘッダー）は良質な出発点。  
一方で、インラインスタイルの多用・フィルタ UI の重複・固定要素の z-index/重複配置がスマホ体験のボトルネックになっている。  
優先度の高い 5 点を潰すだけでタップ品質・視認性・操作確実性が大幅に改善できる。

---

## 📊 現状の実装状況サマリ

| 観点 | 評価 | 備考 |
|------|------|------|
| viewport meta | ✅ 全ページ正常 | `width=device-width, initial-scale=1.0` |
| ボトムナビ | ✅ 実装済み | 60px 固定、safe-area 対応 |
| ドロワーサイドバー | ✅ 実装済み | Escape キー・オーバーレイタップで閉じる |
| フォーム入力 font-size | ⚠️ 一部 14px | `filter-input-keyword`, `filter-select` が 14px → iOS ズーム発生 |
| タップターゲット | ✅ ほぼ 44px 以上 | 設定ページの削除ボタン(×)のみ例外 |
| `inputmode` 属性 | ✅ 数値フォームに付与 | `inputmode="numeric"`, `inputmode="decimal"` 付与済み |
| `autocomplete` | ✅ login / register に付与 | |
| safe-area-inset | ✅ ボトムナビ・スティッキー保存に適用 | |
| `aria-current` | ✅ base.html ナビに付与済み | |
| `prefers-reduced-motion` | ❌ 未対応 | スピナー・プログレスバーなどのアニメーション |
| ダークモード | ❌ 未対応 | `prefers-color-scheme` なし |

---

## 📱 問題点（ユーザー行動ベース）

### 🔴 高優先度（High）

---

#### H-1. ショップセレクタの誤タップ即送信（`base.html`）

**ユーザー行動**: スマホでヘッダーのショップ選択ドロップダウンをスクロール中に誤タップ → 画面がリロードされてどの操作が起きたか分からない。

**コード根拠**:
```html
<!-- templates/base.html:31 -->
<select ... data-auto-submit="true">
```
```js
// templates/base.html:229-233
select.addEventListener('change', function () {
    if (select.form) { select.form.submit(); }
});
```

**影響範囲**: 全ページ（base.html の共通ヘッダー）

**再現手順**:
1. iPhone Safari でいずれかのページを開く
2. ヘッダー右上のショップ選択ドロップダウンに触れる
3. 意図せずショップが切り替わり画面がリロードされる

---

#### H-2. フィルター入力 font-size が 14px → iOS 自動ズーム（`style.css`）

**ユーザー行動**: 商品一覧の検索フィールドをタップ → iOS Safari がページを自動ズームしてレイアウトが崩れる。

**コード根拠**:
```css
/* static/css/style.css:2695-2696 */
.filter-input-keyword { font-size: 14px; }
.filter-select        { font-size: 14px; }
```

基底ルール（行 221〜228）は 16px で正しく設定されているが、`.filter-input-keyword` と `.filter-select` の上書き指定が 14px になっており iOS ズームが発生する。

**影響範囲**: `templates/index.html` の商品一覧フィルタ

---

#### H-3. 固定要素の重複と z-index 競合（`product_detail.html`）

**ユーザー行動**: 商品編集ページでスクロールすると、スティッキー保存ボタン・ボトムナビ・プログレスパネルが重なり、保存ボタンが隠れることがある。

**コード根拠**:
```css
/* style.css:2290-2310 */
.sticky-save-container {
    position: fixed;
    bottom: calc(var(--bottom-nav-height) + env(safe-area-inset-bottom));
    z-index: 997;
}
/* bottom-nav: z-index: 999 */
/* scrape-progress-panel: z-index: 9999 */
```

`sticky-save-container`（z-index: 997）が `bottom-nav`（z-index: 999）の下にくる計算になっているが、端末によってはボタンがナビと重なる。また TinyMCE のツールバーが open 状態で上記 z-index をすべて上回る場合がある。

**影響範囲**: `templates/product_detail.html`（`has-sticky-save` クラス付きページ）

---

#### H-4. ログイン・登録ページが英語表記（`login.html`, `register.html`）

**ユーザー行動**: スマホからアクセスするとログイン画面が突然英語になり、日本語アプリとの言語的不整合で混乱する。

**コード根拠**:
```html
<!-- templates/login.html:1-34 -->
<!DOCTYPE html>
<html lang="ja">   <!-- lang は ja だが -->
<title>Login - ESP</title>  <!-- タイトルは英語 -->
<h1>Login</h1>
<label>Username</label>
<label>Password</label>
<button>Log In</button>
<a>Don't have an account? Create Account</a>
```

`register.html` も同様。アプリ全体は日本語なのにエントリーポイントだけ英語。

**影響範囲**: `templates/login.html`, `templates/register.html`

---

### 🟡 中優先度（Medium）

---

#### M-1. フィルタ UI の重複（モバイル用・PC 用の 2 定義）

**ユーザー行動**: PC でバグを直したのにスマホでは直っていない、またはその逆。ユーザーには見えないが保守コストと体験ずれの温床。

**コード根拠**:
```html
<!-- templates/index.html:94-128 モバイル用 -->
<details class="mobile-collapsible mobile-only">
    <summary>⚙️ エクスポート設定</summary>
    <div class="mobile-collapsible-content">
        <div class="export-grid">...</div>
    </div>
</details>

<!-- templates/index.html:131-163 PC用 -->
<div class="export-section desktop-only">
    <div class="export-section-body">
        <div class="export-grid">...</div>
    </div>
</div>
```

同様の重複がフィルタセクション（`filter-collapsible` 単体 + `filter-advanced` 内にも同等フィールドあり）にも見られる。

**影響範囲**: `templates/index.html`

---

#### M-2. `pricelist_analytics.html` のグリッドがモバイル非対応

**ユーザー行動**: 価格表のアクセス解析ページをスマホで開くとグラフが横2列で表示され、幅不足で読めない。

**コード根拠**:
```html
<!-- templates/pricelist_analytics.html:37 インラインスタイル -->
<div style="display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; align-items: start;">

<!-- 54行 同様 -->
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px;">
```

これらはインラインスタイルで書かれているため、CSS のメディアクエリで上書きできない。スマホ（< 768px）でも強制的に 2 カラムになる。

**影響範囲**: `templates/pricelist_analytics.html`

---

#### M-3. 設定ページの削除ボタンがタップ困難（`settings.html`）

**ユーザー行動**: 除外キーワードを削除しようとするが「×」ボタンが小さすぎて押せない。隣のキーワードテキストを誤タップしてしまう。

**コード根拠**:
```html
<!-- templates/settings.html:インラインスタイル -->
<button type="submit"
    style="border: none; background: none; color: #d32f2f; cursor: pointer;
           font-size: 1.2em; padding: 0;"
    title="削除">×</button>
```

`padding: 0` で `min-height` も未設定。Apple HIG 推奨の 44×44px タップターゲットを大きく下回る。

**影響範囲**: `templates/settings.html`

---

#### M-4. `import.html` のインラインスタイル多用でモバイル再現困難

**ユーザー行動**: CSVインポートのプレビュー表がスマホで横スクロールできない、または行の高さが崩れる。

**コード根拠**:
```html
<!-- templates/import.html:36 -->
<div class="table-responsive" style="max-height: 400px; overflow-y: auto;">
    <table class="product-table" style="font-size: 0.9em;">
        <thead style="position: sticky; top: 0; background: white;">
```

インラインの `overflow-y: auto` と `position: sticky` がモバイルの `-webkit-overflow-scrolling: touch` と干渉し、スクロール挙動が不安定になるリスクがある。

**影響範囲**: `templates/import.html`

---

### 🟢 低優先度（Low）

---

#### L-1. `prefers-reduced-motion` 未対応

スクレイプ進行中のスピナー・プログレスバーのアニメーションが、前庭障害など動きに敏感なユーザーに影響する可能性。Apple HIG / WCAG 2.1 SC 2.3.3 (AAA) で推奨される。

**コード根拠**:
```css
/* style.css には prefers-reduced-motion の記述なし */
animation: scrape-progress-shift 1.4s linear infinite;
```

---

#### L-2. ダークモード未対応

CSS カスタムプロパティ（`--primary-color` 等）は定義されているが `@media (prefers-color-scheme: dark)` のオーバーライドが存在しない。OS ダークモード設定のユーザーは白背景のまま。

---

#### L-3. `pricelist_items.html` のボタンにインラインスタイル

```html
<!-- templates/pricelist_items.html:16-17 -->
<a href="..." class="btn btn-primary"
   style="padding: 10px 16px; background: #0066cc; color: white; ...">
```

`btn-primary` クラスが既に同等のスタイルを持っているため二重定義。将来テーマ変更時に取り残される。

---

## 🧩 改善提案（優先度順）

| # | 優先度 | 対象ファイル | 内容 | 工数目安 |
|---|--------|------------|------|---------|
| 1 | 🔴 High | `base.html` | ショップセレクタ自動送信廃止 → 確認ボタン追加または変更後トースト表示 | S（30分） |
| 2 | 🔴 High | `style.css` | `.filter-input-keyword`, `.filter-select` の `font-size` を `16px` に修正 | XS（5分） |
| 3 | 🔴 High | `login.html`, `register.html` | UI テキストを日本語化（ラベル・ボタン・リンク） | S（20分） |
| 4 | 🟡 Medium | `pricelist_analytics.html` | インライングリッドをクラス化し `@media (max-width: 767px)` で1カラムにフォールバック | S（40分） |
| 5 | 🟡 Medium | `settings.html` | 削除ボタン（×）に `min-width: 44px; min-height: 44px; padding: 0 12px;` を付与 | XS（10分） |

---

## 🔧 実装方針（概要）

### 提案 1: ショップセレクタ（H-1）

```html
<!-- base.html: data-auto-submit="true" を削除 -->
<!-- 「適用」ボタンを既存のサイドバー内ボタン同様に追加 -->
<select name="shop_id" id="mobile-shop-select" class="shop-select-compact">
  ...
</select>
<button type="submit" class="btn btn-compact btn-primary">適用</button>
```

または変更時にのみトーストを表示し、ユーザーに変更が起きたことを通知する方式でも可。

---

### 提案 2: フィルタ入力 font-size（H-2）

```css
/* style.css: 2695-2696 の修正 */
.filter-input-keyword { font-size: 16px; }
.filter-select        { font-size: 16px; }
```

---

### 提案 3: ログイン日本語化（H-4）

```html
<!-- login.html -->
<title>ログイン - ESP</title>
<h1>ログイン</h1>
<label for="username">ユーザー名</label>
<label for="password">パスワード</label>
<button type="submit">ログイン</button>
<a href="...">アカウントをお持ちでない方はこちら</a>
```

---

### 提案 4: アナリティクスページのレスポンシブ対応（M-2）

```css
/* style.css へ追加 */
.analytics-chart-grid {
    display: grid;
    grid-template-columns: 1.4fr 1fr;
    gap: 16px;
    align-items: start;
}
.analytics-bottom-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-top: 16px;
}
@media (max-width: 767px) {
    .analytics-chart-grid,
    .analytics-bottom-grid {
        grid-template-columns: 1fr;
    }
}
```

```html
<!-- pricelist_analytics.html: インラインスタイルをクラスに置換 -->
<div class="analytics-chart-grid">...</div>
<div class="analytics-bottom-grid">...</div>
```

---

### 提案 5: 設定ページ削除ボタン（M-3）

```css
/* style.css へ追加 */
.keyword-delete-btn {
    border: none;
    background: none;
    color: var(--danger-color);
    cursor: pointer;
    font-size: 1.2em;
    min-width: 44px;
    min-height: 44px;
    padding: 0 8px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
}
.keyword-delete-btn:hover {
    background: #ffebee;
}
```

---

## 🧪 検証方法

### 必須テスト端末
- iPhone SE（375px / Safari）
- Android 標準（360px / Chrome）
- iPad（768px / Safari）

### 重要フロー別検証手順

#### フロー 1: ショップ切り替え誤操作
1. iPhone で商品一覧ページを開く
2. ヘッダー右上のショップセレクタをタップ
3. **期待**: 「適用」ボタンを押すまで送信されない（H-1 修正後）
4. **確認ポイント**: ページがリロードされないこと

#### フロー 2: フィルタ検索 iOS ズーム
1. iPhone Safari で商品一覧を開く
2. キーワード検索フィールドをタップ
3. **期待**: ページが自動ズームしない（H-2 修正後）
4. **確認ポイント**: `window.visualViewport.scale` が 1 のまま

#### フロー 3: 商品編集の保存ボタン
1. iPhone で商品詳細ページを開く
2. 下部にスクロール
3. **期待**: 「保存」ボタンがボトムナビと重ならず全体が見える
4. **確認ポイント**: スティッキー保存とボトムナビの overlap なし

#### フロー 4: アクセス解析レイアウト
1. iPhone でいずれかの価格表 → アクセス解析ページを開く
2. **期待**: グラフが 1 カラムで縦積みになる（M-2 修正後）
3. **確認ポイント**: グラフ幅が画面幅に収まる

---

## 📚 参考（1〜3件）

1. **Apple HIG – Controls**: タップターゲット最小 44×44pt（what to learn: 設定ページの削除ボタン改善の基準として）
2. **Luke Wroblewski「Mobile First」**: フォームは必須フィールドのみ常時表示、詳細は折りたたみ（what to learn: フィルタ重複の解消方針）
3. **WCAG 2.1 SC 1.4.4**: テキストは 200% ズームでも機能すること（what to learn: 14px フォントサイズ修正の根拠）

---

## 付録: ファイル別インラインスタイル件数

| テンプレート | `style=` 件数 | リスク |
|-------------|-------------|--------|
| `import.html` | 32 | 高（レイアウト・色・サイズをすべてインラインで管理） |
| `pricelist_list.html` | 23 | 中 |
| `settings.html` | 21 | 中（削除ボタンのタップターゲット問題を含む） |
| `pricelist_analytics.html` | 16 | 高（グリッドのレスポンシブ化をブロック） |
| `pricing.html` | 16 | 中 |
| `pricelist_items.html` | 15 | 中（btn-primary との二重定義） |
| `pricelist_edit.html` | 15 | 中 |
| `product_detail.html` | 19 | 中 |
| `dashboard.html` | 13 | 低 |

インラインスタイルの多い上位ファイルから順次クラス化を進めると、CSS のデザイントークン（`:root` 変数）の恩恵が全体に広がる。

---

*このレポートはコードの静的レビューに基づいています。実機確認により追加の課題が見つかる可能性があります。*
