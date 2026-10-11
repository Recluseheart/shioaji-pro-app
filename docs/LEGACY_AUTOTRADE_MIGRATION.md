# Legacy Autotrade 移植清冊

本文件記錄新產品主幹對舊 Autotrade domain 的可稽核移植狀態；它不是
broker 驗證、simulation canary 或 release evidence。

## 來源快照

- Legacy repository：本機 `legacy` remote
- Legacy commit：`3bd6b938a033568e92ef2686025a91759b54c8c4`
- Legacy branch：`codex/fix-order-remediation-v1`
- 新產品 branch：`codex/autotrade-terminal-refactor`
- 上游 Web baseline：`af476a353e61060cdd18448cb62652f2a4b5e026`

Legacy 工作樹的使用者檔案（包括未追蹤 `docs/.DS_Store`）不屬於本清冊，也
不會被讀寫或提交至新產品。

## 移植分類

| Legacy source | 分類 | 新產品位置／狀態 |
| --- | --- | --- |
| `apps/strategy_runner/reference.py` | 已以 adapter 移植 | `services/autotrade_gateway/strategy_runtime.py` 的 deterministic `reference-fixture`；預設 `DISABLED`，可顯式 SIM lifecycle opt-in，無 feed、browser signal route 或 broker capability。 |
| `apps/strategy_runner/runner.py` | 部分移植 | fixture event dedup、strategy/run identity、durable lifecycle/heartbeat 與共同 intent ingress 已移植；async market runner、callback/reconciliation worker 仍待 Phase 4。 |
| `apps/strategy_runner/persistence.py` | 已以 store adapter 移植 | SQLite／schema-scoped PostgreSQL 已保存 strategy definition、run、heartbeat 與 signal；async runner state projection 仍待 market runner。 |
| `packages/domain/models.py`、`state_machine.py` | 部分移植 | `order_core.py` 已提供 source、intent、outbox、UNKNOWN/reconciliation 不重送及 broker identity seam；durable observation 也投影 symbol/side/quantity/limit price 供唯讀 K 線自動單標記。instrument/unit/fill domain 仍待統一模型。 |
| `packages/risk/engine.py` | 部分移植 | 現行 `RiskEngine` 已移植 gate／simulation baseline，並以 `RiskContext` fail-closed 檢查 quote freshness／偏離、position、daily loss 與 gross exposure；可顯式採用 verified simulation sidecar read context，frequency 與 cooldown limits 仍待 durable read model。 |
| `apps/execution_gateway/store.py` | 部分移植 | `PostgresIntentStore` 提供 schema-scoped intent/outbox、recovery、fills、strategy state、risk usage 與唯讀 observation；broker snapshot reconciliation 仍待接入。 |
| `apps/execution_gateway/worker.py`、`service.py` | 部分移植 | `ExecutionWorker` 會從 durable journal 重建 immutable intent；loopback-only `ShioajiHttpDispatcher` 已可對隔離 mock HTTP server dispatch，且缺少 broker identity 時轉 `UNKNOWN`。Gateway 只在顯式 SIM opt-in 構造 adapter，沒有 arm/dispatch route 或 background loop；callback/reconciliation worker 與真實 mutation 未接入，不能視為可送 simulation order。 |
| `packages/broker_shioaji` | 暫不移植 | 需先取得另行授權才可建立任何 Shioaji simulation session 或 mutation；LIVE 與 CA 維持 disabled。 |

## 已驗證的不變量

- Browser HTTP intent ingress 只接受 `MANUAL_UI`，不能偽造 `STRATEGY` 或
  `SYSTEM_TRIGGER`。
- reference fixture 沒有 browser ingress；僅可由顯式 process-internal harness 啟用，且仍進入共同 coordinator／Risk Engine。
- `DISARMED` 時策略 fixture 只形成 `RISK_REJECTED` receipt，不建立 outbox。
- `UNKNOWN`／`RECONCILING` 不提供盲目 resend。
- Gateway 和 Strategy Runtime 均不持有 Shioaji credential 或 session。

## 尚未完成項目

本產品尚未完成完整 manual／strategy → broker callback → fill 的 simulation
鏈路。任何 future worker、runtime opt-in、Shioaji session、simulation canary 或
LIVE/CA 行為都必須先取得使用者另外明確授權，並以新測試與證據驗證。
