# ベースイメージ: Python 3.11 (Debian BookwormベースのSlim版)
# Playwright / Patchright 用の依存パッケージのみをインストールする
FROM python:3.11-slim

# Pythonのバッファリングを無効化（ログを即時出力）
ENV PYTHONUNBUFFERED=1
# .pycファイルの生成を抑制
ENV PYTHONDONTWRITEBYTECODE=1

# Playwright / Patchright の実行に必要なシステム依存関係
# --no-install-recommends で推奨パッケージを除外しイメージサイズを削減
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright / Patchright ブラウザを root で共有インストールし、非 root ユーザーでも参照可能にする
# インストール後にキャッシュを削除してレイヤーサイズを削減
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV PATCHRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN scrapling install \
    && patchright install chromium \
    && chmod -R 755 /opt/ms-playwright \
    && rm -rf /root/.cache

COPY . .

RUN useradd -m myuser
USER myuser

# ScrapeQueue はプロセス内シングルトンのため worker は 1 を維持する
CMD gunicorn --worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 --bind 0.0.0.0:${PORT:-10000} wsgi:app
