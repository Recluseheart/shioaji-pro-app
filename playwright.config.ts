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
            command: 'python3 -m services.autotrade_gateway.server',
            url: 'http://127.0.0.1:18082/api/v1/autotrade/health',
            reuseExistingServer: !process.env.CI,
        },
        {
            command: `PATH=${node22Path}:$PATH PORT=4175 VITE_API_TARGET=http://127.0.0.1:18082 pnpm dev`,
            url: 'http://127.0.0.1:4175',
            reuseExistingServer: !process.env.CI,
        },
    ],
});
