# Record City AWS WAF 到達性調査・運用手順

更新日: 2026-09-01

## 結論

現時点で確定しているのは次の3点です。

1. 最初の応答は AWS WAF の **Challenge action** である。`202`、`x-amzn-waf-action: challenge`、`challenge.js` が揃っている。CAPTCHA の証拠である `405`、`x-amzn-waf-action: captcha`、`captcha.js` は観測していない。
2. Render の現行 Patchright は Challenge JavaScript を実行し、`aws-waf-token` Cookie を得ている。その後の自動再送が `403` になっている。したがって、問題は「JavaScriptを実行できない」「Cookieが存在しない」だけではない。
3. 最終 `403` の発火ルールが、Bot Control のブラウザ判定、データセンターIP判定、カスタムルール、レートルール、またはCloudFront側の別制限のどれかは、クライアント側の応答だけでは確定できない。現状では **headless/browser fingerprint と Render egress/IP の寄与は未分離** である。

AWS公式仕様では、有効なChallengeトークンがある場合、そのChallengeルールはCount相当となり、Web ACLの後続ルール評価が続く。よって「202 Challenge → token発行 → 403」は、Challengeを通過した後に別の終端判定へ到達した場合とも整合する。

- [AWS WAF CAPTCHA and Challenge action behavior](https://docs.aws.amazon.com/waf/latest/developerguide/waf-captcha-and-challenge-actions.html)
- [AWS WAF token characteristics](https://docs.aws.amazon.com/waf/latest/developerguide/waf-tokens-details.html)

## Render で確認した証拠

対象は `esp-worker`、当時のデプロイコミットは `cd0c01d`、リージョンは Singapore である。Render Logs の直近7日検索から、独立した2回の既存プローブを確認した。

| probe | main responses | WAF resource | token | browser signal | 最終結果 |
|---|---|---|---|---|---|
| `47b2ebe4` | `202 challenge` → `403` | script/fetch は `200` | before=false, after=true | webdriver=false、UAに`HeadlessChrome/145`、ja-JP、UTC | `RC_WAF_BLOCK_403` |
| `c43d8db6` | `202 challenge` → `403` | script/fetch は `200` | before=false, after=true | webdriver=false、UAに`HeadlessChrome/145`、ja-JP、UTC | `RC_WAF_BLOCK_403` |

この証拠により、以前の「Patchright はChallengeを経ず即403」という読みは訂正する。実際は、両プローブともChallengeを受け、トークン発行後の自動再送で403になっている。

なお、`aws-waf-token` の存在は「許可」を意味しない。AWS公式には、トークンにはChallenge時刻だけでなく、ブラウザ自動化や設定不整合を含むクライアント信号が格納されるとある。また、Bot Controlはトークンを `accepted` / `rejected` / `absent` としてラベル付けする。Cookie名をクライアント側で確認できても、このラベルやトークン内部は確認できない。

## WAFモードと発火ルールの切り分け

| 問い | 判定 | 根拠 |
|---|---|---|
| Challengeか | 確定 | 202、`x-amzn-waf-action: challenge`、AWS interstitial、`challenge.js` |
| CAPTCHAか | 現観測では否定 | CAPTCHAの標準応答は405と`x-amzn-waf-action: captcha`。どちらも未観測 |
| Challengeを実行できたか | Cookie発行までは確定 | token resource 200、token_after=true、その後に同一ページの403 |
| tokenがWAFにacceptedされたか | 不明 | Cookie存在だけでは暗号化トークンの評価ラベルを読めない |
| Bot Controlか | 不明 | 応答にmanaged rule名やlabelは出ない |
| IP/ASNルールか | 不明 | 独立egressとの同一browser比較がまだない |
| automation ruleか | 不明だが候補 | webdriverはfalseだがHeadlessChrome UAは露出。トークン自体にもbrowser interrogation信号が入る |
| rate ruleか | 不明 | 数十分離れた2プローブが同じ結果なので単純な短期バーストだけでは説明しにくいが、除外はできない |

AWS Bot Controlの公開ルールには、既定Blockの `SignalAutomatedBrowser` と `SignalKnownBotDataCenter` が別々に存在する。両方が候補になり得ることは分かるが、Record Cityがこのmanaged rule groupや同じactionを採用している証拠にはならない。

- [AWS WAF Bot Control rule group](https://docs.aws.amazon.com/waf/latest/developerguide/aws-managed-rule-groups-bot.html)
- [AWS WAF log fields](https://docs.aws.amazon.com/waf/latest/developerguide/logging-fields.html)
- [CloudFront 403 causes](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/http-403-permission-denied.html)

正確なルール名を確定するには、対象Web ACLの `terminatingRuleId`、`terminatingRuleType`、`ruleGroupId`、`labels`、`challengeResponse`、`clientAsn`、`ja3Fingerprint` / `ja4Fingerprint` が必要である。このリポジトリおよびRenderからそれらのログへはアクセスできないため、ルール名について推測を確定表現にしない。

## 公開事例の調査結果

AWS WAFに対して一つの万能策があるという証拠は見つからなかった。採用したのは、各要因を同じデプロイ・低頻度で分離する比較設計である。

| 出典 | 実測内容 | この実装での扱い |
|---|---|---|
| [Streamlink PR #6102](https://github.com/streamlink/streamlink/pull/6102)、[#6111](https://github.com/streamlink/streamlink/pull/6111) | AWS WAFでheadlessはtoken取得失敗、headfulは成功。後にHeadlessChrome UAを通常Chromeへ変えるとheadlessも成功 | `patchright-headless-ua` を独立診断セルとして採用。本番既定にはしない |
| [HEARTH PR #309](https://github.com/THORCollective/HEARTH/pull/309)、[#311](https://github.com/THORCollective/HEARTH/pull/311) | HTTPは202 Challenge、headless Playwrightで記事取得。固定待機は不安定で、DOM待機・reload・再試行で改善 | 固定sleepを成功判定に使わず、attached DOMとProduct JSON-LDを検証。無制限retryは不採用 |
| [amazon-location-cookies-service PR #17](https://github.com/borys25ol/amazon-location-cookies-service/pull/17) | TLS profileやヘッダだけでは失敗。headless Chromiumでtokenを得て同一UAのHTTPへ移送し成功。tokenは表示期限より早く失効する場合あり | token値をログせず、期限付きメモリキャッシュと制御された1回再送を維持。ただし今回の403には効いていない |
| [sitedobarral PR #183](https://github.com/Danbarral2019/sitedobarral/pull/183) | 同一マシンでHTTP=202、headless=403、headed=200 | Xvfb上の`patchright-headful`セルを採用 |
| [Hardcover-Sync PR #4](https://github.com/gmoran1016/Hardcover-Sync/pull/4) | headed Chromium + XvfbとCookie/UA/client hints整合で成功 | headful比較の根拠。複数変更一括のため個別因果は採用しない |
| [Stack Overflow: WAF silent challenge](https://stackoverflow.com/questions/77529521/why-does-the-aws-waf-intelligent-threat-api-silent-challenge-never-fail) | 投稿者がAWS Support回答として紹介した内容では、自動ブラウザにもtokenは発行され得て、bot判定はtoken内に入り後続でCAPTCHA等になり得る | 二次情報として扱い、`token_after=true`を成功条件にしない |
| [OpenSanctions PR #5136](https://github.com/opensanctions/opensanctions/pull/5136)、[#5535](https://github.com/opensanctions/opensanctions/pull/5535) | Zyte browser session + token再利用で一度成功したが、後に毎回202へ回帰し別データ経路へ移行 | Zyteを有力な外部経路として実装。ただし恒久安定を前提にしない |
| [transfermarkt-scraper PR #7](https://github.com/marcgarnica13/transfermarkt-scraper/pull/7) | Zyte出口ごとにAWS WAF Challenge率が異なり、出口変更で一部回収 | egress要因の実例。高頻度の出口総当たりは不採用 |
| [Patchright README](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#best-practice---use-chrome-without-fingerprint-injection) | Linuxの推奨はheadful + Xvfb、native fingerprint、custom headers/UAを避ける構成 | `headful`はnative UA/no viewport。custom UAは診断セルだけに限定 |
| [ScraperAPI AWS WAF記事](https://www.scraperapi.com/blog/how-to-scrape-amazon-waf-protected-websites/) | ベンダー自身のOLX例でraw約20%、API 99%、3–5秒と主張 | 実装候補には含めるが、独立検証でないため成功率を採用判断に使わない |
| [String benchmark](https://usestring.ai/benchmark) / [raw data](https://github.com/usestring/web-data-frontier-benchmark/blob/main/official_results/benchmark-2026-08-11T22-44-25-322Z.json) | 公開rawのAWS WAF 7サイトではZyte 16/35、ScraperAPI 20/35。ただし設定は両社とも基本HTTPのみ | 「外部APIも対象依存」の反例として記録。最大能力比較には使わない |
| [ScraperAPI cost control](https://docs.scraperapi.com/control-and-optimization/cost-control) | `max_cost`超過はprovider生成403。対象statusは`sa-statuscode`で分離可能 | provider 403とRecord City 403を別reason codeにし、`sa-statuscode`がある場合だけ対象statusとして扱う |

Reddit、GitHub Issues/PR、Stack Overflow、各社ドキュメントまで調べたが、第三者がRecord CityでPatchright、Zyte、ScraperAPIを成功させた報告は見つからなかった。したがってサービス名だけで成功を断定しない。

## 対応案の比較

評価はRecord Cityに対する現時点の根拠であり、一般的な製品評価ではない。

| 案 | 実現可能性 | 安定性 | 保守コスト | 判断 |
|---|---|---|---|---|
| browser_poolへ共通のUA/init scriptを追加 | 高 | 低 | 中 | 不採用。全サイトへ波及する。現行Patchrightで`webdriver=false`は既に達成済み |
| Record City専用context options | 高 | 中 | 低 | 採用。共有コードを変えず、locale/no_viewport/timezone/proxy/UAセルを専用adapterから渡す |
| `navigator.webdriver`を隠すinit script | 高 | 低 | 低 | 現時点では不採用。Render実測で既にfalse。重ねてもこの信号は変わらない |
| Patchright headless | 実装済み | 低 | 低 | 現行controlとして維持。Renderで2回ともtoken後403 |
| Patchright headless + 通常Chrome UA | 高 | 未検証 | 中 | 診断セルとして採用。client hints不整合の可能性があるため本番既定にはしない |
| Patchright headful + Xvfb | 高 | 未検証 | 中 | 最優先の無料診断セルとして採用。成功した場合だけRecord City本番経路への昇格を検討 |
| persistent context | 中 | 未検証 | 中～高 | 今回の最初の比較には不採用。現行は同一context内でtoken後の自動再送まで到達済みで、まずbrowser modeを分離する |
| token Cookie再利用 | 実装済み | 低～中 | 中 | 維持。ただしtoken後403なので単独解ではない。token値は保存・出力しない |
| curl_cffi impersonationのみ | 高 | 低 | 低 | 診断セルとして採用。JavaScript runtimeがなくChallengeを解けないため本番推奨ではない |
| Scrapling StealthyFetcher | 高 | 未検証 | 中 | 今回は不採用。Patchrightと独立egressを同時に変えず、まず原因分離を優先 |
| Camoufox/nodriver/undetected-chromedriver | 中 | 未検証 | 高 | AWS WAFでの再現性ある優位を確認できず、新規browser stack追加になるため保留 |
| Zyte browserHtml | 高 | 対象依存 | 低～中 | 外部経路の第一候補として採用。明示設定時だけ本番利用 |
| ScraperAPI render + JP | 高 | 対象依存 | 低～中 | 第二候補として採用。JP geotargetingの月額条件が重い |
| 独立JP proxyで2×2比較 | proxy契約時は高 | provider依存 | 中 | 原因分離には最も強い。診断専用で採用し、本番自動選択から除外 |
| キャッシュ/Wayback | 高 | 在庫・価格には不適 | 低 | 鮮度要件を満たさないため不採用 |

## 実装内容

### Record City専用ブラウザ診断

`services/recordcity_browser_fetch.py` に、production token cache/retryから独立した1回限りのprobe profileを追加した。

- `patchright-current`: 現行本番と同じheadless control
- `patchright-headless-ua`: `HeadlessChrome`だけを通常Chrome UAへ変更
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

productionは `RECORDCITY_FETCH_PROVIDER` の明示値がない限り、従来のPatchright経路を使う。API keyを置いただけでは課金経路へ切り替わらない。generic proxyはproduction providerとして選べない。

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
Record City関連 + HTML adapter: 122 passed
全体: 1362 passed, 1 skipped, 16 warnings
```

ネットワークへ出るpytestは追加していない。実サイト到達性はRenderへのデプロイ後にのみ確認する。

## Renderで実行するコマンド

### 1. 同一Render egressでbrowser要因を比較

最初は有料経路なしで、次の4セルだけを1回実行する。

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

判定:

- currentがWAF失敗、headless-uaが検証済みProduct成功: `HeadlessChrome` UA要因を支持
- currentがWAF失敗、headfulが検証済みProduct成功: headless/browser mode要因を支持
- current/headless-ua/headfulがすべて比較可能なWAF失敗: browser modeだけでは解消しない。IPを含む別要因が残る
- curlだけが検証済みProduct成功: TLS/HTTP fingerprint差を支持。ただしChallengeの継続安定性を別途確認する

browser起動エラー、timeout、navigation error、または単なるDOM欠落は因果判定に使わず、`inconclusive` とする。

### 2. proxy契約後の2×2比較

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

この4セルはbrowser modeとegressの寄与を支持・反証する比較材料になる。ただし各セル1回の逐次観測なので、時刻差、rate rule、proxy出口差は残る。確定には低頻度での一貫した再現、または対象Web ACLログが必要である。

### 3. Zyteを1回だけprobe

`RECORDCITY_ZYTE_API_KEY` をRender secretとして設定後に実行する。

```bash
flask --app app:app recordcity-probe \
  https://www.recordcity.jp/catalog/4936480 \
  --strategy zyte \
  --timeout-seconds 60 \
  --delay-seconds 5 \
  --allow-external
```

成功後にのみproductionを切り替える。

```text
RECORDCITY_FETCH_PROVIDER=zyte
RECORDCITY_ZYTE_API_KEY=<Render secret>
```

### 4. ScraperAPIをprobe

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

## 推奨順序

1. この変更をデプロイし、無料の4セル比較を1回だけ実行する。
2. UAまたはheadfulだけが成功した場合、そのRecord City専用profileをproduction候補にする。商品1件と一覧1件を低頻度で確認してから切り替える。
3. 同一egressの3 browserセルがすべて403なら、headlessだけを原因としない。Zyteを1回probeする。
4. 原因そのものを確定する必要がある場合は、独立JP egressを用意して2×2を実行する。外部API単独成功ではIPとfingerprintを同時に変えるため、原因は分離できない。
5. ZyteもScraperAPIも、検証済みProduct JSON-LDを返さない場合は、その時点の利用可能経路では到達不能と報告する。成功に見える200やCookieだけを採用条件にしない。

## 現時点で未完了の実証

新しいheadless-UA/headful/external probeはまだRenderへデプロイしていないため、実サイトでの結果はない。現時点の推奨実装は「突破できた」と主張するものではない。次の1デプロイでは同一egress上のbrowser要因を比較し、差が出た場合だけその寄与を支持できる。差が出なければ、独立egressを使った2×2比較が必要である。

現在のRenderには `RECORDCITY_*` の外部provider key/proxyが設定されていない。このまま次のデプロイで実行できるのは無料のcurl/current/headless-UA/headfulセルだけであり、Zyte、ScraperAPI、proxyセルには別途secret/providerの準備が必要である。

今回の範囲では、依頼条件に従ってRecord Cityへの問い合わせを行わず、対応案にも含めない。
