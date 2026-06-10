# 抽出画面 修正方針・詳細タスクリスト（クライアントDM対応）

作成日: 2026-06-10 / 対象リポジトリ: halc8312/ESP

## 0. 結論サマリ

| 要望 | 実現可能性 | 規模感 |
|---|---|---|
| ① 絞り込み検索結果URLからの抽出 | **可能**（既存の検索スクレイパーをほぼ流用できる） | 中 |
| ② 抽出結果の×削除 + 登録先選択（商品一覧 / 顧客用リストのみ） | **可能**（選択登録UIは既に存在。×削除と「リストのみ登録」が追加分） | 中 |
| ③ 登録時に英訳 + 利益上乗せを同時実行 | **可能**（翻訳パイプライン・価格ルールは実装済み。登録時フックの追加） | 中〜大 |

技術的に「明らかに不可能」な項目はありません。ただし後述の制約（特に ①のサイト別URL対応と ③の翻訳の即時性・自動適用ポリシー）に注意が必要です。

---

## 1. 現状実装の確認結果

- 抽出画面: `routes/scrape.py` + `templates/scrape_form.html` + `static/js/scrape_form.js`
  - 「URLから抽出」タブ … **商品単品URLのみ**（`scrape_single_item` に振り分け、limit=1固定）
  - 「検索して抽出」タブ … キーワード・価格min/max等からアプリ側でURLを組み立てて `scrape_search_result` 実行
  - プレビュー → チェックボックスで選択 → `/scrape/register-selected` で商品一覧に登録、という選択登録フローは**既に存在**
- サイト判定: `services/scrape_request.py` の `detect_site_from_url`（7サイト対応済み）
- 検索スクレイパー: 各 `*_db.py` の `scrape_search_result(search_url=...)` は**任意の検索URLを引数で受け取れる設計**になっており、①の土台はほぼ揃っている
- 価格表（顧客用リスト）: `models.py` の `PriceList` / `PriceListItem`、`routes/pricelist.py`（既存商品をリストに追加するAPIあり）。ただし現状 `PriceListItem` は **products テーブルの商品を参照する構造**
- 翻訳: `jobs/translation_tasks.py` + `services/translator/`（Argos / OpenAIバックエンド、`TranslationSuggestion` に提案を保存し、**operatorが手動で適用**する設計）
- 利益上乗せ: `services/pricing_service.py`（`PricingRule`: `(仕入値+送料)×(1+利益率%)+固定費`）。ただし**新規登録時に価格ルールを自動割当する処理は現状なし**（ルールが割り当て済みの商品のみ再計算）

---

## 2. 修正方針

### ① 絞り込みURL（検索結果URL）からの抽出

方針: 「URLから抽出」タブを拡張し、貼り付けられたURLが**商品ページか検索結果ページかを自動判定**して振り分ける。検索欄（キーワードタブ）は当面残し、利用状況を見て削除を判断（削除は容易なので後回しでリスクなし）。

- URL種別判定ロジックを `services/scrape_request.py` に追加
  - 例: メルカリ `jp.mercari.com/item/...`=単品 / `jp.mercari.com/search?...`=検索結果
  - ヤフオク `page.auctions.yahoo.co.jp/jp/auction/...`=単品 / `auctions.yahoo.co.jp/search/...`=検索結果 など7サイト分
- 検索結果URLの場合は、ユーザーURLをそのまま `scrape_search_result(search_url=...)` に渡す（件数上限の入力欄をURLタブにも追加）
- 既存の除外フィルタ・価格フィルタはそのまま適用

制約・注意:
- サイト側のURL形式変更には追従が必要（既存の self-healing と同様の保守前提)
- メルカリはSPAのネットワーク応答横取り方式のため、貼り付けURLのパラメータ（ソート・絞り込み）は基本そのまま有効だが、**全絞り込み条件の完全再現は保証できない**（サイトごとに動作確認が必要）
- ログイン必須の絞り込み（例: スニダンのサイズ別等の一部条件）はbot検知の影響を受ける可能性あり → fail-closed で「取得できなかった」ことを明示する

### ② 抽出結果の選別（×削除）+ 登録先選択

方針: 既存のプレビュー選択UI（`scrape_form.js`）を拡張。

- 各サムネイルカードに「×」ボタンを追加し、クライアント側で候補から除外（サーバー処理不要）
- 登録ボタンを「商品一覧に登録」「商品リストにのみ登録」の2択に変更
  - 「商品リストにのみ登録」: 登録先 `PriceList` を選択するドロップダウン（既存リスト選択 + その場で新規作成）
- データモデル方針（要決定 / 推奨は案A）:
  - **案A（推奨）**: 商品は products に登録するが `is_listed=False`（商品一覧に表示しない一時フラグ）を追加し、PriceListItem から参照。一覧画面はこのフラグで除外。既存のパトロール・翻訳・価格計算インフラをそのまま使え、公開カタログの隔離(`source_url`非公開)も既存実装を踏襲できる
  - 案B: PriceListItem にスナップショットを直接保存（products非依存）。翻訳・価格・在庫監視を別実装する必要があり非推奨

### ③ 登録時に英訳 + 利益上乗せを同時実行

方針: `/scrape/register-selected`（および新設のリスト登録API）に登録オプションを追加。

- 利益上乗せ: 登録リクエストに `pricing_rule_id`（または「デフォルトルールを適用」チェック）を含め、保存時に `pricing_rule_id` を割当て → `update_product_selling_price` を即時実行。**同期処理で確実に可能**
- 英訳: 登録完了時に選択商品ぶんの翻訳ジョブ（scope=full）を自動enqueueし、**完了したら自動適用する `auto_apply` フラグ**を `TranslationSuggestion` に追加
  - 現設計は「提案→手動適用」だが、DMの「5分でリストを作って見せたい」用途には自動適用が必須
  - UI側はリスト編集画面で翻訳進捗（n/m件完了）をポーリング表示し、完了次第カタログに英語タイトルが出る
- ユーザー設定に「デフォルト価格ルール」「翻訳バックエンド/自動適用ON・OFF」を追加

制約・注意:
- 翻訳は非同期（worker処理）のため「登録ボタンを押した瞬間に英訳完了」ではなく**数十秒〜数分のタイムラグ**が発生する（OpenAIバックエンドなら商品数件で1分以内が目安）。「5分で商品リスト完成」という要件は満たせる見込み
- OpenAI利用時はAPIコストが発生。Argos（ローカル）は無料だが品質・速度が落ちる

---

## 3. 詳細タスクリスト（実装順）

### フェーズ1: ① 検索結果URL抽出（土台）
1. `scrape_request.py` に `classify_target_url(url) -> ("item"|"search", site)` を追加（7サイト分のURLパターン定義 + 単体テスト）
2. `jobs/scrape_tasks.py`: `target_url` が検索結果URLの場合 `scrape_search_result(search_url=target_url, ...)` に振り分け
3. `routes/scrape.py` / `scrape_form.html`: URLタブに「件数」セレクトを追加し、検索結果URL受付に対応（プレースホルダー・説明文更新）
4. `build_scrape_job_context` の `limit=1` 固定を解除（検索URL時は指定件数）
5. サイト別に実URLで動作確認（メルカリ/ヤフオク/スニダン優先 → 残り4サイト）
6. E2Eテスト追加（`tests/test_e2e_routes.py` 系 + scrape_request単体テスト）

### フェーズ2: ② プレビュー選別 + 登録先選択
7. `scrape_form.js`: プレビューカードに×ボタン（除外）を実装、選択数表示と連動
8. `models.py`: `Product.is_listed`（bool, default True）を additive migration で追加。一覧/検索/エクスポート系クエリで `is_listed=False` を除外
9. 新API `POST /scrape/register-to-pricelist`: 選択商品を `is_listed=False` で保存し、指定 `PriceList` に `PriceListItem` を作成（リスト新規作成オプション込み、user/shop分離を厳守）
10. UI: 登録ボタンを「商品一覧に登録」「商品リストにのみ登録」に分割、リスト選択モーダル追加
11. 公開カタログ(`routes/catalog.py`)で `source_url`/`site` 非公開の invariant を維持したまま表示されることを確認
12. E2Eテスト（リストのみ登録・一覧非表示・分離）

### フェーズ3: ③ 登録時オプション（英訳 + 利益上乗せ）
13. ユーザー設定にデフォルト `PricingRule` 選択を追加（`routes/settings.py` / `routes/pricing.py`）
14. 登録API群に `pricing_rule_id` / `apply_default_pricing` パラメータ追加 → 保存時にルール割当 + `update_product_selling_price` 実行
15. `TranslationSuggestion.auto_apply` 追加。`translation_tasks.py` 成功時に自動適用（title/description を製品に反映、適用ログ保持）
16. 登録API群に `translate=true` パラメータ追加 → 登録商品ぶんの翻訳ジョブを一括enqueue
17. UI: 登録ダイアログに「英訳する」「利益を上乗せする（ルール選択）」チェックを追加。登録後に翻訳進捗表示
18. worker負荷・Redisキュー上限の確認（100件一括登録時の挙動）
19. E2Eテスト + worker テスト（`tests/test_worker_runtime.py` 系）

### フェーズ4: 仕上げ
20. キーワード検索タブの扱い決定（残す/隠す） — クライアント確認後
21. ドキュメント更新（README / AGENTS.md の feature reality 更新）
22. Render本番での動作確認（esp-worker 経由のジョブ実行）

---

## 4. クライアントへ確認したい事項（実装前の合意推奨）

1. キーワード検索タブは**残すか完全に廃止するか**（推奨: 当面残す）
2. 「商品リストにのみ登録」した商品も**パトロール（在庫・価格監視）の対象にするか**（案Aなら対応可能。一時的な用途なら監視不要＝負荷軽減も可）
3. 翻訳は**自動適用**でよいか（現在は「提案→確認→適用」の安全設計。自動適用だと誤訳がそのまま顧客に見える）
4. 利益上乗せは「全体デフォルトルール1つ」で足りるか、サイト別・リスト別に変えたいか
5. 一時リスト用商品の**自動削除（有効期限）**は必要か

## 5. 技術的リスク（不可能ではないが留意）

- 検索結果URLのスクレイピングはbot検知の影響を単品取得より受けやすい（既存のstealth fetching / fallbackを流用するが、サイト側変更時は取得失敗→明示エラーになる）
- 各サイトの絞り込みパラメータ網羅は「サイトに表示されている通り」を100%保証するものではない（ページング深度上限・除外フィルタの影響あり）
- 翻訳の即時性は非同期処理の制約上「登録後数十秒〜数分」かかる
