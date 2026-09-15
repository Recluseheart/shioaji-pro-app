# Development plan completion audit

> **歷史稽核狀態：PAUSED（2026-09-14）**
>
> 本文件保留「下單程式修正 v1」截至暫停時的真實證據，不再驅動後續產品功能開發。
> 新的主動開發規格為
> [`shioaji-pro-autotrade-refactor-plan.md`](shioaji-pro-autotrade-refactor-plan.md)。
> 未完成 gate 仍是未完成，不因路線切換而視為通過；新架構完成後必須建立新的驗收 marker 與 audit。

更新日期：2026-09-14；狀態：**下單程式修正 v1 為 SIM soak candidate，Goal 仍進行中**。本文件不是正式盤、CA 或 simulation canary 核准。

## 修正前證據的處置

2026-09-14 前的 407 小時 Paper Soak、0-byte backup 計數、舊 simulation canary 與同機 Tailscale 測試，全部只作為稽核起點，已被
[`order-execution-remediation-v1.md`](order-execution-remediation-v1.md) 的重新驗收規格 supersede。它們不得用於 v1 的 backup、soak、remote 或 simulation readiness gate。

先前「tailnet inventory 為 0 peers，無第二裝置」觀察也已被 2026-09-14 的獨立、已撤銷測試裝置驗證 supersede；它不是 FIX-12 的失敗證據。

## FIX 狀態

| FIX | 狀態 | 已驗證證據 | 尚缺的完成證據或限制 |
|---|---|---|---|
| 01 | TIME_GATED | atomic archive、manifest、checksum、`pg_restore --list`、post-fix restore drill | marker 後另兩個日曆日有效 backup，並滿 72 小時 |
| 02 | PASS | raw LF/resume/backlog cursor、desktop/mobile Playwright、第二裝置 Serve SSE（LF、63 ms、連續且無重複） | 無 |
| 03 | REQUIRES_USER_AUTHORIZATION | durable ingress → Risk Engine → outbox → mock broker callback terminal；lifecycle、stale heartbeat、allowlist/rate limit、PostgreSQL claim；長駐且無 broker capability 的 runner；worker-only Shioaji import guard | reference strategy runtime heartbeat 的 explicit opt-in；不需要且未執行 canary |
| 04 | REQUIRES_USER_AUTHORIZATION | baseline、`codex/fix-order-remediation-v1`、local commits、repository safety scan | 外部 review、使用者授權的 remote push/CI 與 local prerelease tag；不得自行執行 |
| 05 | PASS | Taiwan timestamp contract；Taipei/UTC browser E2E 顯示 13:26 market bar（不會誤顯為 21:26） | 無 |
| 06, 10, 11 | REQUIRES_HUMAN_ATTESTATION | lease/terminal cleanup/dedup/fault tests；candidate migration 的 PENDING=0；遮罩 legacy UNKNOWN audit 且 `replay_permitted=false`；soak report 會拒絕 UNKNOWN | 一筆 legacy UNKNOWN outbox 的人工 broker reconciliation 結論；不得重送或改寫歷史 |
| 07 | PASS | active/history snapshot + desktop/mobile E2E | 無 |
| 08 | TIME_GATED | sanitizer fixtures、launchd `Umask=0077`、候選版專屬 0600 log 目錄、不輸出內容的 runtime scan | 持續運行期間的 sanitized log retention evidence |
| 09 | REQUIRES_USER_AUTHORIZATION | unique-schema PostgreSQL test、local full mypy；PostgreSQL CI job 已定義並安裝 pinned broker dependency；本機 dedicated DB contract 3/3 | remote CI run evidence；未經授權不得 push |
| 12 | PASS | 第二台 Windows 裝置（Tailscale 1.102.4）完成 TEST-TS-00 至 09：核准前無 HTTP status、443 可達、18080/18081/54329 不可達、唯讀 GET 8/8 為 200、headers 7/7、SSE LF/63 ms/resume 無重複、五個 mutation=403 且 intent=404、Tailscale-off 不可達、六項管理面 attestation=YES、移除後不可達 | 無；只保存此最小化結論，未納入 URL、IP、主機、帳號、payload 或管理面原文 |
| 13 | PASS | Lightweight Charts 5.2.0、volume、market loading/error states、desktop/mobile E2E | 無 |

## Candidate deployment 與 post-fix soak

- 部署 runtime code：`a98b0c1`；後續文件提交不改變 runtime、schema 或 worker，不能重設 soak marker。
- 部署前後均確認 `SIM`、`DISARMED`、CA disabled；`com.autotrade.simulation-canary` 為未執行，未送任何新的 simulation 或正式委託。
- Legacy PostgreSQL migration 已保存兩筆 migration event：一筆 terminal `CANCELLED` outbox 為 `DISPATCHED`，一筆無法證實 broker handoff 的既有 `FAILED` outbox 為 `UNKNOWN`；沒有 replay。
- 最新 marker：`paper_soak.started` sequence `389270`（2026-09-14 07:37:28 UTC）。首份有效 archive 為同日 `autotrade-20260914T073728Z.dump`（backup event sequence `389273`）；manifest/checksum/`pg_restore --list` 通過。
- restore drill event sequence `389314`（2026-09-14 07:37:41 UTC）為 `SUCCESS`，還原 event count 為 `389269` 並與 manifest snapshot 一致；purpose-built target 已清除。
- 2026-09-14 後續 read-only report 的 `backup_count_valid=2`、`backup_count_invalid=29`、`unresolved_order_count=0`、`outbox_unknown_count=1`、backup exit code `0`。它仍因 duration、UNKNOWN 與不足三個日曆日 backup 而拒絕，不能宣告 readiness。

## 第 18 節 mandatory-gate evidence matrix

| Gate | 分類 | 目前可稽核結論 |
|---|---|---|
| Git | REQUIRES_USER_AUTHORIZATION | branch/local commits/safety scan 已有；仍需使用者授權的 remote push、external review 與 prerelease tag，且不可把未追蹤使用者檔案併入 commit。 |
| Static/Unit | PASS | `ruff`、`mypy`、120 passed/0 skipped 的 local evidence 已保存；文件證據變更不影響 runtime 測試範圍。 |
| PostgreSQL | REQUIRES_USER_AUTHORIZATION | dedicated local PostgreSQL contract 3/3 通過且 CI service job 已定義；需要授權 push 後的 remote CI run。 |
| Backup | TIME_GATED | 有第一份新 marker 後有效 archive 與 restore drill；仍需三個日曆日、最新 archive 時效及 72 小時。 |
| SSE | PASS | raw framing、Playwright 與第二裝置 Tailscale Serve stream 均通過；63 ms 小於 2 秒、resume 連續且無重複。 |
| Time | PASS | backend fixture 與 Taipei/UTC E2E 證明 13:26 顯示契約。 |
| Outbox | REQUIRES_HUMAN_ATTESTATION | 正常 pending=0 且 ambiguous 不重送；保守 legacy UNKNOWN 仍待人工 broker reconciliation。 |
| Strategy | REQUIRES_USER_AUTHORIZATION | mock signal-to-terminal audit chain 通過；runtime reference strategy 預設停用，啟用 heartbeat 需使用者明確 opt-in。 |
| Recovery | PASS | timeout/crash/duplicate/out-of-order callback 與 restart reconciliation fault tests 通過。 |
| UI | PASS | desktop/mobile active/history、candlestick、SSE/reconnect/polling/stale E2E 通過。 |
| Privacy | TIME_GATED | sanitizer、repository safety 與候選 runtime-log scan 通過；尚需持續 retention 證據。 |
| Remote | PASS | 第二裝置 TEST-TS-00 至 09 全數 PASS；read-only 正常、所有 mutation 拒絕，且 device removal 後不可達。 |
| Soak | REQUIRES_HUMAN_ATTESTATION | 新 marker 後 report 正確拒絕；除 72 小時與三日 backup 外，legacy UNKNOWN 必須人工結案，不能縮短或沿用舊證據。 |
| Production | PASS | CA、LIVE、正式下單均未啟用；本案沒有執行 simulation canary。 |

## Candidate local verification（2026-09-14）

- `uv run ruff check .`：通過。
- `uv run mypy packages apps`：通過，22 source files。
- `uv run pytest -ra`：120 passed、0 skipped；使用新建的 `autotrade_remediation_test` database 與 UUID schema，結束後已刪除。唯一 warning 是 Starlette `TestClient` 對 httpx 的第三方 deprecation，非 skip 或安全例外。
- `uv run python scripts/verify_repository_safety.py`：通過。
- `uv run python scripts/verify_runtime_log_safety.py`：通過，候選版專屬 log 12 份、不輸出任何 log 文字。早期 log 保留供稽核，不計入這項通過證據。
- `npm --prefix apps/web run lint`、`build`：通過；`test:e2e`：6 passed（desktop/Taipei、mobile/UTC、SSE、reconnect、polling、stale、active/history、candlestick）。
- Dedicated PostgreSQL backup/restore drill：通過；target 是 purpose-built database，驗證 event 後已清除。

## Current safety boundary

- Environment: `SIM` only.
- Trading gate: `DISARMED`.
- CA: disabled；沒有執行 CA activation path。
- LIVE session、formal order、new simulation canary：未授權且未執行。

## Open mandatory gates

FIX-01 backup/72-hour soak、FIX-03 explicit strategy opt-in、FIX-04 external review/tag、FIX-06/10/11 legacy UNKNOWN 人工稽核、FIX-08 retention、FIX-09 remote CI，及第 18 節 Soak gate 仍開放。FIX-12 已由真實第二裝置驗證完成，舊的「沒有第二裝置」敘述已 superseded。Goal 必須維持進行中，直到每個 mandatory gate 具備真實且可追溯的證據。
