# ベースイメージ: Python 3.9 (Debian BusterベースのSlim版)
# Alpine Linuxはglibc非互換の問題が多いため、SeleniumにはDebian/Ubuntu系が推奨される
FROM python:3.9-slim-buster

# Pythonのバッファリングを無効化（ログを即時出力）
ENV PYTHONUNBUFFERED=1
#.pycファイルの生成を抑制
ENV PYTHONDONTWRITEBYTECODE=1

# --- システム依存関係のインストール ---
# 1. wget, gnupg, unzip: Chrome/Driverのダウンロード用
# 2. Chromeの依存ライブラリ群: ここが最も重要。
#    libnss3, libgconf-2-4, libfontconfig1 など、Renderネイティブ環境に不足しがちなものを網羅
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    libxss1 \
    libappindicator1 \
    libgconf-2-4 \
    fonts-liberation \
    libasound2 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    xdg-utils \
    libgbm1 \
    && rm -rf /var/lib/apt/lists/*

# --- Google Chromeのインストール ---
# 公式リポジトリを追加してapt-getでインストール
# これにより、将来的な依存関係の変更もaptが自動解決してくれる
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable

# --- アプリケーションのセットアップ ---
WORKDIR /app

# 依存関係ファイルのコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY ..

# --- セキュリティ対策: 非rootユーザーの作成 ---
# Chromeはrootでの実行を嫌うため、専用ユーザーを作成
RUN useradd -m myuser
USER myuser

# アプリケーション起動コマンド
# ポートはRenderが環境変数PORTで指定する場合があるが、Dockerfile内で明示も可能
CMD ["python", "mercari_db.py"]
