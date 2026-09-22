# 交易整合修正與本機試用（2026-09-22）

目前產品實作在 `shioaji-pro-autotrade`；舊 `Autotrade` 是歷史 donor，
舊服務的 18080/18081 健康狀態不代表新 Gateway 18082 已啟動。
本文件是此次五項問題的現況入口；歷史稽核與 soak gate 仍保留原紀錄。

## 五項修正

| 原問題 | 現在的行為 | 驗證邊界 |
| --- | --- | --- |
| 整股張數與市價單可低估風險 | 股票 Common/Fixing 以 1 張 = 1,000 股計算數量、部位、名目金額；broker payload 保持原單位。MKT/MKP 用有效行情計算單筆上限。 | 不支援的商品／單位拒絕；期貨／選擇權執行需要獨立風險模型。 |
| worker、回報與 UI receipt 未串接 | 單一程序擁有 journal；背景 worker 接手 outbox，驗證交易回讀與成交、持久化並發布事件。前端收到 intent receipt 後只輪詢 GET，不重送 POST。撤改單等待 COMPLETED。 | timeout、錯誤身分、缺失或超額成交會關閉閘門，保留 UNKNOWN 待對帳。無自動重送。 |
| 策略只有 fixture | `price-threshold` 使用同一行情／帳務 provider，支援三個股票商品、買賣、盤中零股數量、觸發門檻和每 run 訊號上限；設定版本與 lifecycle 持久化。 | PAPER 是固定合成行情；SIM 才讀既有 sidecar。重啟 PAUSED + DISARMED。 |
| 已實現 PnL／頻率限制缺失 | SIM 讀當日已實現損益，保留負值；畫面區分未知、過期與來源。每帳號預設每分鐘 30 筆、冷卻 2 秒；取消單不受頻率限制。 | 畫面是帳戶損益快照，非策略歸因報酬。PAPER 為成本均價估算，不含手續費與稅。 |
| 新服務沒有可重現啟動與文件矛盾 | `scripts/dev-stack.py` 同時管理 Gateway + Web，檢查佔用埠，僅停止自己啟動的子程序，提供 status；修正 backend 狀態文件的歷史 restore 紀錄。 | 本機 Web dev 交付；尚非 native release、遠端 CI、broker canary 或 soak 完成證據。 |

風控目前採保守的每帳號單一未結委託策略：有待送、待回報、部分成交或不明
委託時，拒絕新增 PLACE；撤單可繼續，改單重新以完整委託及最新帳務評估。
這不是可同時容納多筆未結委託的完整曝險預留模型。

## 本機 PAPER 試用

需要 Python 3.11+、uv、Node 22+ 與 pnpm。從本 worktree 執行：

```sh
uv sync --frozen --group dev
pnpm install --frozen-lockfile
.venv/bin/python scripts/dev-stack.py
```

開啟 `http://127.0.0.1:4174`。預設使用 `.runtime/paper/journal.db`；不讀取
本機券商憑證、不載入 DSN、不連 sidecar。Gateway 為 18082；Ctrl-C 停止本次
兩個子程序，保留 journal。另可執行：

```sh
.venv/bin/python scripts/dev-stack.py status
```

在「新增面板 → 策略控制」選商品、股數與門檻，儲存、驗證與 Dry Run；
填寫「操作原因」，啟用交易閘門後開始策略。預設 2330 合成價 1000、買進 1 股、門檻 ≤1000，
每 run 最多 1 個訊號。啟用期限五分鐘；可關閉閘門、暫停或停止策略。
暫停／停止不撤銷既有委託。委託、成交與訊號可在對應面板查閱。

PAPER 的券商專屬市場／帳務面板明確回覆不可用，不把假資料當作券商資料。
Kill Switch 持久化且不可由 UI 重新啟用；保留該 journal，不靠清空歷史復原。

## SIM 執行接線

`--mode sim` 只讀已由使用者管理的 loopback sidecar，仍禁止下單。
`--mode observe` 維持原本觀察模式。啟動器不啟動或重新登入 sidecar，不處理 CA。

需實際 SIM 執行時，由操作人員設定專用 Gateway 服務環境：
`AUTOTRADE_RUNTIME_MODE=SIM`、`AUTOTRADE_ALLOW_SIMULATION_ORDERS=1`、專用
`AUTOTRADE_GATEWAY_DB` 或 `AUTOTRADE_GATEWAY_DATABASE_URL` + `AUTOTRADE_GATEWAY_PG_SCHEMA`。
已有且被驗證為 simulation 的股票帳戶必須唯一；多帳戶時再指定
`AUTOTRADE_SIM_ACCOUNT_ID`。此設定仍以 DISARMED 啟動，必須在 UI 通過
最新行情、損益與回報預檢後另行啟用五分鐘。不得沿用 PAPER journal。

此次修正沒有送出券商 SIM/LIVE 委託；真實回報 payload 相容性、盤中行為與
broker canary 仍須另行驗收。日誌不得記錄憑證、DSN 或完整券商回應。

## 回歸驗證

```sh
uv run ruff check services/autotrade_gateway
uv run mypy
uv run python -m unittest discover -s services/autotrade_gateway -p 'test_*.py'
bash scripts/run_gateway_postgres_integration.sh
pnpm test
pnpm build
pnpm test:e2e
pnpm test:e2e:paper
```

PostgreSQL 腳本建立並移除專用 disposable container，不使用使用者既有資料庫。
PAPER 整合測試涵蓋張股轉換、市價上限、負損益、風控、策略、回報驗證、撤改單
回讀、重啟、單一 owner、期限跨越、sidecar 隔離與不重送。

回報欄位核對來源：[Shioaji 股票撤改單](https://sinotrade.github.io/tutor/order/Stock/)、
[盤中零股](https://sinotrade.github.io/tutor/order/IntradayOdd/)。改價使用 `modified_price`，
減量使用 `order_quantity` 與 `cancel_quantity`；只允許減量，盤中零股拒絕改價。
本 repo 釘選 sidecar 的 `/order/trades` 路徑做狀態快照（見 ADR 0002）；
不假設其提供新版 `/order/update_status` route。

## 本次驗收紀錄

2026-09-22：Python unittest 172 項通過（PostgreSQL class 在一般測試中略過，另行
執行 25 項 PostgreSQL 測試與 disposable backup/restore，均通過）；前端 361 項
通過、2 項既有 skip；TypeScript build、Vite build、ruff、mypy 通過。
既有 50 項桌面／手機 E2E 通過。新 PAPER E2E 於桌面及手機兩種 viewport 直接操作實際 Gateway，全流程通過，
未攔截或偽造交易 API。QA agent 獨立重跑 HTTP 撤改單回歸，兩項通過。

本機 Codex 內嵌瀏覽器實際核對 `PAPER` 標記、設定／Dry Run、五分鐘閘門、
策略 RUNNING → FILLED（2330 一股）→ PAUSED → RUNNING → STOPPED，最後
回到 DISARMED。該筆僅為本機合成成交，journal 保留供核對。原生殼與真實
券商登入／委託未執行；PAPER 沒有 sidecar 版本，repo pin 另見 `SHIOAJI_VERSION`。
