# Playwright 移行 & キューシステム 仕様書 索引

> **⚠️ 統合計画ドキュメント**: このディレクトリの Stage 計画と `reports.md` の Phase 計画は
> **[docs/UNIFIED_ROADMAP.md](../UNIFIED_ROADMAP.md)** に統合されました。
> 新しい実装は統合ロードマップを参照してください。

> **注意**: このディレクトリの文書はリポジトリルートの `reports.md`（`/ESP/reports.md`）の「Phase」計画とは**別の独立した移行計画**です。
> `reports.md` では UI/機能開発の Phase が定義されており、本ディレクトリはスクレイピングエンジンの技術的移行を扱います。
> 混乱を避けるため、本計画では **「Stage」** という用語を使用します。

---

## 移行計画の概要

現在の ESP アプリは Selenium/Chrome によるスクレイピングに依存しており、以下の問題があります：

1. **メモリ問題**: Chrome 1インスタンス ≒ 400MB → 複数同時起動でOOMクラッシュ
2. **スケーラビリティ**: 20ユーザー同時接続不可（Render Standard: 2GB RAM）
3. **同期ブロッキング**: スクレイピング完了まで HTTP 接続を維持し続ける
4. **重いイメージ**: Docker イメージが ~1.5GB（Chrome 込み）

これを解決するため、以下の 5 Stage に分けて段階的に移行します。

---

## Stage 依存関係図

```
Stage 0: キューシステム構築
    │
    ▼
Stage 1: ラクマ Playwright 移行（Render互換性検証）
    │
    ▼
Stage 2: メルカリパトロール Playwright 移行
    │
    ▼
Stage 3: メルカリ全体 Playwright 移行
    │
    ▼
Stage 4: Selenium 完全削除 & クリーンアップ
```

各 Stage は前の Stage が完了していることを前提とします。
**Stage 1 のみ**: Render 本番環境での Playwright 動作確認（互換性検証）が最重要目標です。

---

## Stage サマリーテーブル

| Stage | 名称                              | 状態     | 主な変更ファイル                                          |
|-------|-----------------------------------|----------|-----------------------------------------------------------|
| 0     | キューシステム構築                | ✅ 完了  | `services/scrape_queue.py`（新規）<br>`routes/scrape.py`<br>`templates/scrape_form.html` |
| 1     | ラクマ Playwright 移行            | ✅ 完了  | `rakuma_db.py`<br>`services/patrol/rakuma_patrol.py`<br>`Dockerfile` |
| 2     | メルカリパトロール Playwright 移行 | ✅ 完了  | `services/patrol/mercari_patrol.py`<br>`services/monitor_service.py` |
| 3     | メルカリ全体 Playwright 移行      | ✅ 完了  | `mercari_db.py`（全書き換え）<br>`services/scrape_queue.py` |
| 4a    | Selenium 削除（パトロール層）     | ✅ 完了  | `services/patrol/*_patrol.py`（`_fetch_with_selenium` 削除）<br>`surugaya_db.py`（Selenium fallback → StealthyFetcher）<br>デバッグスクリプト削除 |
| 4b    | Selenium 削除（DB 層・Docker）    | 🔲 未着手 | `yahoo_db.py`, `yahuoku_db.py`, `snkrdunk_db.py`, `offmall_db.py`<br>`requirements.txt`, `Dockerfile` |

> **詳細な残タスクは [docs/UNIFIED_ROADMAP.md](../UNIFIED_ROADMAP.md) を参照してください。**

---

## ドキュメント一覧

| ファイル                                               | 内容                                      |
|--------------------------------------------------------|-------------------------------------------|
| [CURRENT_ARCHITECTURE.md](./CURRENT_ARCHITECTURE.md)  | 現在のコードベース構造リファレンス         |
| [STAGE_0_QUEUE_SYSTEM.md](./STAGE_0_QUEUE_SYSTEM.md)  | Stage 0: スクレイピングキューシステム仕様  |
| [STAGE_1_RAKUMA_PLAYWRIGHT.md](./STAGE_1_RAKUMA_PLAYWRIGHT.md) | Stage 1: ラクマ Playwright 移行仕様 |
| [STAGE_2_MERCARI_PATROL.md](./STAGE_2_MERCARI_PATROL.md) | Stage 2: メルカリパトロール移行仕様    |
| [STAGE_3_MERCARI_FULL.md](./STAGE_3_MERCARI_FULL.md)  | Stage 3: メルカリ全体移行仕様              |
| [STAGE_4_SELENIUM_REMOVAL.md](./STAGE_4_SELENIUM_REMOVAL.md) | Stage 4: Selenium 削除仕様          |

---

## 移行後の目標状態

- **メモリ使用量**: ~300MB（ベース）+ 10×5MB（HTTP）+ 2×150MB（Playwright）≒ ~650MB
  - Render Standard (2GB) で 20 ユーザー同時接続に余裕で対応
- **Docker イメージ**: ~1.5GB → ~800MB（Chrome 削除）
- **Playwright（StealthyFetcher）**: Bot 検知対策付きブラウザ自動化
- **キューシステム**: 公平なリクエスト処理、待機位置表示、非同期応答

---

## 移行における技術選択の根拠

### なぜ Scrapling StealthyFetcher（Playwright）なのか？

1. **すでに `scrapling` が requirements.txt に含まれている** → 追加依存なし
2. **StealthyFetcher は Bot 検知対策が強力** → メルカリ/ラクマの検知回避に有効
3. **Playwright は Selenium より軽量**（メモリ ~150MB vs ~400MB）
4. **非同期対応** → キューシステムとの親和性が高い

### なぜ Redis/Celery を使わないのか？

- Render Standard プランは Redis などの外部サービスが追加コストになる
- Python の `concurrent.futures.ThreadPoolExecutor` + `queue.Queue` で十分な機能が実現可能
- シンプルな実装でメンテナンスコストを下げる

---

## 各 Stage のエージェントへの指示

各 Stage の実装エージェントは以下の手順に従ってください：

1. **このREADMEを読む**
2. **CURRENT_ARCHITECTURE.md を読む**（現状の理解）
3. **対象 Stage の仕様書を読む**（実装詳細）
4. **前の Stage の結果ドキュメントを読む**（Stage 1以降）
5. **仕様書の実装手順に従って実装**
6. **テストを実行して検証**
7. **次の Stage への引き継ぎドキュメントを作成**
