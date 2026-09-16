# Autotrade backend integration status

> Snapshot: 2026-09-16. This is an implementation/evidence matrix, not a
> declaration that simulation execution is enabled. The required posture is
> always `SIM`, `DISARMED`, and CA disabled.

## Gap matrix

| Area | Status | Evidence / next condition |
| --- | --- | --- |
| Gateway durable journal | Implemented | Intent, risk decision, outbox, broker identity, fill, strategy, reconciliation, audit event and broker position snapshot have SQLite and PostgreSQL stores. `UNKNOWN`/`RECONCILING` are never re-enqueued. |
| PostgreSQL schema and recovery | Implemented and disposable-verified | Forward-only migrations are checksummed in `gateway_schema_migrations`; all timestamps use `TIMESTAMPTZ`, monetary/position values use `NUMERIC(20,8)`. The disposable integration script exercises migration rerun, concurrent idempotency and an archive/list/restore drill. |
| Shioaji SIM session posture | Implemented, awaiting a sidecar session | The Gateway only observes an already logged-in loopback sidecar and publishes `DISCONNECTED`, `CONNECTING`, `AUTHENTICATED`, `MARKET_DATA_READY`, `DEGRADED` or `LOST`. It never accepts credentials, invokes login/logout, enables CA or arms trading. |
| SIM position snapshot wiring | Implemented and fixture-verified | A local operator can explicitly read verified SIM stock/futures `position_unit` facts, validate the complete response, atomically retain the full snapshot, and project only masked account labels to the UI. `GET` remains durable-only; observer mode cannot invoke refresh. |
| UI connection and positions presentation | Implemented and fixture/E2E verified | The accepted Auto Command Center keeps its layout, displays only masked account labels, and offers an explicit SIM position snapshot refresh. It retains the existing `SIM` and `DISARMED` indicators and renders an explicit empty state instead of demo positions. |
| User-owned PostgreSQL final verification | External condition | A dedicated, non-production database plus an isolated Gateway schema and DDL/DML/backup-restore permissions are required. Disposable container evidence is not substituted for this check. |
| User-owned Shioaji SIM read integration | External condition | The local sidecar must receive credentials through its secret provider and be running in simulation mode. CA must remain unset/disabled; no simulation canary or order will be sent by this work. |

## Current local observation

At the time of this snapshot, the loopback development Gateway was listening
on port 18082 but did not yet expose the newer connection endpoint (the source
Gateway health contract is now `api_version: 2`; the running process predates
it), and no sidecar was listening on port 21322. No repository-local `.env`
and no Gateway database environment variable were present. This is deliberately
recorded as an absent external dependency, not interpreted as a failed login or
unsigned account. The UI now labels that old process as upgrade-required rather
than treating its health response as fresh sidecar evidence.

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
4. Restart the local Gateway only after the current user-owned process can be
   safely replaced. It will run migrations and recovery before accepting
   requests; the resulting health must still say `SIM` and `DISARMED`.
5. In the existing Auto Command Center's `自動委託與持倉` panel, use `同步 SIM
   持倉快照` only after the connection state is authenticated. This invokes
   only a read-only sidecar query, writes a local durable projection, and does
   not enable CA, arm the Gateway, or submit/cancel an order.
6. Ask Codex to continue this Goal. The next run will validate the dedicated
   DSN, sidecar read session, Gateway connection endpoint, state recovery and
   UI/SSE behavior without enabling CA or submitting an order.
