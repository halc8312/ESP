# Stage 4: Selenium 削除 & クリーンアップ仕様書

## 読むべきドキュメント

1. [CURRENT_ARCHITECTURE.md](./CURRENT_ARCHITECTURE.md) — 現在のコードベース構造
2. [STAGE_3_MERCARI_FULL.md](./STAGE_3_MERCARI_FULL.md) — Stage 3 メルカリ全体移行仕様
3. **`docs/specs/STAGE_3_RESULTS.md`**（Stage 3 実施後に作成） ← **必読**

---

## 前提条件

- **Stage 3 完了済み**: `mercari_db.py` が完全に Playwright に移行されている
- **全サイトが Playwright/HTTP で動作している**: Selenium を使用しているコードが残っていないこと
- **全テストが通過している**: Selenium モックを使用したテストも Playwright モックに更新済み

---

## 目標

プロジェクトから **Selenium / Chrome / ChromeDriver** に関する全ての依存を削除する。

**期待される効果**:

| 項目                  | 削除前    | 削除後    |
|-----------------------|-----------|-----------|
| Docker イメージサイズ | ~1.5GB    | ~800MB    |
| requirements.txt 行数 | +3行（削除）| -         |
| ビルド時間            | 長い      | 短い      |
| Render へのデプロイ時間 | 長い    | 短い      |

---

## 削除・変更対象

### 1. `Dockerfile`（変更）

**削除するブロック**:

```dockerfile
# ← 以下のブロック全体を削除
# --- Google Chromeのインストール ---
RUN set -eux \
    && mkdir -p /usr/share/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor --yes -o /usr/share/keyrings/google-linux-signing-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-signing-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*
```

**削除する apt パッケージ**（Chrome 専用のもの）:

以下の apt パッケージは Chrome のみが必要としており、Playwright には不要なため削除：
```dockerfile
# 以下を apt-get install から削除（Chrome のインストール用途のみ）:
#   wget   - Chrome リポジトリのキー取得に使用
#   gnupg  - Chrome リポジトリのキー検証に使用
#   unzip  - ChromeDriver の解凍に使用
# 注意: curl は Playwright でも必要な場合があるため確認してから削除
```

> ⚠️ **注意**: `libxss1`, `fonts-liberation`, `libasound2`, `libnspr4`, `libnss3` などは
> Playwright の Chromium でも必要な場合があります。
> Stage 1 の `STAGE_1_RESULTS.md` に記載された Playwright の依存関係を確認してから削除してください。

**削除後の Dockerfile イメージ**:

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Playwright/Chromium に必要なシステム依存パッケージのみ残す
RUN apt-get update && apt-get install -y \
    curl \
    libnss3 \
    libxss1 \
    fonts-liberation \
    libasound2 \
    libgbm1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
    # wget, gnupg, unzip: Chrome削除後は不要のため削除

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright ブラウザのインストール（Stage 1 で追加済み）
RUN python -m scrapling install

COPY . .

RUN useradd -m myuser
USER myuser

CMD gunicorn --worker-class gthread --workers 2 --threads 4 \
    --max-requests 100 --max-requests-jitter 20 \
    --timeout 600 --bind 0.0.0.0:${PORT:-10000} app:app
```

> **Chrome 削除後のコメント更新**: Dockerfile 冒頭のコメントを更新すること：
> ```dockerfile
> # ベースイメージ: Python 3.11 (Debian Bookworm ベースのSlim版)
> # Playwright Chromium を使用するため、必要な依存パッケージのみインストール
> ```

---

### 2. `requirements.txt`（変更）

**削除する行**:

```
selenium          ← 削除
webdriver-manager ← 削除
undetected-chromedriver ← 削除
```

**削除後の requirements.txt**:

```
Flask
SQLAlchemy
requests
pandas
gunicorn
Flask-Login
pytest
pytest-flask
Flask-APScheduler
beautifulsoup4
curl_cffi
scrapling
playwright         ← scrapling がPlaywrightを依存として持っているが、明示的に追加することも検討
```

> `scrapling` は内部で `playwright` を使用するが、`python -m scrapling install` で
> Playwright ブラウザをインストールするため、`playwright` を直接 requirements.txt に
> 記載しなくても動作する。ただし明示的に管理したい場合は追加すること。

---

### 3. `mercari_db.py`（確認・クリーンアップ）

Stage 3 で既に削除されているはずだが、以下が残っていないか確認：

```python
# 以下が残っていれば削除
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def _get_chrome_version(): ...  # 削除済みのはず
def create_driver(headless: bool = True): ...  # 削除済みのはず
```

確認コマンド:
```bash
grep -n "selenium\|create_driver\|webdriver" mercari_db.py
```

---

### 4. `surugaya_db.py`（変更）

`surugaya_db.py` には Selenium のフォールバック実装 `_fetch_soup_with_selenium()` が存在する：

```python
# surugaya_db.py の ~702行付近
def _fetch_soup_with_selenium(url: str, headless: bool = True, wait_seconds: int = 20):
    """Fallback HTML fetch via Selenium (Render-safe fallback path)."""
    driver = None
    try:
        from mercari_db import create_driver  # ← Stage 4 で削除される
        from selenium.webdriver.common.by import By
        # ...
```

**対処方針**:

1. `_fetch_soup_with_selenium()` を Playwright（StealthyFetcher）版に書き換える
2. または、駿河屋は HTTP only（Scrapling Fetcher）で問題なく動作している場合は、
   Selenium フォールバック自体を削除する

```python
# 削除または書き換え後
def _fetch_soup_with_playwright(url: str, headless: bool = True):
    """
    Playwright（StealthyFetcher）を使ったフォールバック HTML 取得。
    """
    from scrapling import StealthyFetcher
    try:
        page = StealthyFetcher.fetch(url, headless=headless, network_idle=True)
        from bs4 import BeautifulSoup
        return BeautifulSoup(page.html, "html.parser"), url, None
    except Exception as e:
        return None, url, str(e)
```

---

### 5. `services/patrol/yahoo_patrol.py`（変更）

`_fetch_with_selenium()` メソッドがある場合は削除または Playwright に置き換え：

```python
# yahoo_patrol.py の ~69行付近
def _fetch_with_selenium(self, url: str, driver) -> PatrolResult:
    """Selenium-based fetch using a shared driver."""
    # ← このメソッド全体を削除
```

このメソッドが `fetch()` から呼ばれているか確認し、Scrapling HTTP 版のみを使用するよう変更。

---

### 6. デバッグスクリプトの削除

以下のデバッグスクリプトを削除する：

```
debug_scrape.py        ← Selenium を使用するデバッグ用スクリプト
debug_children.py      ← DOM 探索用デバッグスクリプト
debug_variant_json.py  ← バリエーション取得デバッグスクリプト
```

削除前に各ファイルの内容を確認し、移行後も必要なロジックがないことを確認すること。

```bash
# 削除コマンド
git rm debug_scrape.py debug_children.py debug_variant_json.py
```

---

### 7. `services/monitor_service.py`（確認）

Stage 2 で以下が変更されているはず：

```python
# Stage 2 完了後の状態（確認すること）
_BROWSER_SITES = frozenset()  # 空集合

# check_stale_products() 内に以下のコードがないことを確認
# if product.site in _BROWSER_SITES:
#     if driver is None:
#         from mercari_db import create_driver  # ← なくなっているはず
```

---

## 段階的な削除手順

Stage 4 は以下の順序で実施すること（依存関係による）：

### Step 1: 全ての Selenium インポートを確認

```bash
# プロジェクト全体で Selenium を使っているファイルを確認
grep -rn "selenium\|create_driver\|webdriver" \
    --include="*.py" \
    --exclude-dir=".git" \
    /path/to/project
```

全ての結果が解決済みであることを確認してから次のステップへ。

### Step 2: requirements.txt を変更

```bash
# selenium, webdriver-manager, undetected-chromedriver を削除
```

### Step 3: テストを実行して動作確認

```bash
pytest tests/ -v
```

全テストが通過することを確認。

### Step 4: ローカルでの Docker ビルド

```bash
docker build -t esp-test .
docker run --rm esp-test python -c "import scrapling; print('OK')"
```

### Step 5: Dockerfile から Chrome インストールを削除

Chrome インストールブロックを削除し、再ビルド：

```bash
docker build -t esp-final .
```

イメージサイズを確認：
```bash
docker images esp-final
```
目標: ~800MB（従来 ~1.5GB から半減）

### Step 6: デバッグスクリプトの削除

```bash
git rm debug_scrape.py debug_children.py debug_variant_json.py
```

---

## 検証チェックリスト

Stage 4 完了の確認：

### コード検証
- [ ] `grep -rn "selenium" --include="*.py"` の結果が 0 件
- [ ] `grep -rn "create_driver" --include="*.py"` の結果が 0 件
- [ ] `grep -rn "webdriver_manager\|undetected_chromedriver" --include="*.py"` の結果が 0 件
- [ ] `requirements.txt` に `selenium`, `webdriver-manager`, `undetected-chromedriver` が含まれていない

### 機能検証
- [ ] メルカリ商品の取得が成功する
- [ ] メルカリ検索結果の取得が成功する
- [ ] ラクマ商品の取得が成功する
- [ ] Yahoo/ヤフオク/駿河屋/オフモール/SNKRDUNK が HTTP で正常動作する
- [ ] パトロール（15分間隔の価格監視）が正常動作する

### Docker 検証
- [ ] `docker build` が成功する（Chrome なし）
- [ ] Docker イメージサイズが ~800MB 以下（`docker images` で確認）
- [ ] `docker run` でアプリが起動する

### Render 環境検証
- [ ] Render へのデプロイが成功する
- [ ] 全サイトのスクレイピングが本番で動作する
- [ ] メモリ使用量が正常範囲内（Render ダッシュボードで確認）

### テスト
- [ ] `pytest tests/ -v` が全て通過する

---

## 移行完了レポート: `docs/specs/MIGRATION_COMPLETE.md`

Stage 4 完了後、以下のテンプレートで移行完了レポートを作成すること：

```markdown
# Playwright 移行完了レポート

## 完了日: YYYY-MM-DD

## 各 Stage の完了状況

| Stage | 完了日     | 実施者        |
|-------|------------|---------------|
| 0     | YYYY-MM-DD | [Agent/担当者] |
| 1     | YYYY-MM-DD | [Agent/担当者] |
| 2     | YYYY-MM-DD | [Agent/担当者] |
| 3     | YYYY-MM-DD | [Agent/担当者] |
| 4     | YYYY-MM-DD | [Agent/担当者] |

## 達成されたメトリクス

| 項目                    | 移行前    | 移行後    |
|-------------------------|-----------|-----------|
| Docker イメージサイズ   | ~1.5GB    | [実測値]  |
| ブラウザメモリ（1インスタンス）| ~400MB | [実測値] |
| 同時スクレイピング上限  | ~5件      | 20件      |
| テスト通過数            | [件数]    | [件数]    |

## 発見された問題と対処法

[各 Stage で発生した問題と解決策を記載]

## 今後の推奨事項

[将来の改善点があれば記載]
```

---

## 次の Agent へのメッセージ

この仕様書を読んでいるあなたが Stage 4 の担当者です。

**おめでとうございます！** Stage 4 が完了すれば、ESP プロジェクトの Playwright 移行が
全て完了します。

最後に確認しておくこと：

1. **`MIGRATION_COMPLETE.md` を作成する** — 移行の記録として残す
2. **`docs/specs/` ディレクトリ内の全 RESULTS.md を確認** — 各 Stage の実施記録が揃っているか
3. **本番環境での最終動作確認** — 全サイトが正常にスクレイピングできることを確認

移行作業、お疲れ様でした！🎉
