# Upstream UI parity inventory

> Status: IN PROGRESS. This is an evidence index, not a declaration that a
> panel has reached parity. Source of truth for the supported block set is
> `src/lib/workspace.ts` (`BlockType` and `BLOCK_META`).

## Verification rule

Every block must eventually have deterministic fixture coverage for loading,
empty, data, error and stale states, plus a browser-level add/move/resize/
remove check. A panel being listed here only proves it is in the product
catalogue. It does **not** prove a live broker contract, a mutation flow, or
human UAT completion.

| Area | Block types | Current evidence | Remaining parity evidence |
| --- | --- | --- | --- |
| Market | `watchlist`, `movers`, `chart`, `intraday`, `intradaywall`, `depth`, `tape`, `volprofile`, `replay`, `depthmap`, `grid`, `heatmap`, `pulse`, `signals` | `BLOCK_META`; component/unit suites | Deterministic fixture matrix and desktop visual regression per block |
| Trading | `ticket`, `flash`, `combo`, `combolist` | Gateway interception tests for standard/combo place, cancel and amend routes | Two-step confirmation and complete simulation UAT; no actual order without separate authorization |
| Account | `dock`, `pnl`, `chips` | Read-proxy allowlist and UI unit suites | Fixture coverage for account-empty/error/stale and privacy visual regression |
| Derivatives | `optchain`, `stockfutures`, `warrants`, `optpnl` | `BLOCK_META`; contract/read compatibility tests | Fixture and browser contract matrix |
| Tools | `notices`, `debug`, `backtest`, `assistant` | Feature-gate rendering and upstream component suites | Browser fixture coverage; desktop-only gates must remain explicit |
| Autotrade | `auto_command`, `strategy_control`, `signal_timeline`, `auto_orders`, `risk_limits`, `reconciliation`, `run_performance` | `e2e/autotrade-observer.spec.ts`, `src/autotrade/*.test.ts`, Gateway durable-state tests | Full strategy form fixtures, visual regression and authorized simulation UAT |

## Current browser evidence

- `e2e/autotrade-observer.spec.ts` runs on desktop and mobile. It proves the
  six catalogue categories and their current public/Autotrade counts are
  exposed by the panel library; it adds/removes every repeatable BlockType,
  proves an existing singleton is located rather than duplicated, proves a
  linked chart can enter and leave its pinned state using a non-real contract
  fixture, proves a workspace panel can be moved and resized in both layouts,
  proves the seven Autotrade panels can be added, and confirms these observer
  presentations do not emit broker mutation requests.
- `playwright.config.ts` starts the Gateway with `uv run python`, matching the
  Python 3.11+ project runtime. It deliberately runs with the default
  `SIM`/`DISARMED` Gateway configuration.

## Required follow-up test IDs

The following IDs are deliberately retained as open work and must not be
converted to checkmarks without the named evidence:

- `PARITY-CATALOG-01`: every `BlockType` can be added, moved, resized, pinned
  where allowed, and removed in desktop and mobile layouts. Catalogue presence,
  every repeatable panel's add/remove coverage, and representative
  link → pin → link, move, and resize transitions in both layouts are now
  present; all-pinnable-panel coverage remains open.
- `PARITY-FIXTURE-01`: each public block has loading, empty, data, error and
  stale fixtures without contacting a broker.
- `PARITY-VISUAL-01`: 1440p, 1080p and narrow-screen dark/black/light visual
  baselines for the public catalogue.
- `GATEWAY-READ-175-01`: Shioaji 1.7.5 request/response fixture matrix for all
  allowlisted read routes and SSE reconnect behavior.
- `AUTOTRADE-UAT-01`: validate, dry-run, lifecycle controls, decision timeline,
  reconciliation and observer-mode denial are understandable without logs or
  SQL. It must not include a simulation mutation unless separately authorized.

## Safety boundary

The inventory does not authorize a broker mutation. Until a separately
authorized simulation UAT, the supported verification posture remains
`SIM`, `DISARMED`, CA disabled, fixture/mock/read-only browser checks only.
