# セキュリティチェックレポート

**対象リポジトリ**: halc8312/ESP  
**チェック実施日**: 2026-04-03  
**チェック基準**: Webアプリ向けセキュリティチェックマニュアル（情報処理安全確保支援士ベース）

---

## エグゼクティブサマリー

| カテゴリ | 評価 | 主な懸念事項 |
|---|---|---|
| インフラ・HTTPS/HSTS | ⚠️ 要確認 | アプリ側にHSTSヘッダー未設定 |
| 認証・セッション管理 | ⚠️ 要改善 | ブルートフォース対策なし、Cookie設定未明示、CSRF未対策 |
| 入力値処理（XSS） | ⚠️ 要改善 | `\| safe` フィルターの不適切使用 |
| 入力値処理（SQLi） | ✅ おおむね良好 | ORM使用、一部f-string利用あり（低リスク） |
| OSコマンドインジェクション | ✅ 良好 | Webルート内での使用なし |
| エラーメッセージ管理 | ⚠️ 要改善 | 例外詳細がユーザーに露出する箇所あり |
| 秘匿情報管理 | ⚠️ 要注意 | SECRET_KEYのデフォルト値、.gitignoreに`.env`未記載 |
| 依存ライブラリ管理 | ⚠️ 要改善 | Dependabot未設定 |
| アクセス制御 | ✅ おおむね良好 | 一部設計上の検討事項あり |

---

## 2. インフラ・ネットワーク層

### 2-1. HTTPS化とHSTSの徹底

**評価**: ⚠️ 要確認

**調査結果**:
- デプロイ基盤は **Render** であり、HTTPS自体はRenderによって提供される。
- ただし、アプリケーションコード（`app.py`）に **`Strict-Transport-Security` ヘッダーを付与する処理が存在しない**。
- `after_request` フック等でのセキュリティヘッダー設定も確認されなかった。

```python
# app.py - セキュリティヘッダー設定なし
# @app.after_request 等でのHSTSヘッダー設定が未実装
```

**推奨対応**:
```python
@app.after_request
def add_security_headers(response):
    response.headers['Strict-Transport-Security'] = 'max-age=15768000; includeSubDomains'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
```

---

### 2-2. 不要なポートの閉鎖

**評価**: ✅ 問題なし（インフラ依存）

**調査結果**:
- `render.yaml` において Webアプリはポート `10000`（内部）のみ開放し、外部公開はRenderのリバースプロキシ経由。
- アプリ自体がポートをバインドするのは `0.0.0.0:${PORT:-10000}` のみ（gunicorn）。
- ポートスキャンはデプロイ先Renderのインフラ側確認が必要。

---

### 2-3. DNS設定の適正化

**評価**: ℹ️ インフラ（Render）に委任

**調査結果**:
- DNS管理はRenderおよびドメインレジストラ側の設定に依存する。
- アプリコード側での確認箇所なし。Renderの公式ドキュメントに沿ってDNS設定を確認すること。

---

## 3. 認証・セッション管理

### 3-1. ブルートフォース／パスワードリスト攻撃対策

**評価**: ❌ 未対策

**調査結果**:
- `routes/auth.py` の `/login` エンドポイントに **レート制限なし**。
- アカウントロック機能なし。
- OTP / SMS / 多要素認証（MFA）の実装なし。
- `flask-limiter` や同等ライブラリは `requirements.txt` に含まれていない。

**該当コード**:
```python
# routes/auth.py:13-33
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # ← 試行回数チェックなし
        user = session_db.query(User).filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
```

**推奨対応**: `flask-limiter` 等を用いてログインエンドポイントへのレート制限を実装する。

---

### 3-2. Cookie属性の設定

**評価**: ⚠️ 要確認・要明示設定

**調査結果**:
- `app.py` に `SESSION_COOKIE_SECURE`、`SESSION_COOKIE_HTTPONLY`、`SESSION_COOKIE_SAMESITE` の明示的設定なし。
- Flaskのデフォルト: `HttpOnly=True`、`Secure=False`（HTTPS環境でも明示設定が必要）。
- `ProxyFix` を使用しているため、HTTPS終端はRenderのプロキシだが、Secure Cookieを確実にするにはアプリ側の設定が必要。

**推奨対応**:
```python
app.config.update({
    'SESSION_COOKIE_SECURE': True,      # HTTPSのみ送信
    'SESSION_COOKIE_HTTPONLY': True,    # JS からのアクセス禁止
    'SESSION_COOKIE_SAMESITE': 'Lax',  # CSRF軽減
})
```

---

### 3-3. セッションIDの露出

**評価**: ✅ 問題なし

**調査結果**:
- URLパラメータやリダイレクトURL中にセッションIDが含まれるコードは確認されなかった。
- Flask-Login の標準的なセッション管理を使用。

---

### 3-4. CSRF（クロスサイトリクエストフォージェリ）対策

**評価**: ❌ 未対策

**調査結果**:
- `Flask-WTF` / `flask-csrf` 等のCSRF保護ライブラリが **`requirements.txt` に含まれていない**。
- フォーム送信（ログイン、商品編集、スクレイピング実行）にCSRFトークンが実装されていない。
- 状態変化を伴う操作が `POST` ルートで提供されているが、CSRF検証がない。

**推奨対応**: `Flask-WTF` を導入し、`CSRFProtect(app)` を有効にする。

---

### 3-5. ユーザー登録の公開性

**評価**: ⚠️ 設計確認を推奨

**調査結果**:
- `/register` エンドポイントは **招待コードや管理者承認なしに誰でも利用可能**。
- 社内ツール・クローズドサービスとして運用する場合はリスクがある。

---

## 4. Webアプリケーション脆弱性診断

### 4-1. XSS（クロスサイトスクリプティング）

**評価**: ⚠️ 要改善

**調査結果**:

**問題箇所1: `templates/catalog.html:803`**
```jinja2
{{ pricelist.notes | safe }}
```
- `pricelist.notes` はユーザーが入力したテキストデータ（DBの `notes` カラム）。
- `| safe` フィルターにより **Jinja2の自動エスケープが無効化**されており、XSSのリスクがある。
- カタログページは **ログインなしで閲覧可能**（`catalog_view` は `@login_required` なし）なため、影響範囲が広い。

**問題箇所2: `templates/product_detail.html:16`**
```jinja2
<input type="hidden" name="image_urls_json" id="image_urls_json" value='{{ images|tojson|safe }}'>
```
- `tojson` フィルターはJSONエスケープを行うが、HTML属性内での出力に際してHTMLエスケープが必要。
- `tojson` は `<`, `>`, `&` を `\u003c` 等にエスケープするため実質的に安全だが、`| safe` の使用は慎重に検討すべき。

**Jinja2 自動エスケープ**: 他の `{{ variable }}` 出力はJinja2の自動エスケープが有効なため、概ね安全。

**推奨対応**:
- `pricelist.notes` は `| safe` を削除し、必要に応じて `| e` （明示エスケープ）か許可タグのサニタイズ（bleachライブラリ等）を使用する。

---

### 4-2. SQLインジェクション

**評価**: ✅ おおむね良好（軽微な指摘あり）

**調査結果**:
- ユーザー入力を伴うデータベース操作はすべて **SQLAlchemy ORM**（プレースホルダ相当）を使用。
- 以下のf-string SQL が存在するが、**いずれも外部入力由来ではない**（定数・ハードコードされたテーブル/カラム名）:

```python
# database.py:87 - ADDITIVE_STARTUP_MIGRATIONSの定数から生成
connection.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))

# services/database_migration.py:55 - _repo_table_orderで検証済みの名前のみ使用
connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"'))
```

- `database_migration.py` の `_normalize_table_names` では入力テーブル名をホワイトリスト検証しているため、実質的なリスクは低い。

---

### 4-3. OSコマンドインジェクション

**評価**: ✅ 良好

**調査結果**:
- Webルート（`routes/`）内で `os.system`、`subprocess`、`exec`、`shell=True` の使用なし。
- `scripts/arc1_kpi_probe.py` は `subprocess.run` を使用しているが、これは管理用CLIスクリプトであり、Webアプリ経由でアクセス不可。

---

### 4-4. ディレクトリトラバーサル

**評価**: ✅ 良好

**調査結果**:
- ファイル提供は `send_from_directory` を使用（Werkzeugの安全なファイル配信）。
- ショップロゴのアップロード処理に `secure_filename` を使用:
```python
# routes/shops.py:58-59
safe_name = secure_filename(file_storage.filename)
ext = os.path.splitext(safe_name)[1].lower()
```

---

## 5. 実装・運用時のセキュリティTips

### 5-1. エラーメッセージの秘匿

**評価**: ⚠️ 要改善

**調査結果**:

**問題箇所: `routes/auth.py:59`**
```python
except Exception as e:
    return render_template('register.html', error=f"Error: {e}")
```
- 登録処理での例外がそのままユーザーに返される。DBエラー詳細（テーブル名、制約名等）が露出する可能性がある。

**その他**:
- `debug=True` は `app.py` の `if __name__ == "__main__":` ブロックのみ（ローカル開発用）。本番はgunicornで起動するため直接の影響なし。
- `database.py:33` に `print(f"DEBUG: Using database URL: {debug_url}")` があるが、パスワードは `hide_password=True` でマスクされている。
- `rakuma_db.py` に `DEBUG:` プリント文と `traceback.print_exc()` が多数あるが、これはサーバーログ（stdout）への出力であり、ユーザーへの返却ではない。本番ではログレベル管理を推奨。

**推奨対応**:
```python
# routes/auth.py の例外ハンドリング改善
except Exception:
    # 詳細はログに記録、ユーザーには汎用メッセージのみ
    import logging
    logging.exception("Registration error")
    return render_template('register.html', error="登録処理中にエラーが発生しました")
```

---

### 5-2. POSTとGETの使い分け

**評価**: ✅ 良好

**調査結果**:
- パスワード送信はすべて `POST` フォーム（`/login`、`/register`）。
- GETリクエストに機密情報を含むパラメータは確認されなかった。

---

### 5-3. 依存ライブラリとDependabot

**評価**: ⚠️ 要改善

**調査結果**:
- `.github/dependabot.yml` が**存在しない**。自動的な脆弱性検出・パッチ通知が機能していない。
- `requirements.txt` はバージョン固定なし（例: `Flask`、`SQLAlchemy` など全て最新版指定）。これは自動的に最新を取得するが、意図しない破壊的変更のリスクもある。

**推奨対応**: `.github/dependabot.yml` を作成してPip依存関係の自動更新を設定する。

---

## 6. セキュリティチェックシート（結果一覧）

| 確認項目 | 期待される状態 | 重要度 | 判定 | 備考 |
|---|---|---|---|---|
| HTTPS常時接続 | HSTSが設定され、max-ageが半年以上 | 高 | ⚠️ | アプリ側にHSTSヘッダー未設定。RenderのHTTPS自体は有効 |
| DNS分離 | 権威・キャッシュサーバーが分離 | 中 | ℹ️ | Render/外部DNS側の確認が必要 |
| ポートの最小化 | 不要なポートが閉じている | 高 | ✅ | Renderインフラが制御、アプリは10000番のみ |
| Cookie属性 | Secure と HttpOnly あり | 高 | ⚠️ | SESSION_COOKIE_SECURE/HTTPONLY が未明示設定 |
| セッションの秘匿 | URLやRefererにセッションIDが露出しない | 高 | ✅ | 問題なし |
| エスケープ処理 | すべての動的出力でHTMLエスケープ | 高 | ⚠️ | `catalog.html` の `\| safe` によるXSSリスク |
| プレースホルダ | SQL発行にバインド変数使用 | 高 | ✅ | ORM使用で概ね安全 |
| OSコマンド回避 | シェル呼び出し関数の不使用 | 高 | ✅ | Webルート内で問題なし |
| エラー隠蔽 | 本番で詳細エラーメッセージを表示しない | 中 | ⚠️ | auth.py の `f"Error: {e}"` 露出 |
| 秘匿情報管理 | APIキー等をコードに含めず環境変数管理 | 高 | ⚠️ | SECRET_KEYのデフォルト値問題、`.env` が.gitignore未記載 |
| ブルートフォース対策 | ログイン試行制限あり | 高 | ❌ | 未実装 |
| CSRF対策 | フォームにCSRFトークン | 高 | ❌ | 未実装 |
| MFA/二段階認証 | OTP等の多要素認証 | 中 | ❌ | 未実装 |
| Dependabot | 依存ライブラリ自動更新 | 中 | ❌ | 設定ファイルなし |

---

## 7. 発見事項の優先度別まとめ

### 🔴 高優先度（早急な対応推奨）

1. **CSRF対策の未実装**: すべての状態変化操作（商品編集・削除・スクレイピング実行等）がCSRF攻撃に脆弱。`Flask-WTF` の導入を推奨。

2. **ブルートフォース保護なし**: `/login` エンドポイントへの試行回数制限なし。`flask-limiter` で1分あたりの試行回数を制限すること。

3. **XSS（catalog.html）**: `{{ pricelist.notes | safe }}` が公開カタログページ（ログイン不要）でユーザー入力をそのままHTMLとして出力。悪意あるスクリプトを含む `notes` が設定された場合、閲覧者全員が影響を受ける。

### 🟡 中優先度（計画的対応推奨）

4. **HSTSヘッダー未設定**: `after_request` でセキュリティヘッダーを付与する実装が必要。

5. **Cookie Secure/SameSite の明示設定**: Renderのプロキシ環境で確実に機能させるために明示的に設定する。

6. **エラー詳細の露出**: `routes/auth.py` の登録例外がユーザーに返される。

7. **SECRET_KEY のデフォルト値**: 環境変数未設定時に `"dev-secret-key-change-this"` が使用される。本番環境では必ず環境変数を設定すること（`render.yaml` では `sync: false` で手動設定が必要とされているが、設定漏れリスクがある）。

### 🟢 低優先度・情報提供

8. **Dependabot 未設定**: 自動的な脆弱性通知のために `.github/dependabot.yml` を追加する。

9. **登録エンドポイントの公開**: クローズドサービスとして運用する場合は招待制または管理者による承認フローを検討。

10. **`.env` が `.gitignore` 未記載**: `venv/`, `ENV/`, `env/` はあるが `.env` ファイル自体の除外設定がない。誤って `.env` を作成してコミットするリスクを防ぐため、`.gitignore` に `.env` を追記することを推奨。

11. **`scrape/status/<job_id>`（待機ページ）のIDOR**: 待機ページ自体は `@login_required` で保護されているが、他ユーザーのjob_idを知っていれば待機画面を表示できる。ただし実データへのアクセスはAPIが `user_id` で検証しているため機密データの漏洩はない。設計として許容範囲内だが、より厳格にするならHTMLルートでもjob所有者チェックを行うこと。

---

## 8. 既存の良い実装（評価点）

- **パスワードハッシュ化**: `werkzeug.security.generate_password_hash / check_password_hash` を使用（scrypt/bcrypt相当）。
- **SQLAlchemy ORM**: ユーザー入力のSQLクエリはすべてORMを通じてバインド変数で処理。
- **`@login_required` の網羅**: 認証が必要なルート（products, shops, export等）はほぼ全て保護されている。
- **所有権チェック**: products, shops, pricelists等のリソース操作は `user_id` によるアクセス制御が実施されている（例: `filter_by(id=product_id, user_id=current_user.id)`）。
- **`send_from_directory` + `secure_filename`**: ファイル配信・アップロードにWerkzeugの安全なAPIを使用。
- **ProxyFix設定**: `ProxyFix(app.wsgi_app, x_for=1, x_proto=1, ...)` でリバースプロキシ環境に対応。
- **render.yaml の秘密情報管理**: `SECRET_KEY` は `sync: false`（Renderダッシュボードで手動設定）で管理。
- **公開カタログはトークンベース**: カタログURLはランダムUUID（`PriceList.token`）によりアクセス制御。
- **IPアドレスのハッシュ化**: カタログアクセスログで生IPを保存せずSHA-256ハッシュを使用（プライバシー配慮）。

---

*このレポートはコード静的解析に基づくものであり、実際の動的テスト（ペネトレーションテスト等）による確認も合わせて実施することを推奨します。*
