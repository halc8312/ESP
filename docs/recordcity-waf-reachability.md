# Record City AWS WAF 到達性調査・運用手順

更新日: 2026-09-04

## 結論

Render上の同一egress・同一runtimeで比較し、その後の本番ジョブまで追跡した結果、次の7点を確認した。

1. 最初の応答は AWS WAF の **Challenge action** である。`202`、`x-amzn-waf-action: challenge`、`challenge.js` が揃っている。その後、2026-09-02の実運用の検索→詳細遷移では **CAPTCHA action** の`405`と`x-amzn-waf-action: captcha`も確認した。
2. 旧本番profileのPatchright headlessはChallenge JavaScriptを実行し、`aws-waf-token` Cookieを得た後の自動再送で`403`になった。したがって、旧profileの問題は「JavaScriptを実行できない」「Cookieが存在しない」だけではない。
3. 同じheadless、Render egress、Patchright/Chromium runtimeのまま、UAだけを`HeadlessChrome/145`から通常の`Chrome/145`へ変えた比較では、商品と一覧を一度ずつ検証できた。しかし後日の実運用では通常UAでも詳細がCAPTCHAになったため、UA修正は有効な信号だったが安定した十分条件ではない。
4. 同じRender egressで実施した`patchright-headful`の単発probeは、`202 Challenge → 302 → 200`で要求SKUのProduct JSON-LDを取得した。しかし本番へ反映後の2026-09-03には、同じheadful profileが`405 CAPTCHA`で停止した。headful化は有効な比較条件だったが、安定した十分条件ではない。
5. 最新本番ではXvfb、Patchright/Chromium headful、通常Chrome UA、`navigator.webdriver=false`、Challenge資材の取得、`aws-waf-token` Cookieまで全て確認できた。それでもCAPTCHAとなったため、ブラウザ起動不良、`webdriver=true`、`HeadlessChrome` UA、Challenge JavaScript未実行、Cookie未取得は今回の直接原因ではない。
6. `403`や`405 CAPTCHA`を発生させたAWS WAFルールがBot Controlの特定ルール、別のmanaged rule、またはカスタムルールのどれかは、クライアント側の応答だけでは確定できない。正確なルール名には対象Web ACLのログが必要である。
7. 旧headful実装は、実ブラウザ風のUAとtoken Cookieを使っていても、検索と詳細のたびに新しいincognito BrowserContextを作っていた。これは「同一セッションで一覧を閲覧して商品へ移動する」という通常の遷移と異なる。外部サービスなしで残る根拠ある最終候補として、Patchright公式の推奨形に合わせた **branded Google Chrome + persistent context + native headers/UA + headful** をRecord City専用で実装した。本番到達性はデプロイ後にのみ判定できるため、この文書では実装済み・本番未検証と明記する。

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

### 2026-09-02の再発

通常Chrome UAを本番へ反映した後の実運用ジョブ`rcdiag-20260902-a2e88ea1`では、一覧ページは成功したが、最初の商品詳細が約25秒後に`405`、`x-amzn-waf-action: captcha`となった。`aws-waf-token`は遷移前後とも存在し、`navigator.webdriver=false`、UAは`HeadlessChrome`を含まないChrome/145だった。したがって、今回は「webdriverの露出」や「headless既定UA」だけでは説明できず、パス遷移、セッション履歴、IP/ASN、頻度などを含む複合判定と整合する。

このCAPTCHAに対して自動解答や同一ジョブ内のprofile総当たりは行わない。明示的なCAPTCHAはその場で停止し、一覧の残り候補へもアクセスしない。この時点では、同一Render egressで事前にProduct JSON-LD取得を確認できたheadful profileを次期主経路として選定した。

### 2026-09-03のheadful本番結果

Render MCPで`esp-worker`のデプロイとアプリログを照合した。PR #162まで含むcommit `e760b1895513923c388c58fc6a475713e8b98017`は、2026-09-03 01:26 UTCに`live`となっている。同じinstanceで01:26:11 UTCにprivate Xvfbの起動を確認し、その後の実運用probe `07e92009`は次の結果となった。

| 条件 | 実測 |
|---|---|
| browser | `profile=patchright/chromium/headful`、通常`Chrome/145` UA |
| browser signal | `webdriver_true=false`、`headless_ua=false`、`page_errors=0` |
| Challenge通信 | script `200`、fetch `200`、通信失敗なし |
| token | `token_before=true`、`token_after=true` |
| main response | `405`、`x-amzn-waf-action: captcha`、CloudFront |
| 結果 | `RC_WAF_CAPTCHA_REQUIRED` |
| CloudFront request ID | `cL-vlrWnOY8PjEdGkk-GtUu_RsIErA44AeZKqOUl6Ucy7Mc1uN9Kkw==` |

これにより、採用したheadful実装が本番で使われていない可能性は否定された。Challenge用tokenの存在はCAPTCHA解答済みを意味せず、同じtokenを再利用してもCAPTCHA actionの要求は満たせない。残る候補は、RenderのIP/ASN・geo、残存browser fingerprint、パスやセッション履歴、頻度、またはそれらを組み合わせた判定である。クライアント側だけでは寄与を分離できないため、次の技術比較はbrowser条件を固定してegressだけを変える。

なお、`aws-waf-token` の存在は「許可」を意味しない。AWS公式には、トークンにはChallenge時刻だけでなく、ブラウザ自動化や設定不整合を含むクライアント信号が格納されるとある。また、Bot Controlはトークンを `accepted` / `rejected` / `absent` としてラベル付けする。Cookie名をクライアント側で確認できても、このラベルやトークン内部は確認できない。

## WAFモードと発火ルールの切り分け

| 問い | 判定 | 根拠 |
|---|---|---|
| Challengeか | 確定 | 202、`x-amzn-waf-action: challenge`、AWS interstitial、`challenge.js` |
| CAPTCHAか | 最新の実運用で確定 | 詳細遷移で405と`x-amzn-waf-action: captcha`を観測 |
| Challengeを実行できたか | 確定 | Challenge資材は200、tokenを取得し、過去probeでは同一profileで検証済みProductにも到達 |
| tokenがWAFにacceptedされたか | 過去の成功probeではページ到達まで確認、最新失敗では不明 | Cookie存在だけでは暗号化トークンの評価ラベルを読めない。通常UA profileは過去にtoken後の商品・一覧へ到達した |
| Bot Controlか | 不明 | 応答にmanaged rule名やlabelは出ない |
| Render IP/ASNだけが原因か | 静的な全面拒否ではないが寄与は未確定 | 同一egressで過去に成功した一方、最新headful本番はCAPTCHA。egress固定の時系列観測だけでは分離不能 |
| browser/UA要因か | 単純な2信号だけではない | 最新失敗時も`webdriver=false`かつ通常Chrome UA。残存fingerprintや行動判定までは除外できない |
| rate/path/session ruleか | 候補、ルール名は未確定 | 一覧成功後の最初の詳細だけでCAPTCHA。確定にはWAFログが必要 |

AWS Bot Controlの公開ルールには、既定Blockの `SignalAutomatedBrowser` と `SignalKnownBotDataCenter` が別々に存在する。両方が候補になり得ることは分かるが、Record Cityがこのmanaged rule groupや同じactionを採用している証拠にはならない。同一Render egressで通常UAが成功したため無条件のIP単独blockは否定できるが、複数labelを組み合わせるカスタム判定まで否定するものではない。

- [AWS WAF Bot Control rule group](https://docs.aws.amazon.com/waf/latest/developerguide/aws-managed-rule-groups-bot.html)
- [AWS WAF log fields](https://docs.aws.amazon.com/waf/latest/developerguide/logging-fields.html)
- [CloudFront 403 causes](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/http-403-permission-denied.html)

正確なルール名を確定するには、対象Web ACLの `terminatingRuleId`、`terminatingRuleType`、`ruleGroupId`、`labels`、`challengeResponse`、`clientAsn`、`ja3Fingerprint` / `ja4Fingerprint` が必要である。このリポジトリおよびRenderからそれらのログへはアクセスできないため、ルール名について推測を確定表現にしない。

## 公開事例の調査結果

### Record City固有の成功例

2026-09-04に、GitHub code/repository/issue検索、一般Web検索、Reddit（`r/webscraping`、`r/WebScrapingInsider`、`r/scrapy`、`r/selenium`、`r/puppeteer`）、Qiita、Zenn、note、teratail、公開検索可能なX投稿、The Web Scraping Club、ScrapeOps、ScrapingBee、Zyte、および公開検索に収載されたDiscord投稿を、`recordcity.jp`、商品URL、scraper/browser名、AWS WAF語との組み合わせで確認した。

結論は、**第三者が現在の`recordcity.jp`で商品HTMLまたはProduct JSON-LDの取得に成功したと、コードと実行結果の両方から再現可能に確認できる公開例は見つからなかった**、である。購入体験やGoogle/Bingの索引結果は存在するが、スクレイピング成功の証拠には数えていない。

GitHubで唯一近いものは [TomWang22/record-platform のRecordCity adapter](https://github.com/TomWang22/record-platform/blob/f5c68dec4680dcbe0b2f0add0c2dbc9bdcf83adf/services/auction-monitor/src/platforms/recordcity/adapter.ts) だった。しかし対象は`recordcity.com`、`.co.uk`、`.eu`で`recordcity.jp`ではなく、AWS WAF処理や成功fixtureがなく、同repoのテスト用説明もscraping platformはworker実行時に試すとしている。従ってRecord City Japanの成功例とは扱わない。ESP自身の過去Render probeは要求SKUのJSON-LDまで検証しており、現時点で確認できた唯一の直接実測である。

「すべて調査済み」とは表現しない。招待制・非公開Discord（Scraping Enthusiasts、Extract Data Community、Scraping in Prod、Oxylabs等）、検索未収載の投稿、個人の非公開コード、削除済み投稿は外部から網羅できない。公開範囲については上記の対象と判定基準で横断したが、新しい一次証拠が出れば再評価する。

### AWS WAFの一般事例

AWS WAFに対して一つの万能策があるという証拠は見つからなかった。採用したのは、各要因を同じデプロイ・低頻度で分離する比較設計である。

| 出典 | 実測内容 | この実装での扱い |
|---|---|---|
| [Streamlink PR #6102](https://github.com/streamlink/streamlink/pull/6102)、[#6111](https://github.com/streamlink/streamlink/pull/6111) | AWS WAFでheadlessはtoken取得失敗、headfulは成功。後にHeadlessChrome UAを通常Chromeへ変えるとheadlessも成功 | `patchright-headless-ua`を独立診断セルとして比較し、Record CityのRender実測でも同じ方向の差を再現した。後日のCAPTCHA再発後は診断profileとして維持 |
| [HEARTH PR #309](https://github.com/THORCollective/HEARTH/pull/309)、[#311](https://github.com/THORCollective/HEARTH/pull/311) | HTTPは202 Challenge、headless Playwrightで記事取得。固定待機は不安定で、DOM待機・reload・再試行で改善 | 固定sleepを成功判定に使わず、attached DOMとProduct JSON-LDを検証。無制限retryは不採用 |
| [amazon-location-cookies-service PR #17](https://github.com/borys25ol/amazon-location-cookies-service/pull/17) | TLS profileやヘッダだけでは失敗。headless Chromiumでtokenを得て同一UAのHTTPへ移送し成功。tokenは表示期限より早く失効する場合あり | token値をログせず、期限付きメモリキャッシュと制御された1回再送を維持。ただし旧profileの403には単独で効かなかった |
| [sitedobarral PR #183](https://github.com/Danbarral2019/sitedobarral/pull/183) | 同一マシンでHTTP=202、headless=403、headed=200 | Xvfb上の`patchright-headful`セルを採用 |
| [Hardcover-Sync PR #4](https://github.com/gmoran1016/Hardcover-Sync/pull/4) | headed Chromium + XvfbとCookie/UA/client hints整合で成功 | headful比較の根拠。複数変更一括のため個別因果は採用しない |
| [Stack Overflow: WAF silent challenge](https://stackoverflow.com/questions/77529521/why-does-the-aws-waf-intelligent-threat-api-silent-challenge-never-fail) | 投稿者がAWS Support回答として紹介した内容では、自動ブラウザにもtokenは発行され得て、bot判定はtoken内に入り後続でCAPTCHA等になり得る | 二次情報として扱い、`token_after=true`を成功条件にしない |
| [OpenSanctions PR #5136](https://github.com/opensanctions/opensanctions/pull/5136)、[#5535](https://github.com/opensanctions/opensanctions/pull/5535) | Zyte browser session + token再利用で一度成功したが、後に毎回202へ回帰し別データ経路へ移行 | Zyteを有力な外部経路として実装。ただし恒久安定を前提にしない |
| [transfermarkt-scraper PR #7](https://github.com/marcgarnica13/transfermarkt-scraper/pull/7) | Zyte出口ごとにAWS WAF Challenge率が異なり、出口変更で一部回収 | egress要因の実例。高頻度の出口総当たりは不採用 |
| [Patchright README](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#best-practice---use-chrome-without-fingerprint-injection) | 推奨形はbranded Chrome、persistent context、headful、no viewportで、custom headers/UAを加えない構成 | 旧headfulはChromiumかつfresh contextだったため、推奨形を満たすRecord City専用`persistent-chrome`を最終の内部候補として実装 |
| [Playwright BrowserType](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context) | persistent contextのuser data directoryにはCookieとlocal storage等のsession dataが保存され、同じdirectoryの多重起動は不可 | Record City専用runtimeを同時実行1にし、同じcontext/tabを検索→詳細で維持。他サイトのfresh context経路には適用しない |
| [ScraperAPI AWS WAF記事](https://www.scraperapi.com/blog/how-to-scrape-amazon-waf-protected-websites/) | ベンダー自身のOLX例でraw約20%、API 99%、3–5秒と主張 | 実装候補には含めるが、独立検証でないため成功率を採用判断に使わない |
| [String benchmark](https://usestring.ai/benchmark) / [raw data](https://github.com/usestring/web-data-frontier-benchmark/blob/main/official_results/benchmark-2026-08-11T22-44-25-322Z.json) | 公開rawのAWS WAF 7サイトではZyte 16/35、ScraperAPI 20/35。ただし設定は両社とも基本HTTPのみ | 「外部APIも対象依存」の反例として記録。最大能力比較には使わない |
| [ScraperAPI cost control](https://docs.scraperapi.com/control-and-optimization/cost-control) | `max_cost`超過はprovider生成403。対象statusは`sa-statuscode`で分離可能 | provider 403とRecord City 403を別reason codeにし、`sa-statuscode`がある場合だけ対象statusとして扱う |

一般事例は設計仮説の根拠であり、Record Cityでの成功保証ではない。サービス名や「stealth」という名称だけで成功を断定せず、要求SKUとJSON-LDまで検証する。

## 対応案の比較

評価はRecord Cityに対する現時点の根拠であり、一般的な製品評価ではない。

| 案 | 実現可能性 | 安定性 | 保守コスト | 判断 |
|---|---|---|---|---|
| browser_poolへ共通のUA/init scriptを追加 | 高 | 低 | 中 | 不採用。全サイトへ波及する。現行Patchrightで`webdriver=false`は既に達成済み |
| browser_poolへpersistent contextをopt-in追加 | 高 | 中 | 中 | **採用**。既存APIの既定挙動は変えず、Record City専用helper/runtime keyだけで使用 |
| Record City専用context options | 高 | 中 | 低 | **採用済み**。新profileではPatchright推奨に従い`no_viewport`以外のUA、locale、追加headerを指定しない |
| `navigator.webdriver`を隠すinit script | 高 | 低 | 低 | 現時点では不採用。Render実測で既にfalse。重ねてもこの信号は変わらない |
| Patchright headless + 既定`HeadlessChrome` UA | 実測済み | 低 | 低 | PR #155時点の旧controlでtoken後403を再現。現在の公開profileと本番経路には使わない |
| Patchright headless + 通常Chrome UA | 高 | 低～中 | 低 | 商品・一覧の成功実測後、実運用の詳細でCAPTCHAが再発。診断profileとして維持 |
| Patchright headful + Xvfb | 実装・本番確認済み | 低～中 | 中 | 単発成功後、本番でCAPTCHAが再発。旧profileは診断controlとして維持 |
| branded Chrome + persistent same-context search→detail | 高 | **本番未検証** | 中 | **最終の内部候補として採用**。公式推奨形に一致し、旧実装で失われていたCookie、local storage、service worker、tab履歴、同一origin Refererを維持 |
| token Cookie再利用 | 実装済み | 低～中 | 中 | 維持。ただし旧profileはtoken後403なので単独解ではない。token値は保存・出力しない |
| curl_cffi impersonationのみ | 高 | 低 | 低 | 診断セルとして採用。JavaScript runtimeがなくChallengeを解けないため本番推奨ではない |
| Scrapling StealthyFetcher | 高 | 未検証 | 中 | 不採用。現在の証拠からPatchrightに対する再現性ある優位を示せず、新規browser stack追加になる |
| Camoufox/nodriver/undetected-chromedriver | 中 | 未検証 | 高 | AWS WAFでの再現性ある優位を確認できず、新規browser stack追加になるため保留 |
| Zyte browserHtml | 実装済み・対象未検証 | 対象依存 | 低～中 | credentialと費用上限の承認後に、既知の商品1件だけを試す技術PoC第一候補 |
| ScraperAPI render + JP | 実装済み・対象未検証 | 対象依存 | 低～中 | Zyteが対象で失敗した場合の第二候補。契約planのJP条件を先に確認する |
| 独立JP proxyで2×2比較 | proxy契約時は高 | provider依存 | 中 | 原因分離用。browserをheadfulに固定し、Render直通とのegress-only A/Bを行う |
| キャッシュ/Wayback | 高 | 在庫・価格には不適 | 低 | 鮮度要件を満たさないため不採用 |

## 実装内容

### 次回デプロイ対象の直接browser profile

2026-09-03時点の本番は`RECORDCITY_BROWSER_PROFILE=headful`で、Xvfb上のPatchright/Chromiumをnative UA、`no_viewport`で起動していた。このprofileが正しく稼働しても405 CAPTCHAになることは実測済みである。

次回デプロイではRender workerだけを`RECORDCITY_BROWSER_PROFILE=persistent-chrome`へ変更する。Patchright公式の推奨形に合わせ、次の条件を一つのRecord City専用profileとして固定した。

- `patchright.chromium.launch_persistent_context()`
- `channel="chrome"`でbranded Google Chromeを使用
- `headless=False`、`no_viewport=True`、追加launch argsなし
- custom UA、`Accept-Language`、locale、stealth init scriptを加えずChromeのnative値を使用
- `/tmp/esp-recordcity-chrome-profile`の専用user data directoryと同じBrowserContext/tabをworker生存中維持
- 一覧成功URLを同じcontextに保持し、**同じ抽出ジョブ内だけ**詳細遷移のsame-origin Refererとして使用。別ユーザー／別ジョブの検索queryは再利用しない

`navigator.webdriver=false`はPatchrightで既に成立しているため、snkrdunk用のinit scriptはRecord Cityへ重ねない。旧headfulでもこの値はfalseだったので、このscriptだけでは最新のCAPTCHAを説明・解消できない。Xvfbは引き続き`-nolisten tcp`で外部公開せず、VNCやremote debuggingも追加しない。

portableな既定値は引き続き`headless`で、Blueprintに含まれるRender workerだけを`persistent-chrome`にする。profile directoryはRecord City専用かつ一時領域であり、Mercari、Surugaya、SNKRDUNK等とCookieやstorageを共有しない。Challenge/CAPTCHAや429に対する無制限retryも追加しない。

### Record City専用ブラウザ診断

`services/recordcity_browser_fetch.py` に、production token cache/retryから独立した1回限りのprobe profileを追加した。

- `patchright-current`: `RECORDCITY_BROWSER_PROFILE`を実行時に解決した現在の本番profile（Renderではheadful）
- `patchright-headless-ua`: 通常Chrome UA profileの互換名。PR #155時点の制御比較で使用
- `patchright-headful`: Xvfb上のheadful、native UA、no viewport
- `patchright-headful-tokyo`: headful + Asia/Tokyo
- `patchright-persistent-chrome`: Xvfb上のbranded Chrome、native fingerprint、専用persistent context
- `patchright-headless-proxy`
- `patchright-headful-proxy`

各セルは別runtime名を使うため、共有browser poolに先に作られたprofileと混線しない。返すのはstatus、WAF action、CloudFront request ID、tokenの有無、UA/browser signals、body長とSHA-256、Product JSON-LDの検証結果だけで、HTML本文とtoken値は返さない。

### 外部取得経路

`services/recordcity_external_fetch.py` を追加し、Record Cityにだけ次を提供する。

- Zyte: `browserHtml=true`、`javascript=true`、`geolocation=JP`、attached selector待機
- ScraperAPI: `render=true`、`country_code=jp`、明示routingごとの`max_cost`
- 任意HTTPS URL template: `{url}`は必ずURL encodeし、credentials/非443/fragment/`{raw_url}`を拒否
- 独立proxy: 診断専用。各redirectをRecord City allowlistで再検証し、Cookieを同一session内だけ維持

外部応答は最大12 MiBに制限し、detail pageは最終URLの商品IDとJSON-LDの`sku`が要求IDに一致した場合だけ成功とする。明示的なCAPTCHAはProduct JSON-LDが本文に残っていてもready判定より先に終端化する。provider自身の非2xxは `RC_EXTERNAL_PROVIDER_HTTP_ERROR`、対象metadataがあるChallenge/CAPTCHA/403は別のreason codeで表示する。metadataなしでブロック本文だけが返る場合は、Record Cityとproviderのどちらが発生源か断定せず `RC_EXTERNAL_BLOCK_SOURCE_AMBIGUOUS` とする。

productionは`RECORDCITY_FETCH_PROVIDER`の明示値がない限り、`RECORDCITY_BROWSER_PROFILE`で選んだRecord City専用Patchright経路を使う。次回用Render worker Blueprintは`RECORDCITY_FETCH_PROVIDER=browser`と`RECORDCITY_BROWSER_PROFILE=persistent-chrome`を固定し、その他の環境は既定のbrowser/headlessである。API keyを置いただけでは課金経路へ切り替わらない。generic proxyはproduction providerとして選べない。

### 一括probe CLI

`flask --app app:app recordcity-probe URL` を追加した。

- URLは通信前にRecord City allowlistで検証
- 1 URL、各strategy 1回、strategy間は既定5秒
- 有料/外部経路は `--allow-external` がない限りskip
- headfulはDISPLAYがなければskipし、Xvfb利用を明示
- 人間向け表とmachine-readable JSONを出力
- 結果matrixから、UA要因、browser mode要因、egress要因、両者の相互作用、またはinconclusiveを判定

### 他サイトへの影響を閉じ込めた点

- `services/browser_pool.py`にはpersistent context用の**新しいopt-in関数**を追加したが、既存の`run_browser_page_task()`は変更せず、従来どおり呼出しごとにfresh BrowserContextを作って閉じる。
- 新しい関数を呼ぶのはRecord City専用adapterの`persistent-chrome` profileだけで、専用runtime key `recordcity_persistent_chrome`と専用profile directoryを使う。
- `services/scraping_client.py`、Mercari、Surugaya、SNKRDUNKの呼出し経路と既定値は変更していない。通常browser runtimeがfresh contextを維持する回帰テストも残している。
- persistent tabへ追加したevent listenerとnavigation routeは各取得後に除去し、途中例外時はtabを捨てる。Cookie/local storageを保持しつつ、search用guardがdetailへ漏れないことをテストした。
- `recordcity_db.py`の解析、要求SKU検証、失敗reason表示は変更していない。
- DB migrationは追加していない。

### コンテナ

既存の`xvfb`、`xauth`、`tini`に加え、`patchright install chrome`でbranded Google Chromeをimageへ導入する。bundled Chromiumは他サイトと旧診断profile用に残す。Render workerは`Tini → Python/RQ`で起動し、PythonがRecord City用private Xvfbを子プロセスとして起動・停止する。XvfbはTCP listenを無効にしたままbrowser-pool cleanupまで生存する。Docker CIではpersistent ChromeをXvfb上で起動し、UAから`HeadlessChrome`が消えてUA Client Hintsに`Google Chrome` brandがあること、およびSIGTERM後のcleanupとexit 0を検証する。

## テスト結果

ローカルのPython 3.12環境で次を確認した。

```text
Record City関連 + browser runtime/pool + HTML adapter: 204 passed
全体: 1445 passed, 1 skipped, 16 warnings
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

## 次に行う追加診断

まず`persistent-chrome`変更をPR、CI、merge、`esp-worker`デプロイまで進め、通常の一覧抽出を1回だけ実行する。成功条件は一覧DOMだけではなく、同じpersistent contextから最初の詳細へ遷移し、要求SKUと一致するProduct JSON-LDを1件以上返すことである。`200`、token Cookie、一覧成功だけでは合格にしない。

この試行でも明示的な`405 CAPTCHA`になった場合、Render直通で根拠のある内部browser差分は使い切ったと判断する。CAPTCHA solver、profile総当たり、短時間の連続retryは行わず、次の比較は契約、credential、費用上限が承認された外部egressか、先方との調整に限定する。

### 独立proxyの2×2比較

`RECORDCITY_PROXY_URL` に独立したJP egressを設定した場合のみ実行する。

```bash
xvfb-run -a flask --app app:app recordcity-probe \
  https://www.recordcity.jp/catalog/4936480 \
  --strategy patchright-current \
  --strategy patchright-headful-proxy \
  --timeout-seconds 60 \
  --delay-seconds 5 \
  --allow-external
```

この2セルはbrowser modeをheadfulに固定し、Render直通と独立JP egressだけを比較する。proxy側だけ成功すればegress/IP要因を支持する。同じ405なら、残存fingerprint、行動、サイト側の新規session向けCAPTCHAなどが残る。ただし各セル1回の逐次観測なので、時刻差、rate rule、proxy出口差は残る。確定には低頻度での再現、または対象Web ACLログが必要である。

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

直接headful経路は既に失敗を確認済みである。Zyteが要求SKUと一致する検証済みProductを返した場合にのみproduction切替を検討する。

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

価格は2026-09-03時点。契約前に公式ページで再確認する。

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

1. 外部サービスなしの最終候補として、Record City専用`persistent-chrome`をCI後にRender workerへデプロイし、通常の一覧→詳細抽出を1回検証する。他サイトは既存のfresh context経路のままにする。
2. 既存のtoken cacheとChallenge時の制御された1回再送は維持するが、明示的なCAPTCHAまたは429は終端とし、一覧の候補巡回も直ちに停止する。
3. 成功判定は、`200`やCookieの存在ではなく、商品は最終URLの商品IDとProduct JSON-LDのSKU、一覧は一覧DOMまで検証する。1回成功しても恒久安定とは断定せず、本番profileとして観測を続ける。
4. persistent Chromeでも405 CAPTCHAなら、恒久策は先方へのAPI、商品feed、または許可リストの相談を第一候補とする。照合用にUTC時刻、対象URL、probe ID、HTTP status、WAF action、CloudFront request IDを渡せる。token値は渡さない。
5. 技術的PoCをさらに行う場合は、credentialと費用上限の承認後、既存のZyte経路で既知の商品1件を1回だけ検証する。成功時だけ一覧1件を追加検証し、両方のDOM・SKU検証に合格したproviderだけをproductionへ明示選択する。
6. Zyteが対象で失敗した場合に限りScraperAPIを第二候補とする。原因分離が目的なら、browser条件を固定した独立JP egressとの2セルA/Bを選ぶ。

## 確定範囲と残る限界

Render上では、旧`HeadlessChrome` UAがtoken後403、通常Chrome UAとheadfulが同一egressでProductを取得し、通常UAは一覧も取得するところまで実証した。その後、通常UAの実運用詳細に加え、Patchright headfulの本番ジョブも405 CAPTCHAになった。従ってUAやheadful化を恒久解とは扱わない。persistent Chromeはこの時点ではコードと非ネットワークテストまでであり、通過できるとはまだ報告しない。

一方、クライアント側からは最終403の`terminatingRuleId`やlabelsを読めないため、Bot Controlの特定ルールかカスタムルールかは未確定である。また1回ずつの低頻度probeは長期安定性を保証しない。Record City側のWAF設定やChromium fingerprintが変われば再検証が必要になる。

現在のRenderには`RECORDCITY_*`の外部provider key/proxyを設定しておらず、Zyte、ScraperAPI、proxy probeも実行していない。headful本番経路の失敗までは確定したが、別egressは未検証である。外部なしで未検証なのは、今回実装したbranded Chrome + persistent context + same-tab遷移の1案だけである。その本番結果が405 CAPTCHAなら、「同じRender egressの無人browserでは安定通過を設計できない」という結論へ更新する。
