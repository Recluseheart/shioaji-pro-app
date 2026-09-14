# Autotrade Gateway（本機開發）

目前 Gateway 是 Shioaji Pro Web UI 的唯一 Autotrade API 入口雛形。它固定
在 loopback 上運行，沒有 Shioaji session、API key、CA 或任何正式交易權限。
所有 HTTP mutation（POST、PUT、PATCH、DELETE）都回應 `403`；這是預期行為，
不是可繞過的暫時限制。唯一例外是 `POST /api/v1/autotrade/intents`：它是
broker-free 的稽核 ingress，在目前固定 `DISARMED` 的設定下只會持久化
`RISK_REJECTED` receipt，絕不建立 outbox 或觸碰 broker。

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
- 其他 `/api/*` 的 `GET`：僅轉送至本機 Shioaji HTTP sidecar（預設
  `http://127.0.0.1:21322`，可用 `SHIOAJI_HTTP_ORIGIN` 指定）。只保留
  `Accept` 與 cache validators；不轉送瀏覽器的 Authorization 或 Cookie。

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
單。

開發時 ingress 預設使用 process-local SQLite memory store；如需保留隔離測試
資料，才可設定新的 `AUTOTRADE_GATEWAY_DB` 路徑。設定
`AUTOTRADE_GATEWAY_DATABASE_URL` 與受限格式的 `AUTOTRADE_GATEWAY_PG_SCHEMA`
時，Gateway 會使用 schema-scoped PostgreSQL intent/outbox store；需要安裝
`psycopg[binary]`。兩者都不得指向既有 Autotrade database 或 backup。

## 現在尚未具備的能力

- PostgreSQL migration／持久 read model。
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
