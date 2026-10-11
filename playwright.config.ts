import { defineConfig, devices } from '@playwright/test';

const node22Path = '/opt/homebrew/opt/node@22/bin';

export default defineConfig({
    testDir: './e2e',
    timeout: 30_000,
    use: {
        baseURL: 'http://127.0.0.1:4175',
        trace: 'retain-on-failure',
    },
    projects: [
        { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
        { name: 'mobile', use: { ...devices['iPhone 13'] } },
    ],
    webServer: [
        {
            // pyproject.toml requires Python 3.11+.  Use uv so browser E2E
            // exercises the same pinned interpreter as Gateway unit and
            // PostgreSQL integration tests, not an arbitrary system python.
            command: 'uv run python -m services.autotrade_gateway.server',
            url: 'http://127.0.0.1:18082/api/v1/autotrade/health',
            reuseExistingServer: !process.env.CI,
        },
        {
            // Intentionally omit VITE_API_TARGET: this proves the public Web
            // development default cannot silently return to a direct sidecar.
            command: `PATH=${node22Path}:$PATH PORT=4175 pnpm dev`,
            url: 'http://127.0.0.1:4175',
            reuseExistingServer: !process.env.CI,
        },
    ],
});
