# Simulation preflight — 2026-09-15

> 取得時間：2026-09-15T06:46:12Z；目的：在可能的受控 simulation execution
> 前，保留最小化且去識別的 authority、風控與復原基線。這不是下單收據，也
> 不包含原始 broker payload、帳號、person ID、token、金額或憑證。

## Session 與 gate

| 項目 | 結果 |
| --- | --- |
| Shioaji HTTP sidecar | healthy、version `1.7.5` |
| broker environment | `simulation=true` |
| Gateway environment / gate / status | `SIM` / `DISARMED` / `READY` |
| enabled strategies | 0 |
| CA | 本次未啟用；HTTP CA expiry API 需 person ID，本預檢不讀取或保存它，故 broker CA 狀態記為 unavailable |
| `update_status` | Shioaji HTTP Server 1.7.5 OpenAPI 未提供此 route；未建立第二個 SDK session 繞過 sidecar，改以 `trades` read snapshot 作為目前可用的 broker 委託快照 |

## 去識別 broker read snapshot

| Surface | 結果 |
| --- | --- |
| accounts | 1 個 `S` 帳戶；masked identity `03***`；帳戶 signed 欄位存在，但不是 development gate |
| trades / open orders | HTTP 200；0 筆 |
| stock positions | HTTP 200；0 筆 |
| account balance | HTTP 200；合法 object response（數值未保存） |
| settlements | HTTP 200；0 筆 |
| stock trading limits | HTTP 200；九個 limit/used/available 欄位存在（數值未保存） |
| futures margin | unavailable：此 session 沒有 futures account，未以 stock identity 猜測查詢 |
| market snapshot | HTTP 200；`2330` 一筆快照，含 broker datetime 欄位 |
| datastore / stream freshness | HTTP 200；兩個 status document 均有 timestamp 或資料存放狀態欄位 |

## Application durability baseline

Gateway overview 在預檢時顯示：durable orders 為空、`UNKNOWN`／`RECONCILING`／
`MANUAL_REVIEW` inventory 為空；risk limits 的來源是
`gateway_static_simulation_v1`。目前 overview 尚未能保存 broker 的權威剩餘
額度，故 `authoritative_remaining_available` 不宣稱為 true。

同日以暫時、in-memory、`DISARMED` Gateway 實例完成真實 sidecar 的 read
proxy probe。透過 Gateway 取得 `info`、accounts，以及 `trades`、
`position_unit`、`account_balance`、`settlements`、`trading_limits` 與 `2330`
snapshot，全部為 HTTP 200 且 JSON shape 與 sidecar 相符。該實例在 probe 後
立即關閉；沒有建立 intent、outbox、broker mutation 或 background worker。

三種 local database connection setting 均已配置，但本文件不保存 DSN、資料庫
名稱、帳戶或密碼。任何 canary 前必須在專用 schema 再執行 DB health、outbox
empty、fresh quote、position、loss 與應用程式 risk limit 的即時檢查；任何一項
失敗都必須保持 `DISARMED`，且不得送單。

## 結論與限制

本證據確認可安全繼續 read-contract 與 adapter wiring 開發，不能單獨授權或證明
broker mutation。正式環境、CA activation、LIVE session 與正式下單均未執行。
