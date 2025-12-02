#!/usr/bin/env bash
# エラーが起きたら停止
set -o errexit

# 1. Pythonライブラリのインストール
pip install -r requirements.txt

# 2. Chromeのインストール設定
# インストール先ディレクトリを少し浅く変更します
CHROME_DIR=/opt/render/project/src/chrome-linux

# ディレクトリがない場合のみインストールを実行
if [[ ! -d "$CHROME_DIR" ]]; then
  echo "...Downloading Chrome"
  mkdir -p $CHROME_DIR
  
  # Chromeのダウンロード
  wget -P ./tmp https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  
  # 解凍 (chrome-linuxフォルダの中に opt/google/chrome... が展開される)
  dpkg -x ./tmp/google-chrome-stable_current_amd64.deb $CHROME_DIR
  
  # 掃除
  rm ./tmp/google-chrome-stable_current_amd64.deb
  
  # Chromeバイナリの実行権限を付与
  chmod +x $CHROME_DIR/opt/google/chrome/google-chrome
  
  echo "Chrome installed at: $CHROME_DIR/opt/google/chrome/google-chrome"
fi

# 3. 追加のシステムライブラリをインストール（Chrome起動エラー防止）
# Renderのユーザー権限でも動くように最低限のチェックのみ行う（エラーが出る場合は無視して進む設定）
# ※Renderの無料枠ではapt-getが自由に使えないことがありますが、
# 　Chromeの起動に必要なライブラリが不足している場合があるため、念のため記載します。
# 　もしデプロイ時に権限エラーが出るようならこのセクションは削除してください。