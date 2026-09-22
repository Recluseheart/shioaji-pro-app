# Autotrade backend integration status

> 現況入口（2026-09-22）：[交易整合修正與本機試用](trading-runtime.md)。
> 下表保留 2026-09-16 backend 整合證據；「當時有服務」不是目前仍在運行的證據。
> 新版預設 PAPER/DISARMED 可重現啟動，SIM 執行需獨立設定與操作。

## Gap matrix

| Area | Status | Evidence / next condition |
| --- | --- | --- |
| Gateway durable journal | Implemented | Intent, risk decision, outbox, broker identity, fill, strategy, reconciliation, audit event and broker position snapshot have SQLite and PostgreSQL stores. `UNKNOWN`/`RECONCILING` are never re-enqueued. |
| PostgreSQL schema and recovery | Implemented and disposable-verified | Forward-only migrations are checksummed in `gateway_schema_migrations`; all timestamps use `TIMESTAMPTZ`, monetary/position values use `NUMERIC(20,8)`. The disposable integration script exercises migration rerun, concurrent idempotency and an archive/list/restore drill. |
| Shioaji SIM session posture | Implemented, awaiting a sidecar session | The Gateway only observes an already logged-in loopback sidecar and publishes `DISCONNECTED`, `CONNECTING`, `AUTHENTICATED`, `MARKET_DATA_READY`, `DEGRADED` or `LOST`. It never accepts credentials, invokes login/logout, enables CA or arms trading. |
| SIM position snapshot wiring | Implemented and fixture-verified | A local operator can explicitly read verified SIM stock/futures `position_unit` facts, validate the complete response, atomically retain the full snapshot, and project only masked account labels to the UI. `GET` remains durable-only; observer mode cannot invoke refresh. |
| UI connection and positions presentation | Implemented and fixture/E2E verified | The accepted Auto Command Center keeps its layout, displays only masked account labels, and offers an explicit SIM position snapshot refresh. It retains the existing `SIM` and `DISARMED` indicators and renders an explicit empty state instead of demo positions. |
| User-owned PostgreSQL final verification | Historical verification completed on 2026-09-16 | The completion-audit addendum records the purpose-built archive/list/restore drill, matching row counts for 17 tables and migration 3/3. This supersedes the earlier pending-restore note; it does not establish continuous service availability. |
| User-owned Shioaji SIM read integration | Read-only verified | The loopback sidecar reported `MARKET_DATA_READY` for one masked, signed simulation account. Gateway reads of balance, positions, trades and market subscriptions succeeded; the account had no current positions or trades, which is retained as an explicit empty state. CA remains unset/disabled and no simulation canary or order was sent. |

## Historical local observation（2026-09-16）

At that verification, the loopback sidecar listened on port 21322, loaded its local untracked
environment, and was observed only through read contracts. The replacement
Gateway listened on port 18082 with `api_version: 2`, `SIM`, `DISARMED` and an
operator-only capability. Its connection projection exposes only a masked
account label. The running Gateway uses a new schema-local durable journal:
the previously configured schema is retained unchanged after an earlier test
isolation defect recorded fixture facts (including a kill switch) there. No
history was deleted or reset; future persistent service configuration must use
the new, dedicated schema rather than attempting to re-arm the retained one.

The first full test run after loading the DSN exposed that `GatewayServerTest`
silently inherited `AUTOTRADE_GATEWAY_DATABASE_URL`; its unit fixtures could
therefore share a configured journal. The test setup now explicitly selects its
process-local SQLite store, while PostgreSQL tests opt in only through
`AUTOTRADE_GATEWAY_TEST_POSTGRES_DSN` and a UUID schema. This is a test-only
boundary fix; it does not delete or alter the retained schema.

## Safe user-owned setup when ready

1. Copy the tracked `.env.example` to an untracked `.env` and set only the
   simulation sidecar's `SJ_API_KEY` and `SJ_SEC_KEY` through the local secret
   workflow. Do not set `SJ_PRODUCTION`, `SJ_CA_PATH` or `SJ_CA_PASSWD`.
2. Start the existing local sidecar with `scripts/dev-api.sh`; it is expected
   to bind loopback port 21322. Verify its health/read endpoints locally, not
   by pasting credentials into a shell command or chat.
3. Provision a dedicated PostgreSQL test database and a new,
   non-shared schema. Put the resulting `AUTOTRADE_GATEWAY_DATABASE_URL` and
   `AUTOTRADE_GATEWAY_PG_SCHEMA` only in the local service secret environment;
   never in tracked files, a frontend variable, or logs. Grant only database
   connect plus create/use/drop objects inside that designated schema and the
   permissions needed for the restore drill.
4. Configure a fresh, non-shared `AUTOTRADE_GATEWAY_PG_SCHEMA` after any
   fixture-contaminated schema; never delete, truncate or re-arm the retained
   journal to make a health indicator look clean. Restart the local Gateway
   only after the current user-owned process can be safely replaced. It will
   run migrations and recovery before accepting requests; the resulting health
   must still say `SIM` and `DISARMED`.
5. In the existing Auto Command Center's `自動委託與持倉` panel, use `同步 SIM
   持倉快照` only after the connection state is authenticated. This invokes
   only a read-only sidecar query, writes a local durable projection, and does
   not enable CA, arm the Gateway, or submit/cancel an order.
6. Use the current [runtime guide](trading-runtime.md) and its status command to
   verify the running source/mode. Historical observations above do not authorize
   broker mutations or prove that those processes are still available.
