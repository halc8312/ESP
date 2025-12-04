# ★変更: 軽量版(slim)だとライブラリ不足でクラッシュしやすいため、
#         安定している通常版(python:3.9)を使用します。
FROM python:3.9

# 1. 必要なツールとChromeをインストール
#    .debファイルを直接 apt-get install することで、
#    Chromeに必要な依存ライブラリ(フォントや映像処理系)を自動で全て入れてくれます。
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

# 2. 作業ディレクトリの設定
WORKDIR /app

# 3. Pythonライブラリのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. ソースコードをコピー
COPY . .

# 5. 環境変数の設定
#    apt-getでインストールした場合、通常はこのパスになります
ENV CHROME_BINARY_LOCATION=/usr/bin/google-chrome
ENV PORT=5000

# 6. アプリの起動コマンド
CMD gunicorn app:app --bind 0.0.0.0:$PORT
