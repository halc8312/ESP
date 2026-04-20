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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright / Patchright ブラウザを root で共有インストールし、非 root ユーザーでも参照可能にする
# Chromium のみインストールし、不要なブラウザは除外
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV PATCHRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN scrapling install \
    && patchright install chromium \
    && chmod -R 755 /opt/ms-playwright \
    && rm -rf /root/.cache /tmp/*

# Bake the Argos Translate ja->en model into the image so the worker does
# not have to download ~200MB on first use. The model is also pre-loaded
# into the shared /opt/argos cache so non-root users can read it.
ENV ARGOS_PACKAGES_DIR=/opt/argos/packages \
    ARGOS_TRANSLATE_PACKAGE_DIR=/opt/argos/translate \
    ARGOS_PACKAGE_INDEX_CACHE=/opt/argos/index

# Copy the preload helper first so we can bake the model without having to
# invalidate the requirements layer when application code changes.
COPY services/translator/preload.py /tmp/argos_preload.py
RUN mkdir -p "$ARGOS_PACKAGES_DIR" "$ARGOS_TRANSLATE_PACKAGE_DIR" "$ARGOS_PACKAGE_INDEX_CACHE" \
    && TRANSLATOR_PRELOAD_SOURCE_LANG=ja TRANSLATOR_PRELOAD_TARGET_LANG=en \
        python /tmp/argos_preload.py \
    && chmod -R 755 /opt/argos \
    && rm -f /tmp/argos_preload.py

# Bake the rembg u2netp background-removal model into the image so the
# worker never has to download it on first use. u2netp is ~4.7MB on disk
# and ~250MB resident — comfortably fits alongside the Playwright browser
# pool on the current esp-worker standard plan.
ENV U2NET_HOME=/opt/rembg
COPY services/bg_remover/preload.py /tmp/rembg_preload.py
RUN mkdir -p "$U2NET_HOME" \
    && BG_REMOVAL_PRELOAD_MODEL=u2netp \
        python /tmp/rembg_preload.py \
    && chmod -R 755 /opt/rembg \
    && rm -f /tmp/rembg_preload.py

COPY . .

RUN useradd -m myuser
USER myuser

# ScrapeQueue はプロセス内シングルトンのため worker は 1 を維持する
# シェル形式を使用して ${PORT} 変数を展開する
CMD gunicorn --worker-class gthread --workers 1 --threads 8 --max-requests 0 --timeout 600 --bind 0.0.0.0:${PORT:-10000} wsgi:app
