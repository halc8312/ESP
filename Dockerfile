# ベースイメージ: Python 3.11 (Debian BookwormベースのSlim版)
# Alpine Linuxはglibc非互換の問題が多いため、SeleniumにはDebian/Ubuntu系が推奨される
FROM python:3.11-slim

# Pythonのバッファリングを無効化（ログを即時出力）
ENV PYTHONUNBUFFERED=1
#.pycファイルの生成を抑制
ENV PYTHONDONTWRITEBYTECODE=1

# --- システム依存関係のインストール ---
# 1. wget, gnupg, unzip: Chrome/Driverのダウンロード用
# 2. Chromeの依存ライブラリ群: 最新Debian Trixieに存在するもののみ
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    libxss1 \
    fonts-liberation \
    libasound2 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    xdg-utils \
    libgbm1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- Google Chromeのインストール ---
# 公式リポジトリを追加してapt-getでインストール
# これにより、将来的な依存関係の変更もaptが自動解決してくれる
RUN set -eux \
    && mkdir -p /usr/share/keyrings \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor --yes -o /usr/share/keyrings/google-linux-signing-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-signing-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# --- アプリケーションのセットアップ ---
WORKDIR /app

# 依存関係ファイルのコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright / Patchright ブラウザのインストール
# PLAYWRIGHT_BROWSERS_PATH / PATCHRIGHT_BROWSERS_PATH を同じ共有ディレクトリに設定し、
# root でインストールしたブラウザを非 root ユーザー（myuser）でも参照できるようにする。
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV PATCHRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN scrapling install \
    && patchright install chromium \
    && chmod -R 755 /opt/ms-playwright

# ソースコードのコピー
COPY . .

# --- セキュリティ対策: 非rootユーザーの作成 ---
# Chromeはrootでの実行を嫌うため、専用ユーザーを作成
RUN useradd -m myuser
USER myuser

# アプリケーション起動コマンド
# Gunicornを使ってFlaskアプリ(app.pyの中のapp)を起動
# --worker-class gthread: スレッドベースワーカー（スクレイピング中もヘルスチェックに応答可能）
# --workers 1: ★Stage 0 必須★ ScrapeQueue はプロセス内のインメモリシングルトン。
#              workers > 1 にすると job_id を作ったプロセスと status をポーリングする
#              プロセスが異なる場合があり、「Job not found (404)」が返って
#              待機ページが即エラー表示になる。Stage 1以降でRedis/DB永続化後に増やすこと。
# --threads 8: 1ワーカーで同時8リクエストを処理（workers=2 の合計8スレッドと同等）
# --max-requests 0: ワーカー自動再起動を無効化。
#                   Stage 0 のスクレイピングジョブはバックグラウンドデーモンスレッドで実行される。
#                   --max-requests によるワーカー再起動はデーモンスレッドを強制終了し、
#                   実行中ジョブが全滅するため Stage 0 では 0（無効）にする。
# --timeout 600: スクレイピングが10分かかってもタイムアウトしないように設定
# --bind: Renderの$PORT環境変数にバインド
CMD gunicorn --worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 --bind 0.0.0.0:${PORT:-10000} app:app
