# Record City AWS WAF 到達性調査・運用手順

更新日: 2026-09-01

## 結論

Render上の同一egress・同一runtimeで比較した結果、次の4点を確認した。

1. 最初の応答は AWS WAF の **Challenge action** である。`202`、`x-amzn-waf-action: challenge`、`challenge.js` が揃っている。CAPTCHA の証拠である `405`、`x-amzn-waf-action: captcha`、`captcha.js` は観測していない。
2. 旧本番profileのPatchright headlessはChallenge JavaScriptを実行し、`aws-waf-token` Cookieを得た後の自動再送で`403`になった。したがって、旧profileの問題は「JavaScriptを実行できない」「Cookieが存在しない」だけではない。
3. 同じheadless、Render egress、Patchright/Chromium runtimeのまま、UAだけを`HeadlessChrome/145`から通常の`Chrome/145`へ変えると、`202 Challenge → 302 → 200`となり、商品ページのProduct JSON-LDと一覧ページのDOMを検証できた。観測した環境では **露出したHeadlessChrome UAが成否差の原因であることを支持** し、RenderのデータセンターIPだけで一律に拒否されているという仮説は反証された。
4. 最終`403`を発生させたAWS WAFルールがBot Controlの`SignalAutomatedBrowser`、別のmanaged rule、またはカスタムルールのどれかは、クライアント側の応答だけでは確定できない。正確なルール名には対象Web ACLのログが必要である。

AWS公式仕様では、有効なChallengeトークンがある場合、そのChallengeルールはCount相当となり、Web ACLの後続ルール評価が続く。よって「202 Challenge → token発行 → 403」は、Challengeを通過した後に別の終端判定へ到達した場合とも整合する。

- [AWS WAF CAPTCHA and Challenge action behavior](https://docs.aws.amazon.com/waf/latest/developerguide/waf-captcha-and-challenge-actions.html)
- [AWS WAF token characteristics](https://docs.aws.amazon.com/waf/latest/developerguide/waf-tokens-details.html)

## Render で確認した証拠

対象はSingaporeリージョンの`esp-worker`である。まず旧デプロイ`cd0c01d`のRender Logsから、独立した2回の既存プローブを確認した。

| probe | main responses | WAF resource | token | browser signal | 最終結果 |
|---|---|---|---|---|---|
| `47b2ebe4` | `202 challenge` → `403` | script/fetch は `200` | before=false, after=true | webdriver=false、UAに`HeadlessChrome/145`、ja-JP、UTC | `RC_WAF_BLOCK_403` |
| `c43d8db6` | `202 challenge` → `403` | script/fetch は `200` | before=false, after=true | webdriver=false、UAに`HeadlessChrome/145`、ja-JP、UTC | `RC_WAF_BLOCK_403` |

この証拠により、以前の「Patchright はChallengeを経ず即403」という読みは訂正する。実際は、両プローブともChallengeを受け、トークン発行後の自動再送で403になっている。

続いて診断PR [#155](https://github.com/halc8312/ESP/pull/155) のmerge commit `1e8ad35`を同じworkerへデプロイし、低頻度の制御比較を1回実行した。

| probe / profile | 条件 | response遷移 | token | 検証結果 | 所要時間 |
|---|---|---|---|---|---|
| `7898f96d` / `curl-chrome120` | JavaScriptなしHTTP | `202 challenge` | false | Challenge本文、商品ではない | 約0.2秒 |
| `7898f96d` / `patchright-current` | 旧本番headless、`HeadlessChrome/145` | `202 challenge` → `403` | true | `RC_WAF_BLOCK_403`、Productなし | 約25.7秒 |
| `7898f96d` / `patchright-headless-ua` | 同じheadless/egress/runtime、通常`Chrome/145` UA | `202 challenge` → `302` → `200` | true | Product JSON-LDと要求SKUを検証 | 約29.0秒 |
| `7898f96d` / `patchright-headful` | Xvfb上のheadful、native UA | `202 challenge` → `302` → `200` | true | Product JSON-LDと要求SKUを検証 | 約25.2秒 |

この比較のassessmentは`headless_user_agent_factor_supported`となった。特に`patchright-current`と`patchright-headless-ua`はUA以外のbrowser mode、Render egress、runtimeが同じなので、headful化やIP変更を成功条件とせずUA要因を分離できている。

商品以外の経路も確認するため、同じwinning profileで一覧URLを1回だけ実行した。probe `a164d49d`は`202 challenge → 302 → 200`、token取得、検証済み一覧DOM、本文957,741 bytes、約29.3秒で成功した。一覧ページにProduct JSON-LDがないことは正常であり、一覧DOMを成功条件としている。

なお、`aws-waf-token` の存在は「許可」を意味しない。AWS公式には、トークンにはChallenge時刻だけでなく、ブラウザ自動化や設定不整合を含むクライアント信号が格納されるとある。また、Bot Controlはトークンを `accepted` / `rejected` / `absent` としてラベル付けする。Cookie名をクライアント側で確認できても、このラベルやトークン内部は確認できない。

## WAFモードと発火ルールの切り分け

| 問い | 判定 | 根拠 |
|---|---|---|
| Challengeか | 確定 | 202、`x-amzn-waf-action: challenge`、AWS interstitial、`challenge.js` |
| CAPTCHAか | 現観測では否定 | CAPTCHAの標準応答は405と`x-amzn-waf-action: captcha`。どちらも未観測 |
| Challengeを実行できたか | 確定 | 旧profileはtoken発行後403、winning profileはtoken発行後に同一ページの検証済み200 |
| tokenがWAFにacceptedされたか | winning profileではページ到達まで確認、旧profileは不明 | Cookie存在だけでは暗号化トークンの評価ラベルを読めない。通常UA profileはtoken後に商品・一覧へ到達した |
| Bot Controlか | 不明 | 応答にmanaged rule名やlabelは出ない |
| Render IP/ASNだけが原因か | 観測環境では否定 | 同一egressのheadless通常UAが商品・一覧の両方で成功した |
| browser/UA要因か | `HeadlessChrome` UA要因を支持 | 同一headless/egress/runtimeのA/Bで、旧UAは403、通常UAは検証済み200 |
| rate ruleか | 主因としては支持されない | 同一の低頻度比較内でUAに応じて成否が分かれた。ただし将来のrate制限の存在までは否定しない |

AWS Bot Controlの公開ルールには、既定Blockの `SignalAutomatedBrowser` と `SignalKnownBotDataCenter` が別々に存在する。両方が候補になり得ることは分かるが、Record Cityがこのmanaged rule groupや同じactionを採用している証拠にはならない。同一Render egressで通常UAが成功したため無条件のIP単独blockは否定できるが、複数labelを組み合わせるカスタム判定まで否定するものではない。

- [AWS WAF Bot Control rule group](https://docs.aws.amazon.com/waf/latest/developerguide/aws-managed-rule-groups-bot.html)
- [AWS WAF log fields](https://docs.aws.amazon.com/waf/latest/developerguide/logging-fields.html)
- [CloudFront 403 causes](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/http-403-permission-denied.html)

正確なルール名を確定するには、対象Web ACLの `terminatingRuleId`、`terminatingRuleType`、`ruleGroupId`、`labels`、`challengeResponse`、`clientAsn`、`ja3Fingerprint` / `ja4Fingerprint` が必要である。このリポジトリおよびRenderからそれらのログへはアクセスできないため、ルール名について推測を確定表現にしない。

## 公開事例の調査結果

AWS WAFに対して一つの万能策があるという証拠は見つからなかった。採用したのは、各要因を同じデプロイ・低頻度で分離する比較設計である。

| 出典 | 実測内容 | この実装での扱い |
|---|---|---|
| [Streamlink PR #6102](https://github.com/streamlink/streamlink/pull/6102)、[#6111](https://github.com/streamlink/streamlink/pull/6111) | AWS WAFでheadlessはtoken取得失敗、headfulは成功。後にHeadlessChrome UAを通常Chromeへ変えるとheadlessも成功 | `patchright-headless-ua`を独立診断セルとして比較し、Record CityのRender実測でも同じ方向の差を再現したため専用本番profileへ採用 |
| [HEARTH PR #309](https://github.com/THORCollective/HEARTH/pull/309)、[#311](https://github.com/THORCollective/HEARTH/pull/311) | HTTPは202 Challenge、headless Playwrightで記事取得。固定待機は不安定で、DOM待機・reload・再試行で改善 | 固定sleepを成功判定に使わず、attached DOMとProduct JSON-LDを検証。無制限retryは不採用 |
| [amazon-location-cookies-service PR #17](https://github.com/borys25ol/amazon-location-cookies-service/pull/17) | TLS profileやヘッダだけでは失敗。headless Chromiumでtokenを得て同一UAのHTTPへ移送し成功。tokenは表示期限より早く失効する場合あり | token値をログせず、期限付きメモリキャッシュと制御された1回再送を維持。ただし旧profileの403には単独で効かなかった |
| [sitedobarral PR #183](https://github.com/Danbarral2019/sitedobarral/pull/183) | 同一マシンでHTTP=202、headless=403、headed=200 | Xvfb上の`patchright-headful`セルを採用 |
| [Hardcover-Sync PR #4](https://github.com/gmoran1016/Hardcover-Sync/pull/4) | headed Chromium + XvfbとCookie/UA/client hints整合で成功 | headful比較の根拠。複数変更一括のため個別因果は採用しない |
| [Stack Overflow: WAF silent challenge](https://stackoverflow.com/questions/77529521/why-does-the-aws-waf-intelligent-threat-api-silent-challenge-never-fail) | 投稿者がAWS Support回答として紹介した内容では、自動ブラウザにもtokenは発行され得て、bot判定はtoken内に入り後続でCAPTCHA等になり得る | 二次情報として扱い、`token_after=true`を成功条件にしない |
| [OpenSanctions PR #5136](https://github.com/opensanctions/opensanctions/pull/5136)、[#5535](https://github.com/opensanctions/opensanctions/pull/5535) | Zyte browser session + token再利用で一度成功したが、後に毎回202へ回帰し別データ経路へ移行 | Zyteを有力な外部経路として実装。ただし恒久安定を前提にしない |
| [transfermarkt-scraper PR #7](https://github.com/marcgarnica13/transfermarkt-scraper/pull/7) | Zyte出口ごとにAWS WAF Challenge率が異なり、出口変更で一部回収 | egress要因の実例。高頻度の出口総当たりは不採用 |
| [Patchright README](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#best-practice---use-chrome-without-fingerprint-injection) | Linuxの推奨はheadful + Xvfb、native fingerprint、custom headers/UAを避ける構成 | `headful`比較はnative UA/no viewportで実施。ただし同一headless A/Bで通常UAの効果を実測できたため、運用負荷の小さいRecord City専用UAを採用 |
| [ScraperAPI AWS WAF記事](https://www.scraperapi.com/blog/how-to-scrape-amazon-waf-protected-websites/) | ベンダー自身のOLX例でraw約20%、API 99%、3–5秒と主張 | 実装候補には含めるが、独立検証でないため成功率を採用判断に使わない |
| [String benchmark](https://usestring.ai/benchmark) / [raw data](https://github.com/usestring/web-data-frontier-benchmark/blob/main/official_results/benchmark-2026-08-11T22-44-25-322Z.json) | 公開rawのAWS WAF 7サイトではZyte 16/35、ScraperAPI 20/35。ただし設定は両社とも基本HTTPのみ | 「外部APIも対象依存」の反例として記録。最大能力比較には使わない |
| [ScraperAPI cost control](https://docs.scraperapi.com/control-and-optimization/cost-control) | `max_cost`超過はprovider生成403。対象statusは`sa-statuscode`で分離可能 | provider 403とRecord City 403を別reason codeにし、`sa-statuscode`がある場合だけ対象statusとして扱う |

Reddit、GitHub Issues/PR、Stack Overflow、各社ドキュメントまで調べたが、第三者がRecord CityでPatchright、Zyte、ScraperAPIを成功させた報告は見つからなかった。したがってサービス名だけで成功を断定しない。

## 対応案の比較

評価はRecord Cityに対する現時点の根拠であり、一般的な製品評価ではない。

| 案 | 実現可能性 | 安定性 | 保守コスト | 判断 |
|---|---|---|---|---|
| browser_poolへ共通のUA/init scriptを追加 | 高 | 低 | 中 | 不採用。全サイトへ波及する。現行Patchrightで`webdriver=false`は既に達成済み |
| Record City専用context options | 高 | 中 | 低 | **採用済み**。共有コードを変えず、既存headless Patchrightへ通常Chrome UAを専用adapterから渡す |
| `navigator.webdriver`を隠すinit script | 高 | 低 | 低 | 現時点では不採用。Render実測で既にfalse。重ねてもこの信号は変わらない |
| Patchright headless + 既定`HeadlessChrome` UA | 実測済み | 低 | 低 | PR #155時点の旧controlでtoken後403を再現。現在の公開profileと本番経路には使わない |
| Patchright headless + 通常Chrome UA | 高 | 中 | 低 | **採用済み**。同一headless/egress/runtimeの比較で商品・一覧とも検証済み200 |
| Patchright headful + Xvfb | 高 | 中 | 中 | 商品ページでは成功したが不採用。headless通常UAも成功し、Xvfb常時運用の追加コストが不要 |
| persistent context | 中 | 未検証 | 中～高 | 不採用。現行の一時context内でChallenge、token、商品・一覧到達まで完了したため必要性がない |
| token Cookie再利用 | 実装済み | 低～中 | 中 | 維持。ただし旧profileはtoken後403なので単独解ではない。token値は保存・出力しない |
| curl_cffi impersonationのみ | 高 | 低 | 低 | 診断セルとして採用。JavaScript runtimeがなくChallengeを解けないため本番推奨ではない |
| Scrapling StealthyFetcher | 高 | 未検証 | 中 | 不採用。既存Patchrightの最小変更で成功したためbrowser stackを増やさない |
| Camoufox/nodriver/undetected-chromedriver | 中 | 未検証 | 高 | AWS WAFでの再現性ある優位を確認できず、新規browser stack追加になるため保留 |
| Zyte browserHtml | 高 | 対象依存 | 低～中 | fallback実装のみ維持。現行無料経路が成功したため設定・課金しない |
| ScraperAPI render + JP | 高 | 対象依存 | 低～中 | fallback実装のみ維持。現行無料経路が成功したため設定・課金しない |
| 独立JP proxyで2×2比較 | proxy契約時は高 | provider依存 | 中 | 現時点では不要。同一Render egressで成功し、IP単独ブロック仮説を反証できた |
| キャッシュ/Wayback | 高 | 在庫・価格には不適 | 低 | 鮮度要件を満たさないため不採用 |

## 実装内容

### 採用した本番profile

Record City専用adapterの既存headless Patchrightに、検証済みの通常Chrome/145 UAを`context_options`として渡す。`services/browser_pool.py`や`services/scraping_client.py`は変更せず、Mercari、Surugaya、SNKRDUNKなど他サイトの取得profileには影響させない。

Render実測では`navigator.webdriver=false`が既に成立していたためinit scriptは追加しない。headfulも成功したが、同じegressのheadless通常UAで商品と一覧の両方を取得できたため、Xvfbを本番workerへ常時追加する構成にはしない。外部providerやproxyもproductionには設定しない。

通常UAはPatchrightが利用するChromiumのmajor versionと一致させる必要がある。今回の`Chrome/145`は現在のpinned Patchright/Chromiumに対応するため、browser runtimeをupgradeするときはUAも同時に更新し、Render上でChallenge通過とProduct/一覧DOMを再確認する。固定UAだけを将来も据え置く運用はしない。

### Record City専用ブラウザ診断

`services/recordcity_browser_fetch.py` に、production token cache/retryから独立した1回限りのprobe profileを追加した。

- `patchright-current`: 現在の本番と同じheadless profile（本修正後は通常Chrome UA）
- `patchright-headless-ua`: 通常Chrome UA profileの互換名。PR #155時点の制御比較で使用
- `patchright-headful`: Xvfb上のheadful、native UA、no viewport
- `patchright-headful-tokyo`: headful + Asia/Tokyo
- `patchright-headless-proxy`
- `patchright-headful-proxy`

各セルは別runtime名を使うため、共有browser poolに先に作られたprofileと混線しない。返すのはstatus、WAF action、CloudFront request ID、tokenの有無、UA/browser signals、body長とSHA-256、Product JSON-LDの検証結果だけで、HTML本文とtoken値は返さない。

### 外部取得経路

`services/recordcity_external_fetch.py` を追加し、Record Cityにだけ次を提供する。

- Zyte: `browserHtml=true`、`javascript=true`、`geolocation=JP`、attached selector待機
- ScraperAPI: `render=true`、`country_code=jp`、明示routingごとの`max_cost`
- 任意HTTPS URL template: `{url}`は必ずURL encodeし、credentials/非443/fragment/`{raw_url}`を拒否
- 独立proxy: 診断専用。各redirectをRecord City allowlistで再検証し、Cookieを同一session内だけ維持

外部応答は最大12 MiBに制限し、detail pageは最終URLの商品IDとJSON-LDの`sku`が要求IDに一致した場合だけ成功とする。provider自身の非2xxは `RC_EXTERNAL_PROVIDER_HTTP_ERROR`、対象metadataがあるChallenge/CAPTCHA/403は別のreason codeで表示する。metadataなしでブロック本文だけが返る場合は、Record Cityとproviderのどちらが発生源か断定せず `RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS` とする。

productionは`RECORDCITY_FETCH_PROVIDER`の明示値がない限り、採用したRecord City専用headless通常UAのPatchright経路を使う。API keyを置いただけでは課金経路へ切り替わらない。generic proxyはproduction providerとして選べない。

### 一括probe CLI

`flask --app app:app recordcity-probe URL` を追加した。

- URLは通信前にRecord City allowlistで検証
- 1 URL、各strategy 1回、strategy間は既定5秒
- 有料/外部経路は `--allow-external` がない限りskip
- headfulはDISPLAYがなければskipし、Xvfb利用を明示
- 人間向け表とmachine-readable JSONを出力
- 結果matrixから、UA要因、browser mode要因、egress要因、両者の相互作用、またはinconclusiveを判定

### 他サイトへの影響を閉じ込めた点

- `services/browser_pool.py`、`services/scraping_client.py`、Mercari、Surugaya、SNKRDUNKの経路は変更していない。
- `recordcity_db.py` のfetch入口だけをRecord City専用orchestratorへ差し替えた。
- production provider未選択時は、従来と同じ引数でRecord City Patchright adapterへ委譲するテストを追加した。
- DB migrationは追加していない。

### コンテナ

診断用headful ChromiumをRenderとCIで実行できるよう、`xvfb`と`xauth`を追加した。通常worker起動ではXvfbを起動しない。Docker CIにはPatchright headfulを`about:blank`で起動し、UAに`HeadlessChrome`がないことを確認するsmoke testを追加した。

## テスト結果

ローカルのPython 3.12環境で次を確認した。

```text
Record City関連 + HTML adapter: 125 passed
全体: 1365 passed, 1 skipped, 16 warnings
```

ネットワークへ出るpytestは追加していない。実サイト到達性は、Render probe `7898f96d`（商品）と`a164d49d`（一覧）で別途確認済みである。pytestは引き続きネットワーク非依存とし、live probeを通常CIには含めない。

## Renderで実行したコマンドと結果

### 1. 同一Render egressでbrowser要因を比較（完了）

merge commit `1e8ad35`のデプロイ後、有料経路なしで次の4セルを1回実行した。

```bash
xvfb-run -a flask --app app:app recordcity-probe \
  https://www.recordcity.jp/catalog/4936480 \
  --strategy curl-chrome120 \
  --strategy patchright-current \
  --strategy patchright-headless-ua \
  --strategy patchright-headful \
  --timeout-seconds 60 \
  --delay-seconds 5
```

結果はprobe `7898f96d`で、旧currentのみtoken後403、headless通常UAとheadfulは検証済みProduct成功となった。browser起動エラーやDOM欠落ではなく、同一headlessのUA単独差で結果が再現し、assessmentは`headless_user_agent_factor_supported`となった。

### 2. 一覧ページのwinning profile確認（完了）

商品ページだけの偶然を避けるため、同じheadless通常UAで一覧ページも1回確認した。

```bash
flask --app app:app recordcity-probe \
  'https://www.recordcity.jp/catalog?narrow_down_3=3' \
  --strategy patchright-headless-ua \
  --timeout-seconds 60 \
  --delay-seconds 5
```

probe `a164d49d`は`202 challenge → 302 → 200`、token取得、検証済み一覧DOMで成功した。これにより商品詳細と一覧の両方を本番候補profileで確認できた。

## 再発時だけ行う追加診断

現行の無料経路が成功している間、次のproxy/provider probeは実行しない。

### 独立proxyの2×2比較

`RECORDCITY_PROXY_URL` に独立したJP egressを設定した場合のみ実行する。

```bash
xvfb-run -a flask --app app:app recordcity-probe \
  https://www.recordcity.jp/catalog/4936480 \
  --strategy patchright-current \
  --strategy patchright-headful \
  --strategy patchright-headless-proxy \
  --strategy patchright-headful-proxy \
  --timeout-seconds 60 \
  --delay-seconds 5 \
  --allow-external
```

この4セルは、将来同一Render egressの通常UAも失敗した場合にbrowser modeとegressの寄与を再分離する比較材料になる。ただし各セル1回の逐次観測なので、時刻差、rate rule、proxy出口差は残る。確定には低頻度での一貫した再現、または対象Web ACLログが必要である。

### Zyteを1回だけprobe

`RECORDCITY_ZYTE_API_KEY` をRender secretとして設定後に実行する。

```bash
flask --app app:app recordcity-probe \
  https://www.recordcity.jp/catalog/4936480 \
  --strategy zyte \
  --timeout-seconds 60 \
  --delay-seconds 5 \
  --allow-external
```

通常UA経路が再び失敗し、Zyteが検証済みProductを返した場合にのみproduction切替を検討する。

```text
RECORDCITY_FETCH_PROVIDER=zyte
RECORDCITY_ZYTE_API_KEY=<Render secret>
```

### ScraperAPIをprobe

```text
RECORDCITY_SCRAPERAPI_KEY=<Render secret>
RECORDCITY_SCRAPERAPI_ROUTING=standard
```

```bash
flask --app app:app recordcity-probe \
  https://www.recordcity.jp/catalog/4936480 \
  --strategy scraperapi \
  --timeout-seconds 60 \
  --delay-seconds 5 \
  --allow-external
```

`RECORDCITY_SCRAPERAPI_ROUTING` は `standard`、`premium`、`ultra_premium` のみ許可する。それぞれrendered requestの上限を10、25、75 creditsに固定し、不明値は通信前に拒否する。

## 費用と契約条件

価格は2026-09-01時点。契約前に公式ページで再確認する。

| 経路 | 現行価格・制約 | 推奨用途 |
|---|---|---|
| Zyte API | Standard PAYG、初月$5 credit、月$100 spending limitはcommitmentなし。対象サイトとbrowser tierでリクエスト単価が変動し、成功応答のみ課金 | 最初の外部1回probe。API key単位のblocking spend limitも設定する |
| ScraperAPI | 7日trial/5,000 credits。JPを含むGlobal geotargetingはBusiness $299/月から。render 10 credits、premium+render 25、ultra premium+render 75 | Zyte不成功時の第二候補。まずstandardを1回だけ |
| 独立proxy | provider依存 | 原因をIPとbrowserに分離する2×2診断。production自動利用はしない |

- [Zyte API pricing](https://docs.zyte.com/zyte-api/pricing.html)
- [Zyte API browser reference](https://docs.zyte.com/zyte-api/usage/reference.html)
- [ScraperAPI pricing](https://www.scraperapi.com/pricing/)
- [ScraperAPI credits](https://docs.scraperapi.com/getting-started/quick-start/credits-and-requests-costs)
- [ScraperAPI standard geotargeting](https://docs.scraperapi.com/control-and-optimization/geotargeting/standard-geo)

## 採用判断と運用順序

1. Record City専用の既存headless Patchrightに、pinned Chromiumとmajor versionが一致する通常Chrome UAを設定する。共有`browser_pool`、init script、headful、外部providerは使わない。
2. 既存のtoken cache/retryと取得失敗理由の表示を維持する。`200`やCookieの存在だけでなく、商品はProduct JSON-LDとSKU、一覧は一覧DOMまで検証する。
3. Patchright/Chromiumのupgrade時はUAを同時に更新し、商品1件と一覧1件を低頻度で再確認する。UAと実browser majorの不一致を放置しない。
4. 将来通常UA profileが失敗した場合は、まず同一egressのheadless/headful比較を1回行う。それでも分離できない場合だけ独立egressの2×2、Zyte、ScraperAPIを順に検討する。
5. fallbackを試す場合も、検証済みProduct JSON-LDを返さない経路は採用しない。成功に見える`200`やCookieだけを成功条件にしない。

## 確定範囲と残る限界

Render上では、旧`HeadlessChrome` UAがtoken後403、通常Chrome UAが同一headless/egress/runtimeで商品・一覧とも成功するところまで実証した。このため、観測環境における主因の切り分けと、本番へ採用する最小変更の根拠は得られている。

一方、クライアント側からは最終403の`terminatingRuleId`やlabelsを読めないため、Bot Controlの特定ルールかカスタムルールかは未確定である。また1回ずつの低頻度probeは長期安定性を保証しない。Record City側のWAF設定やChromium fingerprintが変われば再検証が必要になる。

現在のRenderには`RECORDCITY_*`の外部provider key/proxyを設定しておらず、Zyte、ScraperAPI、proxy probeも実行していない。無料のRecord City専用Patchright経路で検証済みHTMLを取得できたため、現時点では追加契約やegress変更の根拠がない。
