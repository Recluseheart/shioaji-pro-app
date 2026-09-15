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
    await expect(page.getByRole('button', { name: '啟用 Kill Switch' })).toBeVisible();
    await expect(page.getByText('唯讀觀察模式；不會送單、取消、平倉或重送 UNKNOWN。')).toBeVisible();
});

test('Signal Timeline renders a durable signal linked to its risk-rejected intent', async ({ page }) => {
    await page.route('**/api/v1/autotrade/overview', async (route) => {
        await route.fulfill({ json: {
            health: {
                environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 1,
                enabled_strategy_count: 0, gateway_status: 'READY',
            },
            signals: [{
                signal_id: 'signal-1', strategy_id: 'reference-fixture', run_id: 'run-1',
                event_id: 'event-1', signal_type: 'REFERENCE_SIGNAL', symbol: '2330', side: 'BUY',
                intent_id: 'intent-1', correlation_id: 'correlation-1', status: 'RISK_REJECTED',
                risk_reasons: ['trading_gate_not_armed'], observed_at: '2026-09-15T00:00:00+00:00',
            }],
            audit_events: [{
                event_id: 'audit-1', intent_id: 'intent-1', event_type: 'INTENT_EVALUATED',
                details: { status: 'RISK_REJECTED' }, occurred_at: '2026-09-15T00:00:01+00:00',
            }],
        } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /新增面板/ }).click();
    await page.getByRole('button', { name: /訊號與決策 策略訊號、風控決策與 intent 時間線/ }).click();
    await expect(page.getByText(/訊號 · reference-fixture · REFERENCE_SIGNAL · 2330 買 · RISK_REJECTED · correlat/)).toBeVisible();
    await expect(page.getByText('event event-1 · run run-1 · 2026-09-15T00:00:00+00:00')).toBeVisible();
    await expect(page.getByText('風控：交易閘門尚未啟用')).toBeVisible();
    await expect(page.getByText('執行事件 · INTENT_EVALUATED · intent intent-1 · 2026-09-15T00:00:01+00:00')).toBeVisible();
    await page.getByRole('textbox', { name: '篩選商品' }).fill('2317');
    await expect(page.getByText(/訊號 · reference-fixture/)).toHaveCount(0);
    await expect(page.getByText('沒有符合目前篩選條件的 durable 事件。')).toBeVisible();
    await page.getByRole('button', { name: '清除篩選' }).click();
    await expect(page.getByText(/訊號 · reference-fixture/)).toBeVisible();
    await expect(page.getByRole('button', { name: '開始' })).toHaveCount(0);
});

test('Run performance renders durable lifecycle facts without a trading request', async ({ page }) => {
    let mutationRequests = 0;
    page.on('request', (request) => {
        if (/\/api\/v1\/order\/(?:place_order|cancel_order|update_price|update_qty|place_comboorder|cancel_comboorder)(?:$|[?#])/.test(request.url())) {
            mutationRequests += 1;
        }
    });
    await page.route('**/api/v1/autotrade/overview', async (route) => {
        await route.fulfill({ json: {
            health: {
                environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 1,
                enabled_strategy_count: 0, gateway_status: 'READY',
            },
            performance: { filled_order_count: 1, realized_pnl: '0', unrealized_pnl: '0' },
            runs: [{
                run_id: 'fixture-run-1', strategy_id: 'reference-fixture', version: 'v1',
                config_checksum: 'fixture-checksum', state: 'FIXTURE_COMPLETED',
                last_status: 'RISK_REJECTED', intent_count: 1,
                heartbeat_at: '2026-09-15T00:00:01+00:00',
                ended_at: '2026-09-15T00:00:02+00:00', updated_at: '2026-09-15T00:00:02+00:00',
            }],
        } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /新增面板/ }).click();
    await page.getByText('執行績效', { exact: true }).click();
    await expect(page.getByText('已確認成交委託：1')).toBeVisible();
    await expect(page.getByText('狀態：FIXTURE_COMPLETED · 最近結果：RISK_REJECTED · 1 intents')).toBeVisible();
    await expect(page.getByText('Heartbeat：2026-09-15T00:00:01+00:00 · 結束：2026-09-15T00:00:02+00:00')).toBeVisible();
    expect(mutationRequests).toBe(0);
});

test('Reconciliation queue renders its durable case without retry controls', async ({ page }) => {
    await page.route('**/api/v1/autotrade/overview', async (route) => {
        await route.fulfill({ json: {
            health: { environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0, enabled_strategy_count: 0, gateway_status: 'READY' },
            reconciliation: [{
                intent_id: 'unknown-1', correlation_id: 'correlation-1', source: 'MANUAL_UI', status: 'UNKNOWN',
                symbol: '2330', side: 'BUY', quantity: 1, limit_price: '1000', order_price_type: 'LMT', operation: 'PLACE', risk_reasons: [],
                reconciliation_case: { state: 'OPEN', reason: 'restart_recovery', opened_at: '2026-09-15T00:00:00+00:00', updated_at: '2026-09-15T00:00:00+00:00' },
            }],
        } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /新增面板/ }).click();
    await page.getByText('對帳佇列', { exact: true }).click();
    await expect(page.getByText('對帳：restart_recovery · 開案 2026-09-15T00:00:00+00:00')).toBeVisible();
    await expect(page.getByRole('button', { name: /重送|重新送出|開始/ })).toHaveCount(0);
});

test('Auto Orders defaults to durable strategy observations and exposes manual orders only on request', async ({ page }) => {
    let detailReads = 0;
    let mutationRequests = 0;
    page.on('request', (request) => {
        if (/\/api\/v1\/order\/(?:place_order|cancel_order|update_price|update_qty|place_comboorder|cancel_comboorder)(?:$|[?#])/.test(request.url())) {
            mutationRequests += 1;
        }
    });
    await page.route('**/api/v1/autotrade/overview', async (route) => {
        await route.fulfill({ json: {
            health: {
                environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 1,
                enabled_strategy_count: 0, gateway_status: 'READY',
            },
            orders: [
                {
                    intent_id: 'automatic-intent', correlation_id: 'automatic-correlation',
                    source: 'STRATEGY', status: 'SUBMITTED', symbol: '2330', side: 'BUY',
                    quantity: 2, limit_price: '1000', order_price_type: 'LMT',
                    strategy_id: 'reference', run_id: 'run-1', operation: 'PLACE', risk_reasons: [],
                },
                {
                    intent_id: 'manual-intent', correlation_id: 'manual-correlation',
                    source: 'MANUAL_UI', status: 'RISK_REJECTED', symbol: '2330', side: 'SELL',
                    quantity: 1, limit_price: '999', order_price_type: 'LMT',
                    operation: 'PLACE', risk_reasons: ['trading_gate_not_armed'],
                },
            ],
        } });
    });
    await page.route('**/api/v1/autotrade/orders/automatic-intent', async (route) => {
        detailReads += 1;
        expect(route.request().method()).toBe('GET');
        await route.fulfill({ json: {
            intent_id: 'automatic-intent', correlation_id: 'automatic-correlation',
            source: 'STRATEGY', status: 'SUBMITTED', symbol: '2330', side: 'BUY',
            quantity: 2, limit_price: '1000', order_price_type: 'LMT',
            strategy_id: 'reference', run_id: 'run-1', operation: 'PLACE',
            broker_order_id: 'SIM-ORDER-1', risk_reasons: [],
            updated_at: '2026-09-15T00:00:00+00:00',
        } });
    });

    await page.goto('/');
    await page.getByRole('button', { name: /新增面板/ }).click();
    await page.getByText('自動委託與持倉', { exact: true }).click();
    await expect(page.getByText(/STRATEGY · 下單 · SUBMITTED · automati/)).toBeVisible();
    await expect(page.getByText('商品：2330 · 買 2 · LMT 1000 · 策略 reference · run run-1')).toBeVisible();
    await expect(page.getByText(/MANUAL_UI · 下單/)).toHaveCount(0);

    await page.getByRole('button', { name: '查看可稽核詳情' }).first().click();
    await expect(page.getByText(/intent automati · SUBMITTED · 更新 2026-09-15T00:00:00\+00:00 · broker SIM-ORDER-1/)).toBeVisible();
    await expect(page.getByText('Correlation ID：automatic-correlation')).toBeVisible();
    await expect(page.getByRole('button', { name: '複製' }).first()).toHaveAttribute('title', /完整 Intent ID/);
    expect(detailReads).toBe(1);
    expect(mutationRequests).toBe(0);

    await page.getByLabel('顯示全部委託（含手動）').check();
    await expect(page.getByText(/MANUAL_UI · 下單 · RISK_REJECTED · manual-c/)).toBeVisible();
});

test('Auto Observe preset only renders observer panels and no strategy start control', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: '版面' }).click();
    await page.getByTitle('套用預設版面「自動交易觀察」').click();

    await expect(page.getByText('自動交易總覽', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('訊號與決策時間線', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('自動委託與持倉', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('對帳佇列', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('執行績效', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: '開始' })).toHaveCount(0);
    await expect(page.getByText('唯讀觀察模式；不會送單、取消、平倉或重送 UNKNOWN。').first()).toBeVisible();
});
