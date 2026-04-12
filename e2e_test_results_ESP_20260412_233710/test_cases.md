# test_cases

## 目次
- [詳細E2Eテストケース一覧](#詳細e2eテストケース一覧)
- [カテゴリ別整理](#カテゴリ別整理)

## 詳細E2Eテストケース一覧

| ID | 優先度 | カテゴリ | 観点 | 目的 | 前提条件 | 手順 | 期待結果 | 追加確認 | バグになりやすい理由 | 関連機能 / 依存機能 | 証跡想定 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| TC-E2E-001 | Critical | 認証 | 初回登録 | 新規登録と自動ログインを確認 | 未ログイン | `/register` でユーザー登録 | `/` へ遷移しログイン状態になる | 再読み込み後も認証維持 | セッション初期化漏れ | auth, main | EVD-001 |
| TC-E2E-002 | Critical | 認証 | レート制限 | 不正認証時の失敗表示と上限到達挙動を確認 | ユーザー存在 | 誤パスワードを6回送信 | 5回までは認証失敗、6回目で上限文言表示 | 以降のログイン影響 | IP単位境界が壊れやすい | auth | EVD-002 |
| TC-E2E-003 | High | ショップ | 作成/重複 | ショップ追加と重複名防止確認 | ログイン済み | `/shops` で追加後、同名追加 | 一覧反映し重複は拒否 | current_shop 候補反映 | ユーザー分離と重複制御 | shops | EVD-003 |
| TC-E2E-004 | Critical | 商品 | 手動作成 | 商品手動追加から編集画面遷移まで確認 | ショップ存在 | `/products/manual-add` で登録 | 詳細画面へ遷移し初期値が保持される | 変種/在庫/説明の初期値 | モデル複数生成 | main, products | EVD-004 |
| TC-E2E-005 | High | 商品 | バリデーション | 重複 source_url と入力保持確認 | 既存商品あり | 同一元URLで再登録 | エラー表示、保存されない、入力保持 | DB件数維持 | サーバ側制御漏れ | main | EVD-005 |
| TC-E2E-006 | Critical | 権限 | 直接URL | 他ユーザー商品の参照拒否確認 | ユーザーA/B作成済み | ユーザーBで `/product/1` へアクセス | 404/拒否される | DB改変なし | IDOR事故 | products | EVD-006 |
| TC-E2E-007 | Critical | 商品 | 更新整合性 | 編集後に一覧/DB/公開面へ反映されるか確認 | 商品存在 | 商品名・EN名・variant価格/在庫/SKU を編集して保存 | 詳細・一覧・DBが一致 | `selling_price` 同期 | 複数モデル更新 | products, main | EVD-007 |
| TC-E2E-008 | High | テンプレート | 作成/削除 | テンプレート作成削除とHTMLサニタイズを確認 | ログイン済み | `/templates` で script 含む内容を作成し削除 | 一覧反映、scriptは除去、削除成功 | WYSIWYG有無 | リッチテキスト系は壊れやすい | shops/manage_templates | EVD-008 |
| TC-E2E-009 | High | 価格設定 | 作成/編集 | 価格ルールの作成/更新確認 | ログイン済み | `/pricing` で作成しモーダル編集 | 計算例と一覧が更新される | 再計算メッセージ | 数値変換・モーダル保存 | pricing | EVD-009 |
| TC-E2E-010 | Critical | 価格表 | 作成〜公開 | 価格表作成、商品追加、公開URL確認 | 商品存在 | `/pricelists/create` → items → add-products | カタログ公開ページに商品が表示される | token URL | 公開導線は業務影響大 | pricelist, catalog | EVD-010 |
| TC-E2E-011 | High | 価格表 | notes/layout | notesサニタイズと layout 反映確認 | 価格表作成可能 | script含む notes と list layout で作成 | 公開面で script除去、layout反映 | 内部情報非露出 | XSS/設定漏れ | pricelist, catalog | EVD-011 |
| TC-E2E-012 | High | 公開分析 | page view | カタログ閲覧/Quick View が analytics に反映されるか確認 | 価格表公開済み | catalog閲覧 → Quick View → analytics確認 | PV/詳細表示が増える | referrer/ユニーク | 非同期記録漏れ | catalog | EVD-012 |
| TC-E2E-013 | Medium | 一覧 | 検索/フィルタ | 商品一覧検索/フィルタ整合 | 複数商品 | indexで検索/絞込 | 一覧/件数一致 | 再読み込み保持 | 条件組合せで壊れやすい | main | EVD-013 |
| TC-E2E-014 | Medium | 状態遷移 | アーカイブ/ゴミ箱 | archive/trash移動と復元確認 | 商品存在 | 一括操作で archive/trash/restore | 一覧間の移動が整合 | 復元後公開面 | 状態遷移抜け | archive, trash | EVD-014 |
| TC-E2E-015 | Medium | セキュリティ | ログアウト後戻る | ログアウト後に保護画面へ戻れないことを確認 | ログイン済み | `/logout` 後にブラウザ戻る | loginへ遷移し再表示不可 | nextパラメータ | キャッシュ露出 | auth | EVD-015 |
| TC-E2E-016 | High | API | 所有権 | 他ユーザーからの inline-update API を拒否するか確認 | ユーザーBログイン | `/api/products/1/inline-update` を PATCH | 404/拒否 | CSRF/JSONレスポンス | APIだけ抜けやすい | api | EVD-016 |
| TC-E2E-017 | Medium | インポート/エクスポート | CSV | CSV導線と文字化け確認 | サンプルCSV | import preview/execute, export download | 成功または明確な失敗 | 列ズレ | 形式依存 | import/export | EVD-017 |
| TC-E2E-018 | High | 外部連携 | scrape失敗 | 外部サイト/ネットワーク失敗時のジョブ状態確認 | scrape設定 | `/scrape` 実行し失敗条件を与える | 見かけ成功せず失敗が可視化 | tracker/API | 非同期失敗隠蔽 | scrape, api | EVD-018 |
| TC-E2E-019 | High | 非機能入口 | 外部CDN断 | 編集系画面が CDN 不達でも致命的に崩れないことを確認 | CDN到達性を制限可能 | `/product/1`, `/templates`, `/pricelists/create` を開く | フォールバック表示で継続利用可能、JS例外なし | console error, WYSIWYG | 外部依存は環境差で顕在化 | product_detail, manage_templates, pricelist_edit | EVD-019 |
| TC-E2E-020 | Medium | セキュリティ | 価格表直接URL | 他ユーザーの価格表管理画面に到達できないことを確認 | ユーザーA/B作成済み | ユーザーBで `/pricelists/1/items` へアクセス | 価格表一覧へ逃がす/拒否 | analytics/edit も同様 | owner check 漏れ | pricelist | EVD-020 |

## カテゴリ別整理
- 認証: TC-E2E-001, 002, 015
- ショップ/テンプレート/価格設定: TC-E2E-003, 008, 009
- 商品/状態遷移: TC-E2E-004, 005, 006, 007, 013, 014
- 価格表/公開/分析: TC-E2E-010, 011, 012, 020
- API/外部連携/非機能: TC-E2E-016, 017, 018, 019
