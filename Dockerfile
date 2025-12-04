# ベースイメージ（安定版）
FROM python:3.9

# 1. 必要なツールとGoogle Chrome Stableをインストール
#    apt-get install ./...deb を使うことで、依存ライブラリも自動解決させます
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    gnupg \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. 作業ディレクトリ
WORKDIR /app

# 3. Pythonライブラリ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. ソースコード
COPY . .

# 5. 環境変数
#    Google Chromeの標準パス
ENV CHROME_BINARY_LOCATION=/usr/bin/google-chrome
ENV PORT=5000

# 6. 起動コマンド
CMD gunicorn app:app --bind 0.0.0.0:$PORT
