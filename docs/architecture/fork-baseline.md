# Fork baseline and provenance

> Recorded locally on 2026-09-16. This artifact records repository identity
> for review and reproducible validation only. It does not create a Git tag,
> contact a remote, authorize a push, or enable broker access.

## Remote topology

| Remote | Recorded URL | Purpose |
| --- | --- | --- |
| `origin` | `https://github.com/Recluseheart/shioaji-pro-app.git` | User fork / canonical product repository |
| `upstream` | `https://github.com/Sinotrade/shioaji-pro-app.git` | Public Shioaji Pro source of truth |
| `legacy` | local `Autotrade` checkout | Read-only Autotrade donor; not merged as an unrelated history |

## Baseline pins

| Item | Value |
| --- | --- |
| Fork initial commit | `dda58a8fde7f151d1fb3478ed3eb27d7542a4de6` (2026-06-10) |
| Integration baseline from upstream | `af476a353e61060cdd18448cb62652f2a4b5e026` (2026-09-14) |
| Current fetched `upstream/main` at recording | `b37f7a099e6585d56dc2b055085f36626f02283f` (`v0.1.48`) |
| Shioaji HTTP version file | `v1.7.5` |
| Legacy donor HEAD at recording | `259d6d7d67dac4059a9c925f1530cedfb6ecef67` |
| License | AGPL-3.0-only |

## Integrity checksums

| File | SHA-256 |
| --- | --- |
| `LICENSE` | `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0` |
| `pnpm-lock.yaml` | `e62b15eb7034ace436b2621ba7516395f5ac0488f8e39910dd0874d0025b5c48` |
| `SHIOAJI_VERSION` | `278600f9e9030589d4eaeb7588d37f0ff3e3596b730c40bbaf8d5f394b352f26` |

## Constraints

- The baseline tag examples in the refactor plan remain uncreated. Creating a
  tag or pushing it needs separate user authorization.
- Upstream synchronization must first fetch and compare `upstream/main` on a
  dedicated `upstream-sync/<date>` branch, then rerun parity, contract and
  Autotrade E2E suites.
- This baseline proves neither Shioaji account connectivity nor simulation
  execution readiness. Runtime posture remains `SIM`, `DISARMED`, and CA
  disabled.
