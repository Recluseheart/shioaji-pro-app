# Autotrade Gateway（本機開發）

目前 Gateway 是 Shioaji Pro Web UI 的唯一 Autotrade API 入口雛形。它固定
在 loopback 上運行，沒有 Shioaji session、API key、CA 或任何正式交易權限。
所有未明確列舉的 HTTP mutation（POST、PUT、PATCH、DELETE）都回應 `403`；這是
預期行為，不是可繞過的暫時限制。唯一和 order 無關的例外是後文列出的行情／trade
event subscription 與 watchlist 偏好 CRUD。`POST /api/v1/autotrade/intents` 則是
broker-free 的稽核 ingress：在目前固定 `DISARMED` 的設定下只會持久化
`RISK_REJECTED` receipt，絕不建立 outbox 或觸碰 broker。

`POST /api/v1/order/place_order` 的 stock／futures `LMT`、`MKT`、`MKP` 形狀也已
先映射到同一個 `MANUAL_UI` intent ingress；限價單才要求正的 limit price，市價
形狀不會被誤報為無效限價。在 `DISARMED` 時仍只會得到 `409 RISK_REJECTED` 與
intent/correlation receipt，絕不轉送 sidecar。這不是上游 Trade response
compatibility 的完成宣告：cancel、update、close 與實際 broker worker 尚未接通。

`POST /api/v1/order/cancel_order` 也會建立 `CANCEL` intent，但在 durable
broker-order lookup 尚未能驗證目標帳戶與 simulation ownership 前，會 fail closed
為 `RISK_REJECTED`，絕不轉送 sidecar。這是稽核入口，不是可用的取消功能。

`POST /api/v1/order/cancel_comboorder` 使用同一個 `CANCEL` intent 與 target
broker order identity 入口，因此不會另開一條未對帳的組合單 mutation 路徑；目標帳戶
尚未驗證時同樣 fail closed，絕不轉送 sidecar。

`POST /api/v1/order/update_price` 與 `update_qty` 使用相同 target-order audit
模型，並持久化 requested price／quantity；同樣因目標帳戶尚未驗證而 fail closed。

`POST /api/v1/order/place_comboorder` 已同樣建立 `MANUAL_UI` intent，並把
combo legs 原樣保留為 instrument identity；在 `DISARMED` 下只會回傳 risk
receipt，不會送往 sidecar。combo broker dispatch、callback 與取消仍未接通。

上述所有手動 audit ingress 都接受嚴格格式的 `Idempotency-Key`。相同 key 與
完全相同的 broker-relevant request 只會重播第一筆 durable receipt；若 key 已經
用於不同的商品、數量、價格、目標委託或操作內容，Gateway 會回傳 `409`，保留原始
稽核紀錄且不建立第二筆 intent。這個保護同時適用 SQLite 開發 journal 與 PostgreSQL
schema store，不是瀏覽器端去重。

Gateway 也註冊一個從 legacy 來源移植的 `reference-fixture` strategy
descriptor，供唯讀 UI 呈現策略生命週期。它固定為 `DISABLED`，沒有市場
subscription、HTTP 啟動路由或 broker capability。僅隔離測試可明確啟用 fixture
event；即使如此它仍必須走共用 intent／Risk Engine，在 Gateway 固定
`DISARMED` 時只產生 `RISK_REJECTED` audit receipt，永不建立 outbox。

策略 descriptor 與 version metadata 會存入 `strategy_definitions`／
`strategy_versions` journals，讓重新啟動後的唯讀 API 有可稽核來源；寫入 metadata
不代表啟動 runner，且不會建立 signal、intent、outbox 或 broker session。

瀏覽器的 `POST /api/v1/autotrade/intents` 僅接受 `MANUAL_UI` source。它不能
冒充 `STRATEGY` 或 `SYSTEM_TRIGGER`；這兩種來源未來只能由 Gateway process
內的 runtime 呼叫共用 coordinator，避免瀏覽器繞過 strategy/run 身分邊界。

上游 Web 端原有的 browser-side 觸價、括號單與 Agent helper 不可被誤標為
`MANUAL_UI`。在它們移植成 Gateway process 內、帶有 durable strategy/run identity
的 producer 前，這些自動發起者會在瀏覽器的 HTTP 請求前 fail closed；手動的複合
平倉拆單仍維持 `MANUAL_UI`，並只做一次父層確認。

策略控制面板可呼叫 `reference-fixture` 的 `validate` 與 `dry-run` preview。
兩者是明確 allowlist 的 broker-free routes：Validate 只回傳 descriptor 與 warnings；
Dry Run 只回傳共同 Risk Engine 的 hypothetical verdict，永不持久化 intent、建立
outbox、啟動 strategy runtime 或呼叫 sidecar。Start／Pause／Resume／Stop 仍為 disabled；
即使舊版或遠端 UI 直接呼叫其 API，Gateway 也會回傳 `403 strategy runtime is disabled`，
不會建立 intent 或連接 sidecar。

`POST /api/v1/autotrade/kill-switch` 是唯一可用的 control mutation，但它只能加嚴：
request 必須提供非空 `reason` 與有效 `Idempotency-Key`，並將 durable gate 從
`DISARMED` 轉為 `KILLED`。本 Gateway 沒有 unkill 或 arm route；重啟後仍維持
`KILLED`，且會寫入 `KILL_SWITCH_APPLIED` audit event。它不會取消既有委託、建立
intent、啟動 worker 或聯絡 sidecar。

目前已移植的 simulation risk baseline 會同時套用於手動和策略 `PLACE` intent：
固定帳戶／商品 allowlist、單筆最大數量（10,000）與單筆最大名目金額（5,000,000）。
這些限制透過 overview 的 `risk.limits` 唯讀顯示。若未來 gate 進入 `ARMED`，每筆
`PLACE` 還必須取得最新的 `RiskContext`；缺少 context、行情過期、價格偏離、預估部位、
每日損失或總曝險超限都會 fail closed，且不建立 outbox。現階段尚未有 PostgreSQL／sidecar
提供權威 context，因此不宣稱這些動態限制可用；frequency 與 cooldown 也仍待 durable
read model 移植。

## 啟動唯讀開發環境

```sh
python3 -m services.autotrade_gateway.server
PATH=/opt/homebrew/opt/node@22/bin:$PATH PORT=4174 \
  VITE_API_TARGET=http://127.0.0.1:18082 pnpm dev
```

開啟 [http://127.0.0.1:4174/](http://127.0.0.1:4174/)，從「新增面板」選擇
「自動交易」類別，或套用「完整交易終端」、「自動交易控制」、「自動交易觀察」。
Vite 只綁 `127.0.0.1`，並透過同源 `/api` proxy 連到 Gateway。

## 已提供的 read contract

- `GET /api/v1/autotrade/health`：環境、交易閘門與 Gateway 狀態。
- `GET /api/v1/autotrade/overview`：策略、訊號、訂單、風控、對帳與績效的
  聚合 read model。沒有持久資料來源時各事件陣列必須為空，不能製造 broker
  activity 來填滿 UI。`risk.last_verdict` 只投影最新 durable ingress 的
  intent、correlation、status 與 reasons，方便 UI 說明最近一次風控結果；它不會
  自行重新評估、建立 signal 或補造 broker 狀態。
- `GET /api/v1/autotrade/strategies` 與
  `GET /api/v1/autotrade/strategies/{id}`：唯讀投影目前已註冊的策略 descriptor；
  現階段僅有固定 `DISABLED` 的 `reference-fixture`，未知 id 為 `404`。這些 route
  不提供 Start、Pause、Resume 或 Stop 能力。
- `GET /api/v1/autotrade/signals`：只會回傳 durable signal records，包含 event、
  strategy/run、intent/correlation、risk verdict 與觀測時間。現階段只有隔離測試
  fixture 能建立此資料；production runtime 固定 disabled 時會正確回傳空陣列，絕不
  以 process memory 或假事件填滿。
- `GET /api/v1/autotrade/intents`：直接列出 durable intent observations；包含
  source、correlation、status、broker identity（若有）與 risk reasons，且永遠
  是唯讀資料。每筆也投影已持久化的 `symbol`、`side`、`quantity`、`limit_price`
  與 `order_price_type`，讓相同商品的 K 線可建立不可操作的觀察標記；複合商品會
  同時帶回其持久化的 `instrument.legs` identity。
- `GET /api/v1/autotrade/intents/{intent_id}` 與
  `GET /api/v1/autotrade/orders/{intent_id}`：同一 immutable observation 的 detail
  projection，供 UI 顯示／複製 correlation、risk verdict 與 broker identity；不會
  查 sidecar、改寫狀態或重送。未知 id 回傳 `404`。
- `GET /api/v1/autotrade/orders`：和 intent 使用同一個 durable journal 的 order
  projection；提供 source、correlation、risk verdict 與已持久化的 broker identity，
  不會呼叫 sidecar、重送或偽造 broker status。
- `GET /api/v1/autotrade/fills`：只列出已通過 intent/broker identity 綁定與正數
  quantity/price validation 的 durable fill facts。terminal status 本身不會被當成 fill
  來補造；目前僅 mock／未來 callback seam 可寫入，Gateway production runtime 不會
  自行產生成交資料。
- `GET /api/v1/autotrade/runs`：從獨立的 durable `strategy_runs` journal 回報策略
  lifecycle、heartbeat、intent count 與最近 intent/status；不會從 order 猜測一個 run。
  目前 production runner 仍 disabled，只有隔離 fixture harness 會寫入 `FIXTURE_COMPLETED` 記錄。
- `GET /api/v1/autotrade/reconciliation`：只列出 `UNKNOWN`、`RECONCILING` 與
  `MANUAL_REVIEW` 的 intent，並附上 durable case 的開案原因與時間。此 API 用於人工
  對帳導覽，不提供重送功能。
- `GET /api/v1/autotrade/events`：唯讀 SSE。連線時重播 durable observation，
  以 `Last-Event-ID` 繼續尚未看過的 projection，之後只通知新的本機 durable
  write；同 timestamp 的 replay 以 intent id 作次要排序，讓斷線續接的順序可重現。
  它不會轉送 sidecar、修改 broker subscription 或重送委託。
- `GET /api/v1/autotrade/decisions`：將 durable intent 與獨立的
  `risk_decisions` journal 投影為 Decision Timeline；會顯示 source、correlation、
  operation、status、allow／deny、決策時間與 risk reasons，但不會生成 signal、
  改寫 verdict 或提供 retry。
- allowlist 中的上游 `GET`（health、info、帳戶、data、monitor、活動委託／
  保留庫存 read model、watchlist）：僅轉送至本機 Shioaji HTTP sidecar（預設
  `http://127.0.0.1:21322`，可用 `SHIOAJI_HTTP_ORIGIN` 指定）。只保留
  `Accept` 與 cache validators；不轉送瀏覽器的 Authorization 或 Cookie。未列入
  的 route 一律 `404`，不會被通用 proxy 放行。accounts、usage、trade 與 reserve
  read model 等固定路徑一律 exact match；只有 `data/`、`monitor/` 這類明確的
  read-only namespace 可用 segment-safe 前綴。

第一批上游以 `POST` 表示的 read query 使用各自的 compatibility allowlist，而
不是擴大 generic proxy 或例外放行 mutation namespace：已支援 snapshots、K 線、
ticks、scanner、data lookup、trades、portfolio／風控、保留庫存與 usage；body
最大 64 KiB，且只接受 JSON object 或 array；其餘 POST query 仍未接通。市場的
aggregate SSE `GET /api/v1/stream/data`
與明確列舉的 `POST /api/v1/stream/subscribe`／`unsubscribe` 已可透過 Gateway
relay（保留 `Last-Event-ID`，不轉送瀏覽器憑證）；它們只改變行情 subscription，
不會建立 order intent 或呼叫交易 mutation。上游 capability subscriptions 也只允許
`calculated_index`、`index_contribution`、`industry_contribution`、
`index_components` 與 `scanner` 五個明確 suffix。

`POST /api/v1/auth/subscribe_trade` 是另一條獨立 allowlist 的 callback
subscription：它只把帳戶的 trade-event 訂閱 JSON 轉交本機 sidecar，移除 browser
Authorization／Cookie，且不建立 intent、outbox 或 broker order。它不是取消、改價、
下單或重送的例外通道。

期貨組合單的 `POST /api/v1/data/contracts/combo` 是另一個獨立的 broker-free
contract resolver allowlist：只接受剛好兩個 TW `FUT` 腳，且每腳只能有 canonical
contract identity 欄位。Gateway 只把這個經驗證的 JSON query 轉給 sidecar，不轉送
browser credential，也不允許 option、額外欄位或任何 order payload 混入。

上游 watchlist CRUD（建立清單、同步清單合約、增刪合約、刪除清單）同樣採用狹窄的
`/api/v1/watchlist` path／method allowlist；這些是 UI 偏好資料，並非 broker order。
Gateway 只接受 JSON object、限制 list id 字元、移除 browser credentials，且仍拒絕所有
未列舉的 mutation route。

read proxy、POST-read query、watchlist 與行情 SSE 在 upstream connection 或 socket
timeout 時都回傳不含 credential 的 `502` JSON 訊息；不會讓 handler exception 中斷
回應、把 URL／header／request body 回送給瀏覽器，或將 timeout 解讀為下單結果。

回應一律附 `Cache-Control: no-store`，以免共享快取保存交易相關狀態。

Web 的 Autotrade overview 與七個 observer panels 共享單一 5 秒唯讀 refresh
loop。暫時讀取失敗時，面板會保留最後一次成功的 durable projection，明確標為 stale，
而不是把資料顯示成空白、觸發 mutation 或嘗試重送 UNKNOWN。最後一個 observer
panel 卸載後 refresh loop 會停止。支援 `EventSource` 的 Web UI 也會訂閱上述
SSE；event 只觸發同一份 overview 的唯讀 reload，SSE 中斷時仍使用 polling fallback。
在「自動委託與持倉」或「對帳佇列」展開某一筆資料的「查看可稽核詳情」時，Web
只會以其 durable intent id 向 `GET /api/v1/autotrade/orders/{intent_id}` 重新讀取投影。
按鈕不會呼叫 sidecar 或 broker；`404`、格式不符與網路失敗都明確顯示為沒有可用
durable 詳情，不能用 overview 快取或瀏覽器資料補造委託。

「執行與績效」面板只顯示 `strategy_runs` journal 所提供的 strategy/version、lifecycle
state、最近結果、heartbeat 與結束時間；缺少 heartbeat 時會明確顯示尚無持久化事實，
不會以瀏覽器時間或 order 資料推測策略正在執行。

## 訂單安全邊界

`services/autotrade_gateway/order_core.py` 是尚未連接 broker 的共用訂單
spine：MANUAL_UI、STRATEGY、SYSTEM_TRIGGER 共用 intent、correlation id、
risk verdict 與原子 outbox 寫入。它的 SQLite 實作只用於隔離測試／開發契約；
正式移植時必須以 PostgreSQL store 取代，並由唯一的 Execution Gateway worker
持有 Shioaji HTTP session。read proxy 只供相容讀取，不能成為 mutation 的繞道。

程序 restart 時，`APPROVED`、`SENDING`、`PENDING_SUBMIT`、`SUBMITTED`
等可能已送達 broker 的狀態都必須變為 `UNKNOWN`。`UNKNOWN` 只能進入
`RECONCILING`，不得直接建立新的 outbox 或重送。這項限制同時適用手動與策略
單。Gateway 建立持久化 store 後立即執行此 recovery，再開始接受 HTTP request。

reference fixture 以 event id 產生 deterministic intent id，並在接受前查詢 durable
store。runtime 重啟後收到同一 event 只會被辨識為重複事件，不能重新建立 intent、outbox
或 broker delivery。

worker 只有在 adapter 回傳不可空的 broker order identity 後，才可把 leased
outbox 標為 `DISPATCHED` 並將 intent 前進為 `PENDING_SUBMIT`；identity 會和
intent 一起持久化並出現在唯讀 observation。`broker_orders` journal 對 broker identity
設唯一約束；若同一 identity 被回報給不同 intent，第二筆 delivery 會進入 `UNKNOWN`，
不能錯誤地視為同一筆 broker order。任何 timeout、例外或缺少 identity
都維持 delivery-ambiguous，轉為 `UNKNOWN`，不會猜測成功或重送。SQLite 舊
journal 以 idempotent 欄位 migration 保留既有稽核資料；PostgreSQL 以 schema
內的 `ADD COLUMN IF NOT EXISTS` 完成同一項演進。

Execution Worker 在呼叫未來的 HTTP adapter 前，會從 durable journal 重建最初被
Risk Engine 接受的 immutable intent，而不是使用瀏覽器重送的 payload 或由欄位猜測
新 request。若 journal payload 無法嚴格重建，該 leased outbox 直接進入 `UNKNOWN`，
adapter 不會被呼叫；這與 network timeout 相同，必須經 reconciliation 才能處理。

`services/autotrade_gateway/broker_http.py` 現有一個只允許 credential-free loopback
origin 的 HTTP dispatch seam。它只接受 intent 內持久化且與操作相符的 request，並且
只有回應含有非空 `order.id` 時才回傳 broker identity；缺少 identity、無效回應或
timeout 一律由 worker 標為 `UNKNOWN`。Gateway server 現在**不會構造或啟用**這個
adapter；測試僅連接本機 mock HTTP server，沒有 Shioaji session、API key、CA 或任何
simulation broker mutation。未來若要啟用，仍須另行完成唯一 session owner、target
account lookup、callback/reconciliation 與明確 simulation 授權。

mock 或未來 adapter 的 terminal callback 必須同時帶入 intent 與已持久化的 broker
identity，且只可由 `PENDING_SUBMIT`／`SUBMITTED` 前進到 `FILLED`、`CANCELLED`
或 `BROKER_REJECTED`。相同 terminal callback 可冪等重放；identity 不符、衝突終態
或 `UNKNOWN` callback 一律拒絕，必須走明確 reconciliation，不能藉 callback 重開派送。

若未來 callback 提供 broker-confirmed fill identity、數量、價格與時間，必須先以
`record_fill` 驗證其 order identity 再寫入 `fills` journal，並留下 `FILL_RECORDED`
audit event；partial fill 可在 terminal callback 前存在，但沒有 callback data 時不得
從 `FILLED` status 推斷數量或價格。

每筆 observation 同時帶有持久化的 `risk_reasons`。Auto Orders／對帳面板只讀
顯示它們，讓使用者可辨認為何某筆 intent 被拒絕；原因不是可由 UI 修改、重送或
繞過 Gate 的控制項。

每個 ingress 的 allow／deny verdict 也會和 intent／outbox 在同一個資料庫交易內
另寫入不可覆寫的 `risk_decisions` journal（`allowed`、reasons、決策時間）。它讓
稽核與後續 read model 不必從 UI 或可變狀態推斷風控結論；此 journal 不是重送、
改寫 Gate 或觸發 broker 的 API。

`audit_events` 另保留不可變的 `INTENT_EVALUATED`、`BROKER_ORDER_BOUND`、
`DELIVERY_UNKNOWN` 與已驗證 identity 的 `BROKER_TERMINAL_CALLBACK` 事實，分別連結
risk verdict、broker identity、delivery ambiguity 與 terminal status。相同 terminal
callback 的冪等重放不會新增 audit event；這些 journal 只記錄已發生的事實，不會把
UNKNOWN 推測成成交或觸發派送。

K 線只將同商品、具有有效限價的 `STRATEGY`／`SYSTEM_TRIGGER` `PLACE` intent
畫成虛線觀察標記，並顯示其方向、數量與 durable status。這些線不是 upstream
活動委託線：不能拖曳、取消或改價；`MANUAL_UI`、取消／改價／改量及無價格的
market intent 都不會被畫成價格線，以避免把觀察資料誤解為可操作的 broker 委託。

開發時 ingress 預設使用 process-local SQLite memory store；如需保留隔離測試
資料，才可設定新的 `AUTOTRADE_GATEWAY_DB` 路徑。設定
`AUTOTRADE_GATEWAY_DATABASE_URL` 與受限格式的 `AUTOTRADE_GATEWAY_PG_SCHEMA`
時，Gateway 會使用 schema-scoped PostgreSQL intent/outbox store；需要安裝
`psycopg[binary]`。兩者都不得指向既有 Autotrade database 或 backup。

## 現在尚未具備的能力

- 唯一 Shioaji HTTP session owner、實際 adapter wiring 與 1.7.4/1.7.5 end-to-end
  compatibility（目前僅有未啟用的 loopback adapter seam）。
- 任何實際或 simulation broker mutation、策略啟動、取消、平倉或自動平倉。
- callback、SSE、reconciliation snapshot 與完整 UI event chain。

因此本階段的 UI 是可操作的安全觀察介面，而不是交易啟動器。狀態固定為
`SIM`、`DISARMED`、CA disabled。

## 驗證

```sh
uv run ruff check .
uv run mypy
python3 -m unittest services.autotrade_gateway.test_order_core \
  services.autotrade_gateway.test_server
scripts/run_gateway_postgres_integration.sh
PATH=/opt/homebrew/opt/node@22/bin:$PATH pnpm test
PATH=/opt/homebrew/opt/node@22/bin:$PATH pnpm build
PATH=/opt/homebrew/opt/node@22/bin:$PATH pnpm test:e2e
```

Playwright 會在 desktop 與窄螢幕 Chromium 載入真實 Vite 頁面，透過同源
proxy 驗證 Gateway 狀態與 DISARMED 控制項；它不啟動 Shioaji server 或 broker
adapter。
