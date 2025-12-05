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

# ソースコードのコピー
COPY . .

# --- セキュリティ対策: 非rootユーザーの作成 ---
# Chromeはrootでの実行を嫌うため、専用ユーザーを作成
RUN useradd -m myuser
USER myuser

# アプリケーション起動コマンド
# Gunicornを使ってFlaskアプリ(app.pyの中のapp)を起動
# --timeout 300: 処理が5分までかかってもタイムアウトしないように設定
CMD ["gunicorn", "--timeout", "300", "app:app"]
