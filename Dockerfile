# ベースイメージ
FROM python:3.9

# 1. Chromium と ChromeDriver をインストール
#    OS標準のパッケージを使うことで、ライブラリの不整合によるクラッシュを根絶します
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
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
#    Chromiumの場所を指定
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV PORT=5000

# 6. 起動コマンド
CMD gunicorn app:app --bind 0.0.0.0:$PORT
