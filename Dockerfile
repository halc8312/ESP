# ベースイメージ
FROM python:3.9

# 1. 必要なパッケージとChromeのインストール
# Chromeのヘッドレス起動に必要なライブラリ(libgbm1等)と日本語フォントを追加
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    gnupg \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    fonts-kacst \
    fonts-freefont-ttf \
    libxss1 \
    libappindicator1 \
    libindicator7 \
    libgbm1 \
    libnss3 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
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

# 6. 起動コマンド
# メモリ節約のためworkerは1つ、スレッド並列で処理
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
