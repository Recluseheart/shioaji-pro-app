# Fork UI baseline evidence — 2026-09-16

這組 baseline 記錄 fork 在沒有 Shioaji sidecar、帳號、CA 或資料庫設定時的
公開 Web 初始 workspace。它是 UI layout 與 empty/error posture 的可重現視覺
證據，不宣稱為 live market、broker read 或 simulation mutation 驗證。

| Capture | Viewport | Color scheme | SHA-256 |
| --- | --- | --- | --- |
| [desktop dark](upstream-fork-desktop-dark.png) | 1440 × 1080 | dark | `ade945459db9e8cdebf84b7a448ad60ad81b3a687aa056f18cecf779f73485b7` |
| [mobile dark](upstream-fork-mobile-dark.png) | iPhone 13 | dark | `f4572993c3ccfcaad0824f9c7cb6dd2f803468b7f914c3b332f87894dd1abcba` |

## Reproduction

The screenshots were captured from the production preview generated at source
commit `9b7742d`, with a fresh browser context, `zh-TW` locale and
`Asia/Taipei` timezone:

```sh
pnpm run build
pnpm exec vite preview --host 127.0.0.1 --port 4176
pnpm exec playwright screenshot --browser chromium --color-scheme dark \
  --viewport-size "1440,1080" --lang zh-TW --timezone Asia/Taipei \
  --wait-for-selector "[data-block-id]" --wait-for-timeout 1500 --full-page \
  http://127.0.0.1:4176/ docs/architecture/baselines/upstream-fork-desktop-dark.png
pnpm exec playwright screenshot --browser chromium --color-scheme dark \
  --device "iPhone 13" --lang zh-TW --timezone Asia/Taipei \
  --wait-for-selector "[data-block-id]" --wait-for-timeout 1500 --full-page \
  http://127.0.0.1:4176/ docs/architecture/baselines/upstream-fork-mobile-dark.png
```

At capture time, no repository-local `.env`, PostgreSQL DSN or loopback
sidecar listener existed. The visible `LOST` status is therefore expected and
must not be interpreted as an authenticated account or a broker failure.

## Associated test count

`pnpm test:e2e` completed with **50 passed** across desktop and mobile. This
baseline does not replace the required fixture matrix or later visual
regression baselines for loading, data, error and stale states.
