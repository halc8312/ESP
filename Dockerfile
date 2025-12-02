# ベースイメージ（Python 3.9 を使用）
FROM python:3.9-slim

# 1. 必要なシステムライブラリとChromeをインストール
#    (wget, gnupg, unzip などを入れ、Googleの公式リポジトリからChromeを入れる)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
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
#    Pythonコードがこのパスを見てChromeを使います
ENV CHROME_BINARY_LOCATION=/usr/bin/google-chrome
ENV PORT=5000

# 6. アプリの起動コマンド
CMD gunicorn app:app --bind 0.0.0.0:$PORT