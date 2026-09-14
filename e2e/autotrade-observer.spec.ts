import { expect, test } from '@playwright/test';

test('Autotrade panel reads the loopback Gateway and exposes no mutation controls', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: /新增面板/ }).click();
    await expect(page.getByRole('button', { name: /自動交易 7/ })).toBeVisible();
    await page.getByText('自動交易總覽', { exact: true }).click();
    await expect(page.getByText('環境：SIM')).toBeVisible();
    await expect(page.getByText('交易閘門：DISARMED')).toBeVisible();
    await expect(page.getByText('Gateway READY')).toBeVisible();
    await expect(page.getByText('不會啟動策略、送出委託或重送 UNKNOWN')).toBeVisible();
});

test('Autotrade control panel is visible but cannot start a strategy while disarmed', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: /新增面板/ }).click();
    await page.getByText('策略控制', { exact: true }).click();
    await expect(page.getByRole('button', { name: '開始' })).toBeDisabled();
    await expect(page.getByText('唯讀觀察模式；不會送單、取消、平倉或重送 UNKNOWN。')).toBeVisible();
});
