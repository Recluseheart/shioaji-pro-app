# Shioaji 1.7.4 → 1.7.5 compatibility matrix

本矩陣是 Gateway adapter 的實作界線，不是 broker mutation 或正式盤驗證。
新 fork 的 Web baseline 對應 Shioaji HTTP Server 1.7.5；legacy Python SDK
evidence 對應 1.7.4。未列為 verified 的項目不得由 component 自行猜測或轉換。

| Surface | 1.7.4 evidence | 1.7.5 evidence | Gateway disposition |
| --- | --- | --- | --- |
| `/info` environment/version | SDK simulation-only probe | public UI fixture and local read-only OpenAPI metadata | forward read-only; reject malformed/LIVE-shaped Autotrade state |
| snapshots, kbars, ticks | normalized 1.7.4 read contract | upstream Web API contract | explicit POST read allowlist; retain JSON/time/decimal shape |
| market SSE | SDK callbacks | upstream `/stream/data` contract | relay with `Last-Event-ID`; do not forward browser credentials |
| Taiwan KBar time | market wall-clock is Asia/Taipei | existing UI displays market time | preserve timezone-aware value; never relabel wall-clock as UTC |
| stock/CB/futures contract classification | 2330/11011 STK, TXFR1 FUT | UI supports stock/futures paths | map only through explicit stock/futures adapters |
| order callback schema | sanitized fake-event contract only | sanitized 1.7.5 OpenAPI fixture | mock/integration only; terminal callback requires persisted broker identity |
| broker trade reconciliation read | SDK trade status query evidence | local 1.7.5 OpenAPI: `POST /order/trades` and `POST /order/order_deal_records` accept `AccountRequest`; `GET /stream/data/order_event` is SSE | Gateway explicitly proxies both POST read routes and relays the order-event SSE without browser credentials. The local parser currently consumes only `trades`; `order_deal_records` and the event stream remain compatibility/read-only until their terminal semantics have dedicated fixtures. |
| account signed / CA | not a development gate | no new live evidence | SIM/DISARMED only; CA and LIVE disabled |
| broker order mutation | not executed | not authorized | immutable intent 可由 loopback-only HTTP adapter 送至隔離 mock worker；Gateway 未 wiring，真實 sidecar／simulation dispatch 禁止 |

## Required future evidence

- A dedicated, isolated PostgreSQL schema must run the terminal callback integration test.
- A read-only 1.7.5 sidecar fixture must cover status/error and stale/reconnect shapes before
  a version transformer is claimed complete.
- Reconciliation parser fixtures must include a matching order identity, broker-only order,
  local-only UNKNOWN, duplicate and out-of-order terminal event; a refresh must never resend.
- Any real simulation order, signed-account check, CA action, or LIVE behavior requires a
  separate user authorization and is outside this matrix.
