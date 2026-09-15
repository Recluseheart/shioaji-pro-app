# Autotrade Gateway（本機開發）

目前 Gateway 是 Shioaji Pro Web UI 的唯一 Autotrade API 入口雛形。它固定
在 loopback 上運行，沒有 Shioaji session、API key、CA 或任何正式交易權限。
所有 HTTP mutation（POST、PUT、PATCH、DELETE）都回應 `403`；這是預期行為，
不是可繞過的暫時限制。唯一例外是 `POST /api/v1/autotrade/intents`：它是
broker-free 的稽核 ingress，在目前固定 `DISARMED` 的設定下只會持久化
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
outbox、啟動 strategy runtime 或呼叫 sidecar。Start／Pause／Resume／Stop 仍為 disabled。

目前已移植的 simulation risk baseline 會同時套用於手動和策略 `PLACE` intent：
固定帳戶／商品 allowlist、單筆最大數量（10,000）與單筆最大名目金額（5,000,000）。
這些限制透過 overview 的 `risk.limits` 唯讀顯示。quote freshness、position、daily
loss、gross exposure、頻率及 cooldown 必須等 PostgreSQL read model 移植完成後才可宣稱可用。

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
  activity 來填滿 UI。
- `GET /api/v1/autotrade/intents`：直接列出 durable intent observations；包含
  source、correlation、status、broker identity（若有）與 risk reasons，且永遠
  是唯讀資料。複合商品會同時帶回其持久化的 `instrument.legs` identity。
- `GET /api/v1/autotrade/orders`：和 intent 使用同一個 durable journal 的 order
  projection；提供 source、correlation、risk verdict 與已持久化的 broker identity，
  不會呼叫 sidecar、重送或偽造 broker status。
- `GET /api/v1/autotrade/runs`：從 durable `STRATEGY` intent 依 `run_id` 投影策略
  run；只回報 intent count、最新 intent/status 與更新時間，沒有活動時回傳空陣列。
- `GET /api/v1/autotrade/reconciliation`：只列出 `UNKNOWN`、`RECONCILING` 與
  `MANUAL_REVIEW` 的 intent。此 API 用於人工對帳導覽，不提供重送功能。
- `GET /api/v1/autotrade/decisions`：將 durable intent／risk verdict 投影為
  Decision Timeline；會顯示 source、correlation、operation、status 與 risk
  reasons，但不會生成 signal、改寫 verdict 或提供 retry。
- allowlist 中的上游 `GET`（health、info、帳戶、data、monitor、活動委託／
  保留庫存 read model、watchlist）：僅轉送至本機 Shioaji HTTP sidecar（預設
  `http://127.0.0.1:21322`，可用 `SHIOAJI_HTTP_ORIGIN` 指定）。只保留
  `Accept` 與 cache validators；不轉送瀏覽器的 Authorization 或 Cookie。未列入
  的 route 一律 `404`，不會被通用 proxy 放行。

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

回應一律附 `Cache-Control: no-store`，以免共享快取保存交易相關狀態。

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
intent 一起持久化並出現在唯讀 observation。任何 timeout、例外或缺少 identity
都維持 delivery-ambiguous，轉為 `UNKNOWN`，不會猜測成功或重送。SQLite 舊
journal 以 idempotent 欄位 migration 保留既有稽核資料；PostgreSQL 以 schema
內的 `ADD COLUMN IF NOT EXISTS` 完成同一項演進。

mock 或未來 adapter 的 terminal callback 必須同時帶入 intent 與已持久化的 broker
identity，且只可由 `PENDING_SUBMIT`／`SUBMITTED` 前進到 `FILLED`、`CANCELLED`
或 `BROKER_REJECTED`。相同 terminal callback 可冪等重放；identity 不符、衝突終態
或 `UNKNOWN` callback 一律拒絕，必須走明確 reconciliation，不能藉 callback 重開派送。

每筆 observation 同時帶有持久化的 `risk_reasons`。Auto Orders／對帳面板只讀
顯示它們，讓使用者可辨認為何某筆 intent 被拒絕；原因不是可由 UI 修改、重送或
繞過 Gate 的控制項。

開發時 ingress 預設使用 process-local SQLite memory store；如需保留隔離測試
資料，才可設定新的 `AUTOTRADE_GATEWAY_DB` 路徑。設定
`AUTOTRADE_GATEWAY_DATABASE_URL` 與受限格式的 `AUTOTRADE_GATEWAY_PG_SCHEMA`
時，Gateway 會使用 schema-scoped PostgreSQL intent/outbox store；需要安裝
`psycopg[binary]`。兩者都不得指向既有 Autotrade database 或 backup。

## 現在尚未具備的能力

- Shioaji 1.7.4/1.7.5 HTTP adapter 與唯一 session owner worker。
- 任何實際或 simulation broker mutation、策略啟動、取消、平倉或自動平倉。
- callback、SSE、reconciliation snapshot 與完整 UI event chain。

因此本階段的 UI 是可操作的安全觀察介面，而不是交易啟動器。狀態固定為
`SIM`、`DISARMED`、CA disabled。

## 驗證

```sh
python3 -m unittest services.autotrade_gateway.test_order_core \
  services.autotrade_gateway.test_server
PATH=/opt/homebrew/opt/node@22/bin:$PATH pnpm test
PATH=/opt/homebrew/opt/node@22/bin:$PATH pnpm build
PATH=/opt/homebrew/opt/node@22/bin:$PATH pnpm test:e2e
```

Playwright 會在 desktop 與窄螢幕 Chromium 載入真實 Vite 頁面，透過同源
proxy 驗證 Gateway 狀態與 DISARMED 控制項；它不啟動 Shioaji server 或 broker
adapter。
