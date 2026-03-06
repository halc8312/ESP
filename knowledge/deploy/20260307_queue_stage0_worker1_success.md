# Stage 0 キューシステム導入時のGunicornワーカー障害 — 解消記録

**ファイル**: `knowledge/deploy/20260307_queue_stage0_worker1_success.md`
**カテゴリ**: deploy / queue-system / gunicorn / worker
**ステータス**: ✅ 解消済み
**発生日**: 2026-03-07

---

## 概要

Stage 0（`scrape_queue` インメモリキューシステム）導入時、全ECサイトの1件目からスクレイピングが失敗する問題が発生した。
Gunicornのワーカー数を `--workers 1` に固定することで解消。

---

## 障害発生状況

- **環境**: Docker / Render 本番デプロイ環境
- **影響範囲**: 全ECサイト（Mercari, Rakuma, Yahoo, Surugaya 等）のスクレイピングジョブ全件
- **症状**:
  - スクレイピング結果がすべて `empty title` または `error` ステータスで返却される
  - ジョブのステータスポーリング（`/scrape/status/<job_id>`）でジョブが見つからない（404 相当）
  - ジョブIDは発行されているが、ワーカーがキュー内のジョブを認識できない
- **エラーログ例**:
  ```
  KeyError: 'job_id not found in queue'
  scrape result: empty title or error
  ```

---

## 調査経緯

1. **ワーカー数の確認**: Dockerfile/Render設定でGunicornが `--workers 2`（またはデフォルト値）で起動していることを確認
2. **キュー管理の調査**: `services/scrape_queue.py` の `get_queue()` シングルトン実装を確認
   - `get_queue()` はモジュールレベルのグローバル変数 `_queue_instance` を返す
   - 各Gunicornワーカープロセスは**独立したメモリ空間**を持つため、シングルトンがプロセスごとに独立して存在する
3. **タスク初期化の調査**: ジョブ登録リクエストとステータスポーリングリクエストが**別々のワーカープロセス**に振り分けられていることを確認
   - プロセスAでジョブを登録 → プロセスBにステータスポーリングが届く → プロセスBのキューにジョブが存在しない → 404/error
4. **executor / worker context 分離の確認**: `http_executor`, `browser_executor` もプロセスごとに独立して初期化されることを確認

---

## 根本原因

**Gunicorn `--workers > 1` 時のプロセス間インメモリ状態不一致**

```
[Gunicornワーカー構成]
    ┌──────────────────────┐    ┌──────────────────────┐
    │  Worker Process A    │    │  Worker Process B    │
    │  ─────────────────   │    │  ─────────────────   │
    │  _queue_instance     │    │  _queue_instance     │
    │    job_id=abc123     │    │    (空)              │
    │    status=running    │    │                      │
    └──────────────────────┘    └──────────────────────┘
           ↑                              ↑
    POST /scrape/run              GET /scrape/status/abc123
    → ジョブ登録成功              → ジョブが見つからない！
```

- `scrape_queue.py` の `ScrapeQueue` クラスはプロセス内シングルトン（`_queue_instance` グローバル変数）
- ジョブIDの登録・ステータス管理・結果格納はすべてプロセスのヒープメモリ上にのみ存在する
- Gunicornのデフォルト設定（`--workers > 1`）ではリクエストが複数ワーカーにラウンドロビンされる
- 登録リクエストと参照リクエストが別ワーカーに届くと、参照側のキューにジョブが存在しない

---

## 解消対策

### 即時対応: Gunicorn起動オプションの修正

**Dockerfile（またはRender設定）のCMDを以下のように変更:**

```dockerfile
# 変更前（問題あり）
CMD ["gunicorn", "--workers", "2", "app:app"]

# 変更後（正常）
CMD ["gunicorn", "--workers", "1", "--max-requests", "0", "app:app"]
```

**オプション説明:**

| オプション             | 値  | 理由                                                                 |
|------------------------|-----|----------------------------------------------------------------------|
| `--workers`            | `1` | プロセスを1つに限定し、インメモリキューを全リクエストで共有する       |
| `--max-requests`       | `0` | ワーカー自動再起動を無効化（再起動するとキュー状態が失われる）         |

### Renderデプロイへの適用

Render の Start Command にも同様のオプションを明示:

```
gunicorn --workers 1 --max-requests 0 --bind 0.0.0.0:$PORT app:app
```

---

## 検証結果

- `--workers 1` 適用後、全ECサイトの1件目からスクレイピングが正常に完了
- ジョブのステータスポーリングが正しいステータスを返却
- 結果取得 (`/scrape/result/<job_id>`) も正常動作

---

## 再発防止・今後の注意点

### Stage 0（現在）運用ルール

1. **Gunicornは必ず `--workers 1 --max-requests 0` で起動する**
2. Dockerfileや起動スクリプトを変更する際は、このオプションを削除・変更しないこと
3. APSchedulerのfcntl.flockによるシングルワーカーガードも同様の理由で存在する（`app.py`参照）

### Stage 1以降（スケールアップ時）の必須要件

| 要件                   | 内容                                                                       |
|------------------------|----------------------------------------------------------------------------|
| 状態永続化             | ジョブ状態をDB（PostgreSQL等）またはRedisに移行する                        |
| キューブローカー        | Redis Queue / Celery / RQ等のプロセス横断キューシステムを採用する           |
| ジョブID帰属検証        | `/scrape/status/<job_id>` でジョブが `current_user` に属するか検証する     |
| ワーカーコンテキスト    | 各ワーカーが独立して起動・初期化できる設計にする                           |
| 初期化の冪等性          | ワーカー再起動・スケールアップ時にジョブ状態が消失しない設計にする         |

---

## 関連ファイル

| ファイル                    | 内容                                               |
|-----------------------------|----------------------------------------------------|
| `services/scrape_queue.py`  | インメモリキューシングルトン実装（`get_queue()`）   |
| `Dockerfile`                | Gunicorn起動コマンド（`CMD`行）                    |
| `app.py`                    | APScheduler の fcntl.flock ガード実装              |
| `routes/scrape.py`          | ジョブ登録・ステータス・結果取得エンドポイント     |
| `routes/api.py`             | APIエンドポイント（`/api/scrape/status/<job_id>`） |

---

## 参考: 現在のアーキテクチャ概要（Stage 0）

```
[クライアント]
    │
    ▼
[Gunicorn: worker=1]
    │
    ├── routes/scrape.py (POST /scrape/run)
    │       └── services/scrape_queue.py::get_queue().submit_job()
    │               └── _queue_instance (プロセス内シングルトン)
    │                       ├── http_executor (ThreadPoolExecutor, max=10)
    │                       └── browser_executor (ThreadPoolExecutor, max=2)
    │
    └── routes/scrape.py (GET /scrape/status/<job_id>)
            └── services/scrape_queue.py::get_queue().get_status(job_id)
                    └── 同じ _queue_instance を参照 → 正常動作
```

**worker=1 であれば全リクエストが同一プロセスのキューを参照するため、ジョブIDの整合性が保たれる。**
