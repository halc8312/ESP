#!/usr/bin/env bash
# エラーが起きたら停止
set -o errexit

# Pythonのライブラリをインストール
pip install -r requirements.txt

# Chromeのインストール先
STORAGE_DIR=/opt/render/project/src/opt/google/chrome

if [[ ! -d "$STORAGE_DIR" ]]; then
  echo "...Downloading Chrome"
  mkdir -p $STORAGE_DIR
  
  # Chromeをダウンロードして解凍
  wget -P ./tmp https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  dpkg -x ./tmp/google-chrome-stable_current_amd64.deb $STORAGE_DIR
  rm ./tmp/google-chrome-stable_current_amd64.deb
  
  # 実行権限などの調整
  cd $STORAGE_DIR/opt/google/chrome
  rm chrome-sandbox
  chmod 755 chrome
fi
