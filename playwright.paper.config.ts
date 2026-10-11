import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
    testDir: './e2e-paper',
    workers: 1,
    timeout: 45_000,
    use: { baseURL: 'http://127.0.0.1:4176', trace: 'retain-on-failure' },
    projects: [
        { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
        { name: 'mobile', use: { ...devices['iPhone 13'] } },
    ],
    webServer: [
        {
            command: 'uv run python -m services.autotrade_gateway.server',
            env: { AUTOTRADE_RUNTIME_MODE: 'PAPER', AUTOTRADE_GATEWAY_MODE: 'OPERATOR', AUTOTRADE_GATEWAY_DATABASE_URL: '', AUTOTRADE_GATEWAY_DB: ':memory:' },
            url: 'http://127.0.0.1:18082/api/v1/autotrade/health',
            reuseExistingServer: false,
        },
        {
            command: 'PATH=/opt/homebrew/opt/node@22/bin:$PATH PORT=4176 node node_modules/vite/bin/vite.js --strictPort',
            url: 'http://127.0.0.1:4176',
            reuseExistingServer: false,
        },
    ],
});
