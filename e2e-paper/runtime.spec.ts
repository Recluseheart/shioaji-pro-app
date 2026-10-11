import { expect, test } from '@playwright/test';

test('real PAPER Gateway: configure, dry run, arm, fill, pause, resume, stop and disarm', async ({ page, request }) => {
    const runtime = await (await request.get('/api/v1/autotrade/runtime')).json();
    expect(runtime.mode).toBe('PAPER');
    const before = await (await request.get('/api/v1/autotrade/orders')).json();
    await page.goto('/');
    await expect(page.getByText('PAPER · 本機紙上交易', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /新增面板/ }).click();
    await page.getByText('策略控制', { exact: true }).click();
    await page.getByRole('textbox', { name: '操作原因', exact: true }).fill('isolated PAPER browser regression');
    await page.getByRole('button', { name: '儲存策略設定' }).click();
    await expect(page.getByText('策略設定已儲存；請先 Dry Run，再開始策略')).toBeVisible();
    await page.getByRole('button', { name: 'Dry Run', exact: true }).click();
    await expect(page.getByText(/Dry Run：不建立 intent／outbox/)).toBeVisible();
    expect((await (await request.get('/api/v1/autotrade/orders')).json()).items.length).toBe(before.items.length);
    await page.getByRole('button', { name: '啟用交易閘門', exact: true }).click();
    await expect(page.getByText('交易閘門已啟用五分鐘')).toBeVisible();
    await page.getByRole('button', { name: '開始', exact: true }).click();
    // Starting shows the settings, limits and a fresh dry run before anything runs.
    const confirmStart = page.getByRole('dialog', { name: '確認啟動策略' });
    await expect(confirmStart.getByText(/Dry Run：不建立 intent／outbox/)).toBeVisible();
    await confirmStart.getByRole('button', { name: '確認啟動', exact: true }).click();
    await expect.poll(async () => {
        const orders = await (await request.get('/api/v1/autotrade/orders')).json();
        return orders.items.filter((row: { status: string }) => row.status === 'FILLED').length;
    }, { timeout: 15000 }).toBe(before.items.filter((row: { status: string }) => row.status === 'FILLED').length + 1);
    await page.getByRole('button', { name: '暫停', exact: true }).click();
    await page.getByRole('button', { name: '確認暫停', exact: true }).click();
    await expect(page.getByText(/暫停完成：已暫停/)).toBeVisible();
    await page.getByRole('button', { name: '恢復', exact: true }).click();
    await page.getByRole('button', { name: '確認恢復', exact: true }).click();
    await expect(page.getByText(/恢復完成：運行中/)).toBeVisible();
    await page.getByRole('button', { name: '停止', exact: true }).click();
    await expect(page.getByText(/未完成委託|目前沒有未完成委託/)).toBeVisible();
    await page.getByRole('button', { name: '確認停止', exact: true }).click();
    await expect(page.getByText(/停止完成：已停止/)).toBeVisible();
    await page.getByRole('button', { name: '關閉交易閘門', exact: true }).click();
    await expect(page.getByText('交易閘門已關閉')).toBeVisible();
    expect((await (await request.get('/api/v1/autotrade/runtime')).json()).trading_gate).toBe('DISARMED');
});
