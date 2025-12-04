# ベースイメージ
FROM python:3.9

# 1. Google Chrome Stableのインストール
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
ENV CHROME_BINARY_LOCATION=/usr/bin/google-chrome
ENV PORT=5000

# 6. 起動コマンド（★重要修正）
#    --workers 1 : 並列処理を1つにしてメモリ消費を抑える
#    --threads 8 : その代わりスレッドを使ってリクエストをさばく
#    --timeout 120 : 処理待ち時間を延ばす
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
