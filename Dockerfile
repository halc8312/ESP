# ベースイメージ（Python 3.9 slim）
FROM python:3.9-slim

# 1. 必要なツールとChromeをインストール
#    (apt-keyを使わず、直接.debファイルをダウンロードしてインストールする方法に変更)
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    gnupg \
    ca-certificates \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. 作業ディレクトリの設定
WORKDIR /app

# 3. Pythonライブラリのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. ソースコードをコピー
COPY . .

# 5. 環境変数の設定
#    Chromeのインストール先は通常 /usr/bin/google-chrome になります
ENV CHROME_BINARY_LOCATION=/usr/bin/google-chrome
ENV PORT=5000

# 6. アプリの起動コマンド
CMD gunicorn app:app --bind 0.0.0.0:$PORT
