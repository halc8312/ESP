# ESP 商品編集ページ E2E テスト手順

`templates/product_detail.html` の sticky header / 仕入設定カード / readiness popover / save round-trip / `product_manual_add` 2 カラム regression を実機で検証するための手順。

## 1. ローカル Flask dev 起動

```bash
cd /home/ubuntu/repos/ESP
WTF_CSRF_ENABLED=0 FLASK_APP=app.py FLASK_ENV=development python -m flask run --port 5050 --host 127.0.0.1
```

- `WTF_CSRF_ENABLED=0` を入れないと、ブラウザ自動操作経由の POST が CSRF で 400 弾かれる。
- ポートは 5050 を使用。プロダクション・他テストとぶつけないため。

## 2. テスト用ユーザー

- ログイン: `tester` / `<TESTER_PASSWORD>` (テスト用の付与値。local DB のみで使用)
- パスワードをリセットするなら以下のスニペットを使用 (コード中のパスワードは入れず、コマンドラインで受け取る)

```bash
cd /home/ubuntu/repos/ESP
NEW_PASS='<TESTER_PASSWORD>' python -c "
import os
from app import app
from database import session_scope
from models import User
with app.app_context():
    with session_scope() as s:
        u = s.query(User).filter_by(username='tester').one()
        u.set_password(os.environ['NEW_PASS'])
        s.commit()
        print('reset:', u.username)
"
```

## 3. 検証用商品 (id=1) 前提

商品 id=1 (`tester` 所有) は以下のソースデータを持つ前提:

| カラム | 期待値 |
|---|---|
| `site` | `mercari` |
| `last_price` | `2000` |
| `last_status` | `on_sale` |
| `last_title` | `テスト商品 1 ヴィンテージ カメラ` |
| `source_url` | `https://example.com/item/1` |
| `tags` | `vintage,black,studio` |
| `manual_margin_rate` | `NULL` |
| `manual_shipping_cost` | `NULL` |

DB スキーマは `products.title` ではなく `products.last_title` / `products.custom_title` の二段構成。修正系 SQL を書くときは `PRAGMA table_info(products)` でカラム名を必ず確認すること。

## 4. テスト後の test data restore

UI 操作で値をいじったら必ず復元すること:

```bash
cd /home/ubuntu/repos/ESP
python -c "
import sqlite3
conn = sqlite3.connect('/home/ubuntu/repos/ESP/mercari.db')
c = conn.cursor()
c.execute(\"UPDATE products SET tags='vintage,black,studio', manual_margin_rate=NULL, manual_shipping_cost=NULL WHERE id=1\")
conn.commit()
c.execute('SELECT id, custom_title, tags, manual_margin_rate, manual_shipping_cost FROM products WHERE id=1')
print('AFTER:', c.fetchone())
conn.close()
"
```

## 5. 検証スニペット (browser console から走らせる)

### sticky header の構造と pin

```js
var h=document.querySelector('.product-edit-page-header');
var cs=getComputedStyle(h);
console.log({position:cs.position, top:cs.top, zIndex:cs.zIndex,
  back:!!h.querySelector('.product-edit-back-icon'),
  saveBtn:!!h.querySelector('.product-edit-header-save'),
  readiness:!!h.querySelector('#heroReadinessBadge')});
window.scrollTo(0, 2500);
setTimeout(()=>console.log('after scroll top=', h.getBoundingClientRect().top), 200);
```

PASS: `position:sticky / top:0px / zIndex:30`、スクロール後も `top` が 0 付近。

### popover の icon サイズ (22×22px regression check)

```js
var icons=[...document.querySelectorAll('.product-edit-checklist-popover .product-edit-check-icon')];
console.log(icons.map(i=>{var r=i.getBoundingClientRect();return Math.round(r.width)+'x'+Math.round(r.height)}));
```

PASS: 全要素 `22x22`。FAIL: `42x42` (= `min-width:42px` 継承のリグレッション = `8868a33` の修正が外れた疑い)。

### popover が DOM 破壊されていないか

```js
var pop=document.querySelector('.product-edit-checklist-popover');
console.log({exists:!!pop, items:pop?.querySelectorAll('li[data-check-key]').length});
```

PASS: `items:5`。FAIL: 0 件 = `heroReadinessBadge.textContent` が popover を上書きしている可能性 (`9340fab` の修正が外れた疑い)。

### live readiness 更新

```js
var t=document.querySelector('input[name="title"]');
var prev=t.value; t.focus(); t.value=''; t.dispatchEvent(new Event('input',{bubbles:true}));
setTimeout(()=>{console.log(document.querySelector('#heroReadinessText').textContent);
  t.value=prev; t.dispatchEvent(new Event('input',{bubbles:true}));}, 400);
```

PASS: title クリア時にカウントが下がり、復元で 5/5 に戻る。

Note: TinyMCE 初期化が遅延すると description が一時的に空判定 → 直後 OK 判定に切り替わるため、最初の更新でカウントが変わらないことがある。`title` クリア後 数秒待ってから再度確認すること。

### `product_manual_add.html` 2 カラム regression

```js
// /products/manual-add で実行
var layout=document.querySelector('.product-edit-layout');
var cs=getComputedStyle(layout);
var tracks=cs.gridTemplateColumns.split(' ').filter(Boolean).length;
var side=document.querySelector('.product-edit-side');
var sidePos=side?getComputedStyle(side).position:'(no side)';
console.log({viewport:innerWidth+'x'+innerHeight, tracks, gridCols:cs.gridTemplateColumns, sidePos});
```

PASS (≥1024px viewport): `tracks:2`, `sidePos:sticky`、grid に `Xpx Ypx` 形式の 2 値。
FAIL: `tracks:1` = 共通 CSS から `.product-edit-layout` の grid が消えたリグレッション (`c497567` の修正が外れた疑い)。`product_detail.html` の inline `<style>` で **scoped 単一カラム override** を使っているか確認すること (グローバル CSS で 1 カラム化してはいけない)。

PASS (<1024px): `tracks:1` (mobile breakpoint で collapse は OK)。

## 6. 仕入設定 (自動取得) read-only card

商品 1 の場合の期待値:

```
取得元サイト: mercari
仕入れ価格 (記録): ¥2,000
仕入れ元の状態: on_sale
最後に取得したタイトル: テスト商品 1 ヴィンテージ カメラ
取得元URL: https://example.com/item/1 (clickable, target=_blank)
```

`#productSourcePanel` 内に `<input>/<textarea>/<select>` が 0 件であること (read-only)。

## 7. 公開カタログでの source_url leak (高リスク不変)

`AGENTS.md` 記載: `source_url` / `site` は public カタログに **絶対漏らさない**。手動検査するなら curl で 認可なしアクセスして response HTML を grep:

```bash
curl -s http://127.0.0.1:5050/catalog | grep -E 'example.com/item|mercari'
```

出力が空であること。出力があれば即修正。CI 側では `tests/test_e2e_routes.py` でカバー。

## 8. console error チェック

```js
var errs=[];
var orig=console.error;
console.error=function(){errs.push([...arguments].join(' '));orig.apply(console,arguments)};
window.addEventListener('error',e=>errs.push('JS:'+e.message));
setTimeout(()=>console.log('ERRORS:', errs.length, errs), 2000);
```

PR1 以降は header / popover まわりで `Cannot read … of null` が再発しやすい (旧 sidebar 要素 `summaryImageValue`, `checklistCountBadge`, `heroShopBadge` などが消えているため)。`updateProductEditSummary` の null guard が機能しているかも併せて確認。

## 9. 関連ファイル

- `templates/product_detail.html` — 主たる編集対象
- `templates/product_manual_add.html` — 共通 CSS の regression 監視対象 (PR1 で 1 度壊した実績あり)
- `static/css/style.css` — `.product-edit-layout` / `.product-edit-side` / `.product-edit-checklist-popover` / `.product-edit-check-icon` を持つ
- `routes/products.py` — `name=` 属性に依存する保存ロジック (PR1〜PR4 の段階では一切触らない)
- `tests/test_e2e_routes.py` — 95 件、CI ゲート

## 10. Devin Secrets Needed

この skill には外部 secret は不要 (ローカル DB と Flask dev で完結)。`tester` ユーザーのパスワードはテスト用で、セッション間で使い回してよいものを `NEW_PASS` env var として渡す。

## 11. 関連 PR と既知の落とし穴

- **PR #96** (merged): タグ pill / 画像ライトボックス / manual price override。`/api/products/<id>/recalc-price` が CSRF を要求するため、ブラウザ JS は `<meta name="csrf-token">` を読んで `X-CSRFToken` header に渡している。CSRF 無効化での dev 起動でも動作確認できるが、本番準拠でテストするなら CSRF 有効化のうえ meta タグの値が空でないことを別途確認する。
- **PR #97** (open): 商品編集のフラットセクション化 + sticky header。Devin Review が以下の 4 件を捕捉した実績あり:
  1. CSS class `.check-mark` が無く `.product-edit-check-icon` を参照すべき
  2. `heroReadinessBadge.textContent =` が popover DOM を破壊
  3. popover icon が `min-width: 42px` 継承で 22px にならない
  4. 共通 CSS の grid 削除が `product_manual_add.html` を破壊
  
  すべて regression 検証スニペット (§5) でカバー済。
