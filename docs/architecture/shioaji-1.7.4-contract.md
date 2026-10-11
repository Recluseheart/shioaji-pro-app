# Shioaji 1.7.4 simulation contract findings

驗證日期：2026-08-28；環境：simulation；不送出委託。

## 驗證結果

- `Shioaji(simulation=True)` 登入成功。
- SDK version 為 `1.7.4`。
- `api.contracts.get("2330")` 可取得 TSE 股票 contract。
- `update_status()`、`list_trades()`、`list_positions()` 可在登入後作為唯讀 reconciliation 查詢。
- `set_on_tick_stk_v1_callback(...)` → `subscribe(contract, QuoteType.Tick)` → `unsubscribe(...)` 已在 simulation 對 `2330` 完成訂閱／取消訂閱往返。非交易時段沒有 callback tick 屬預期行為。
- 期貨 adapter 使用官方文件指定的 `set_on_tick_fop_v1_callback(...)` 與同一 `QuoteType.Tick` 訂閱流程；`TXFR1` 的實際 simulation callback fixture 仍待開盤時段驗證。
- 五檔資料只取 `code`、`datetime`、前五筆 `bid_price`/`bid_volume` 與 `ask_price`/`ask_volume`，再寫入本機 observer store；不保存券商 raw payload。股票與期貨分別使用 STK/FOP v1 bid/ask callback。
- `api.kbars(contract, start, end).dict()` 已對 `2330` 驗證為平行欄位：`ts`、`Open`、`High`、`Low`、`Close`、`Volume`、`Amount`；worker 只保存正規化後的 OHLCV/amount，並由 observer API 唯讀提供。
- KBar nanosecond `ts` 依 Shioaji 市場資料契約解讀為 `Asia/Taipei` market wall-clock；不得將同一 wall-clock 再標為 UTC，否則 Dashboard 會錯移 8 小時。資料庫保存 timezone-aware ISO-8601，UI 明確使用 `Asia/Taipei` 顯示。
- scope contract probe：`2330`（現貨）與一檔可轉債皆回傳 `STK`，分別位於 TSE/OTC；`TXFR1`（台指期近月）回傳 `FUT`／TAIFEX。可轉債因此使用 stock mapping，期貨使用獨立 futures mapping。
- 帳戶 callback 的正確生命週期是：`login(subscribe_trade=False)` → `set_order_callback(...)` → 每帳戶 `subscribe_trade(account)`。
- 在登入前呼叫 `set_order_callback` 會收到 `AuthError: Not authenticated`；adapter 已依此行為調整。

## 安全限制

simulation 帳戶回傳的 `signed` 不作為本專案開發閘門；開發環境固定不執行任何 `place_order`、CA 啟用或 production-mode 連線。正式盤前才需依券商程序重新在 production-mode 驗證 signed 狀態。

本修正案中，只有 `apps.execution_gateway.worker` 可以建立並持有 Shioaji simulation session。Strategy Runner 只持久化 immutable `OrderIntent` 至 `strategy_intents`；Observer API/UI 只讀。每個 intent 都必須經 Risk Engine，且 timeout、crash 或 broker id 未持久化時一律標示 UNKNOWN 後以唯讀 `update_status()`／trades snapshot 對帳，不得盲目重送。

## Fixture 策略

尚未有真實 order/deal event，因此 callback schema 使用 unit test fake event；下一次有已授權的模擬盤測試時，僅保存 allowlist 欄位：`code`、`event_type`、`status`、`seqno`、`ordno`。不可保存帳號、person id、token、原始 callback payload 或 CA 資料。

## 尚待開盤時段驗證

- 可轉債與 `TXFR1`（期貨）的即時 callback 與 K 線回傳，需在 simulation 開盤時段各自建立 sanitized fixture 後才視為市場資料支援完成。
- 現階段的完整看盤頁可選取這些商品並顯示已持久化資料；沒有資料時會明確顯示尚未取得，不會假裝即時。
- 模擬 worker 每 60 秒（可由 `AUTOTRADE_RECONCILE_SECONDS` 調整，最低 10 秒）以唯讀 `update_status()`／trades／positions 快照對帳。重啟後的未知委託會先進入 `RECONCILING`；快照沒有可匹配證據即改為 `MANUAL_REVIEW`，絕不自動重送。
