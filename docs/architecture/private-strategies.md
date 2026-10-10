# 私有策略外掛

策略**邏輯**屬於使用者私有資產，不進入本 repo、也不上 GitHub。Gateway 只提供載入框架：
策略模組放在任何 git 工作樹之外的資料夾，由 `AUTOTRADE_PRIVATE_STRATEGY_DIR` 指定；
資料夾位於 repo 或任何含 `.git` 的上層目錄時，Gateway 拒絕載入並停止啟動。

## 邊界

- 策略只拿到唯讀的 `MarketView`（最新成交價、成交時間、最佳買賣價）與 `PositionView`
  （股票／CB 為股數單位、期貨為口數），回傳 `OrderProposal`；拿不到 Gateway、券商連線或帳號。
- 每個提案都會轉成不可變 intent，經過共同 Risk Engine、交易閘門、durable outbox 與券商回讀。
  策略無法繞過風控，也不能自行啟用交易閘門。
- 每筆新成交（同商品、同方向、同成交時間）最多觸發一次；每個 tick 最多新增一筆委託。
  只有在成交時間落在風控時效內（一般 5 秒；`AUTOTRADE_CB_SYMBOLS` 內的商品預設 60 秒）
  才會把行情交給策略。
- 策略程式丟出例外時，只會暫停該策略並在 UI 顯示錯誤，不影響 Gateway 與既有委託。
- 期貨提案使用期貨帳戶，近月代號（`TXFR1`）會對應到實際月份合約（如 `TXFJ6`）後送出。

## 模組介面

檔名為 `<strategy_id 的 - 改成 _>.py`，並匯出 `STRATEGY`：

| 屬性／方法 | 說明 |
|---|---|
| `strategy_id`、`display_name`、`version` | 識別與顯示名稱；設定內容會另外算 checksum 併入版本 |
| `config_fields` | UI 表單欄位：`name`、`label`、`type`（`text`／`decimal`／`integer`）、`min`、`max` |
| `default_config()`、`parse_config(raw)` | 預設值與伺服器端驗證；錯誤時丟 `ValueError`（訊息會顯示在 UI） |
| `symbols(config)` | 要監看的商品；每個都必須能在 sidecar 合約表找到且在風控允許清單內 |
| `max_signals(config)` | 每次執行（start 到 stop）最多訊號數 |
| `decide(config, markets, positions)` | 回傳 `OrderProposal` 清單；數量單位為張（`lot="Common"`）、股（`IntradayOdd`）或口 |

## 啟動

```sh
.venv/bin/python scripts/dev-stack.py --mode paper \
  --strategy <strategy-id> \
  --private-strategy-dir <git 以外的私有策略資料夾> \
  --cb-symbols <CB 代號>
```

PAPER 模式下，「策略控制」面板會出現「測試用合成價格」，可手動調整價格測試策略觸發。
SIM 實際送單另需 `AUTOTRADE_ALLOW_SIMULATION_ORDERS=1` 的專用 Gateway 服務設定（見
[trading-runtime.md](trading-runtime.md)）。

## 相關風控設定（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `AUTOTRADE_CB_SYMBOLS` | 空 | 允許交易的 CB 代號；同時套用較長的成交價時效 |
| `AUTOTRADE_SLOW_QUOTE_MAX_AGE_SECONDS` | 60 | 上述商品的成交價時效（5–3600 秒） |
| `AUTOTRADE_ALLOWED_SYMBOLS` | 空 | 額外允許的股票代號 |
| `AUTOTRADE_MAX_POSITION_PER_SYMBOL` | 2000 | 每檔股票／CB 部位上限（股數單位；1 張 = 1,000） |
| `AUTOTRADE_MAX_FUTURES_ORDER_CONTRACTS` | 1 | 期貨單筆口數上限（1–10） |
| `AUTOTRADE_MAX_FUTURES_POSITION` | 2 | 同一期貨商品（各月份合計）部位上限（1–20） |

數值格式錯誤或超出範圍時 Gateway 拒絕啟動，不會默默改用預設值。帳戶永遠只來自已驗證的
SIM 連線，不能由設定指定為其他帳戶以外的值。
