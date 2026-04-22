# Render 分割デプロイの運用メモ

ESP は Render 上で 4 サービス構成(`esp-web` / `esp-worker` / `esp-keyvalue` / `esp-postgres`)。worker → web の内部 HTTP 通信まわりでハマりやすい点を記録する。

## 1. 内部ホスト名は `<slug>-<suffix>` 形式

- `render.yaml` 上のサービス名は `esp-web` / `esp-worker` などだが、**private network 上の実ホスト名は別**。
- 実体は例: `esp-1-kend`、`esp-web-ne5j` のようにサフィックスが付く。
- 権威値は **Render Dashboard の該当サービス → Connect → Internal** タブに表示される文字列。
- 素の `esp-web` は解決できない(`socket.gaierror: Name or service not known`)。

### 対処
- render.yaml では `fromService.property: host` で注入する(コードは `WEB_INTERNAL_HOST` env var 経由で参照)。
- Blueprint と実運用のスラッグがズレていて `fromService` が効かないときは、Dashboard で **手動で env var を貼る**のが確実。
  - esp-worker → Environment タブ → `WEB_INTERNAL_HOST` = `esp-1-kend`(実例)
  - 併せて `WEB_INTERNAL_PORT=8080` も入れる

## 2. ポート 10000 / 18012 / 18013 / 19099 は private network でブロック

- Render 公式ドキュメント: 公開 HTTPS 用の port 10000 を含む上記 4 ポートは **private network では到達できない**。
- つまり web service を `PORT=10000` で 1 本しか bind していないと、worker からの内部通信は永遠に届かない。
- 対処: Dockerfile の gunicorn を **dual-bind** する。
  ```Dockerfile
  CMD gunicorn ... --bind 0.0.0.0:${PORT:-10000} --bind 0.0.0.0:${INTERNAL_PORT:-8080} wsgi:app
  ```
- worker 側は `http://<internal-host>:8080` を叩く。

## 3. `sync: false` の env var は自動で揃わない

- render.yaml で `sync: false` な env var(HMAC 共有シークレット / SECRET_KEY など)は、Render が自動で同値を注入してくれない。
- esp-web と esp-worker で別々にランダム値が生成される、もしくは片方未設定、になりがち。
- 症状: worker → web の疎通は OK なのに `401 invalid_signature` が返る。
- 対処: **Dashboard で片方の値をコピーして、もう片方に貼り付ける**。
  - ESP では `BG_REMOVAL_INTERNAL_SECRET` と `SECRET_KEY` がこれに該当。
  - `BG_REMOVAL_INTERNAL_SECRET` が未設定だとコードは `SECRET_KEY` にフォールバックするため、最低限 `SECRET_KEY` だけでも一致していれば動く。

## 4. worker → web の URL 解決順序

`jobs/bg_removal_tasks.py::_resolve_web_base_url` の解決順は:

1. 明示的な URL: `ESP_WEB_INTERNAL_URL` / `WEB_INTERNAL_URL` / `WEB_PUBLIC_URL`
2. host + port 組み立て: `WEB_INTERNAL_HOST` + `WEB_INTERNAL_PORT`(default 8080)
3. fallback: `http://esp-web:8080`(Render 本番ではほぼ到達しない、ローカル/CI 向け)

**ハマりポイント**: 過去に手動で `WEB_INTERNAL_URL=http://esp-web:10000` などを入れたままだと、(1) が最優先で残り続けて 10000 ブロックを踏む。Dashboard で古い値は**消してから** `WEB_INTERNAL_HOST` を入れる。

## 5. Blueprint 変更の反映

- `render.yaml` の変更(特に `fromService` 追加)は GitHub push だけでは反映されないことがある。
- Dashboard の Blueprints から **Manual Sync / Apply Changes** を実行して env var を注入し直す。
- それでも反映されない場合は、render.yaml のサービス名と実スラッグを手動で一致させに行くか、(3) の要領で手動 env var を入れる。

## 6. 動作確認の早見表

| 症状 | 見るもの |
|---|---|
| `NameResolutionError: Failed to resolve 'esp-web'` | 内部ホスト名の注入漏れ → (1), (4) |
| 疎通 OK で `401 invalid_signature` | 共有シークレットのズレ → (3) |
| `Connection refused` / タイムアウト | dual-bind が効いていない → (2) |
| URL が `...:10000/...` になっている | 古い `WEB_INTERNAL_URL` が残っている → (4) |

## 7. 確認コマンド(ローカル / CI)

```bash
# worker / runtime まわりの変更を入れたとき
pytest tests/test_worker_entrypoint.py tests/test_worker_runtime.py -q

# bg-removal / 内部 HMAC の変更を入れたとき
pytest tests/test_bg_removal_routes.py tests/test_bg_remover_internals.py -q

# split-render の cutover 前チェック
flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict
```

## 8. トピック別の関連ファイル

- `Dockerfile` — dual-bind の gunicorn 起動行
- `render.yaml` — サービス定義 / `fromService` / env var
- `jobs/bg_removal_tasks.py::_resolve_web_base_url` — worker 側 URL 解決
- `services/bg_remover/internal_auth.py` — HMAC 署名生成 / 検証
- `routes/bg_removal.py::internal_upload_bg_result` — web 側 HMAC 検証エンドポイント
- `worker.py`, `services/worker_runtime.py` — worker エントリ
