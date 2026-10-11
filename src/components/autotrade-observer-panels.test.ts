import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../lib/api', () => ({ apiGet: api.get, apiPost: api.post }));

import { normalizeAutotradeOverview } from '../autotrade/client';
import { autotradeObserverStore } from '../autotrade/observer-store';
import {
    automatedOrderObservations,
    CopyableIdentity,
    DurableOrderDetail,
    AutotradeObserverPanels,
    decisionChain,
    filledText,
    filterTimeline,
    heartbeatText,
    orderObservationText,
    panelSummary,
    riskUsageRows,
    riskValueText,
    riskVerdictText,
    sortOrdersForReview,
} from './autotrade-observer-panels';

beforeEach(() => vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true));
afterEach(() => {
    vi.unstubAllGlobals();
    // The shared store keeps running briefly after the last panel unmounts;
    // each test must start from a fresh Gateway read.
    autotradeObserverStore.dispose();
});

describe('Risk limit values', () => {
    it('renders lists and multiplier maps as readable text, never [object Object]', () => {
        expect(riskValueText({ TXF: '200' })).toBe('TXF ×200');
        expect(riskValueText(['2330', '99991'])).toBe('2330、99991');
        expect(riskValueText([])).toBe('無');
        expect(riskValueText(5)).toBe('5');
    });
});

describe('Autotrade observer panel summaries', () => {
    it('filters signals, decisions, and immutable events by strategy, product, status, and time', () => {
        const overview = normalizeAutotradeOverview({
            signals: [{
                signal_id: 'signal-match', strategy_id: 'alpha', run_id: 'run-1', event_id: 'event-1',
                signal_type: 'ENTRY', symbol: '2330', side: 'BUY', intent_id: 'intent-1',
                correlation_id: 'correlation-1', status: 'RISK_REJECTED', risk_reasons: [],
                observed_at: '2026-09-15T00:00:00+00:00',
            }, {
                signal_id: 'signal-other', strategy_id: 'beta', run_id: 'run-2', event_id: 'event-2',
                signal_type: 'ENTRY', symbol: '2317', side: 'BUY', intent_id: 'intent-2',
                correlation_id: 'correlation-2', status: 'SUBMITTED', risk_reasons: [],
                observed_at: '2026-09-15T00:01:00+00:00',
            }],
            decisions: [{
                intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'STRATEGY', status: 'RISK_REJECTED',
                strategy_id: 'alpha', symbol: '2330', decided_at: '2026-09-15T00:00:10+00:00', risk_reasons: [],
            }],
            audit_events: [{
                event_id: 'audit-match', intent_id: 'intent-1', event_type: 'INTENT_EVALUATED',
                details: { strategy_id: 'alpha', symbol: '2330', status: 'RISK_REJECTED' },
                occurred_at: '2026-09-15T00:00:20+00:00',
            }],
        });

        expect(filterTimeline(overview, {
            strategy: 'alpha', symbol: '2330', status: 'RISK_REJECTED',
            from: '2026-09-15T00:00:00+00:00', to: '2026-09-15T00:00:20+00:00',
        })).toEqual({
            signals: [overview.signals[0]],
            decisions: [overview.decisions[0]],
            auditEvents: [overview.audit_events[0]],
        });
    });

    it('does not describe populated simulation limits as unavailable', () => {
        const overview = normalizeAutotradeOverview({
            risk: { limits: { max_order_quantity: 10000 } },
        });

        expect(panelSummary('risk_limits', overview, null)).toContain('已載入風控限額');
        expect(panelSummary('risk_limits', overview, null)).toContain('資料不足時拒絕新委託');
        expect(panelSummary('risk_limits', overview, null)).not.toContain('尚未載入');
    });

    it('describes durable decisions without treating them as empty data', () => {
        const overview = normalizeAutotradeOverview({
            decisions: [{
                intent_id: 'decision-1', correlation_id: 'correlation-1', source: 'MANUAL_UI',
                status: 'RISK_REJECTED', risk_reasons: ['trading_gate_not_armed'],
            }],
        });

        expect(panelSummary('signal_timeline', overview, null)).toContain('durable intent');
    });

    it('counts a durable signal as timeline evidence before its linked decision', () => {
        const overview = normalizeAutotradeOverview({
            signals: [{
                signal_id: 'signal-1', strategy_id: 'reference-fixture', run_id: 'run-1',
                event_id: 'event-1', signal_type: 'REFERENCE_SIGNAL', symbol: '2330', side: 'BUY',
                intent_id: 'intent-1', correlation_id: 'correlation-1', status: 'RISK_REJECTED',
                risk_reasons: ['trading_gate_not_armed'], observed_at: '2026-09-15T00:00:00+00:00',
            }],
        });
        expect(panelSummary('signal_timeline', overview, null)).toContain('durable signal');
    });

    it('keeps a connection error ahead of any observation summary', () => {
        expect(panelSummary('risk_limits', null, 'Gateway 尚未連線')).toBe('Gateway 尚未連線');
    });

    it('presents the latest rejected verdict with a readable reason and short correlation', () => {
        expect(riskVerdictText({
            intent_id: 'intent-1', correlation_id: 'correlation-123456', status: 'RISK_REJECTED',
            allowed: false, reasons: ['trading_gate_not_armed'], source: 'MANUAL_UI', updated_at: '',
        })).toBe('最近風控：拒絕（交易閘門尚未啟用） · 追蹤 correlat');
    });

    it('defaults Auto Orders to strategy and system sources, excluding manual intents', () => {
        const overview = normalizeAutotradeOverview({
            orders: [
                { intent_id: 'manual', correlation_id: 'c1', source: 'MANUAL_UI', status: 'RISK_REJECTED' },
                { intent_id: 'strategy', correlation_id: 'c2', source: 'STRATEGY', status: 'RISK_REJECTED' },
                { intent_id: 'system', correlation_id: 'c3', source: 'SYSTEM_TRIGGER', status: 'RISK_REJECTED' },
            ],
        });
        expect(automatedOrderObservations(overview.orders).map((order) => order.intent_id)).toEqual(['strategy', 'system']);
        expect(panelSummary('auto_orders', overview, null)).toContain('自動委託');
    });

    it('treats a masked durable SIM position as observer evidence, never demo data', () => {
        const overview = normalizeAutotradeOverview({
            positions: [{
                snapshot_id: 'snapshot-1', account_type: 'F', account_label: '***78', symbol: 'TXFZ6', side: 'SHORT',
                quantity: '1', average_price: '22000', market_price: '22010', unrealized_pnl: '-10',
                observed_at: '2026-09-16T00:00:00+00:00',
            }],
        });
        expect(panelSummary('auto_orders', overview, null)).toContain('策略歸屬持倉');
    });

    it('renders durable product and strategy attribution without pretending a target mutation has a price', () => {
        expect(orderObservationText({
            operation: 'PLACE', symbol: '2330', side: 'BUY', quantity: 2,
            limit_price: '1000', order_price_type: 'LMT', strategy_id: 'reference', run_id: 'run-1',
        })).toBe('商品：2330 · 買 2 · LMT 1000 · 策略 reference · run run-1');
        expect(orderObservationText({ operation: 'UPDATE_PRICE', symbol: '2330', side: 'BUY', quantity: 2 })).toBeNull();
        expect(orderObservationText({ operation: 'PLACE', symbol: '2330', side: 'BUY' })).toBeNull();
    });
});

describe('DurableOrderDetail', () => {
    it('shows a complete durable identity and offers a local clipboard action', async () => {
        let renderer!: ReturnType<typeof create>;
        await act(async () => {
            renderer = create(createElement(CopyableIdentity, {
                label: 'Correlation ID', value: 'correlation-1234567890',
            }));
        });
        expect(JSON.stringify(renderer.toJSON())).toContain('correlation-1234567890');
        expect(renderer.root.findByType('button').props.title).toContain('Correlation ID');
        await act(async () => renderer.unmount());
    });

    it('only reads the durable order endpoint after an explicit expansion', async () => {
        const loadObservation = vi.fn().mockResolvedValue({
            intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'MANUAL_UI',
            status: 'RISK_REJECTED', symbol: '2330', side: 'BUY', quantity: 1,
            limit_price: '1000', order_price_type: 'LMT', strategy_id: null, run_id: null,
            broker_order_id: null, operation: 'PLACE', target_order_id: null,
            operation_payload: null, instrument: null, risk_reasons: ['trading_gate_not_armed'],
            updated_at: '2026-09-15T00:00:00+00:00',
        });
        let renderer!: ReturnType<typeof create>;
        await act(async () => { renderer = create(createElement(DurableOrderDetail, { intentId: 'intent-1', loadObservation })); });
        expect(loadObservation).not.toHaveBeenCalled();

        await act(async () => {
            renderer.root.findByType('button').props.onClick();
            await Promise.resolve();
        });
        expect(loadObservation).toHaveBeenCalledWith('intent-1');
        expect(JSON.stringify(renderer.toJSON())).toContain('RISK_REJECTED');
        expect(JSON.stringify(renderer.toJSON())).toContain('交易閘門尚未啟用');
        await act(async () => renderer.unmount());
    });

    it('does not fabricate a detail after a failed durable read', async () => {
        const loadObservation = vi.fn().mockRejectedValue(new Error('offline'));
        let renderer!: ReturnType<typeof create>;
        await act(async () => { renderer = create(createElement(DurableOrderDetail, { intentId: 'intent-2', loadObservation })); });
        await act(async () => {
            renderer.root.findByType('button').props.onClick();
            await Promise.resolve();
        });
        expect(JSON.stringify(renderer.toJSON())).toContain('Gateway 未提供此 durable 記錄');
        expect(JSON.stringify(renderer.toJSON())).not.toContain('intent intent-2');
        await act(async () => renderer.unmount());
    });
});

describe('Autotrade observer capability presentation', () => {
    it('hides every strategy control in observer mode while retaining the mode explanation', async () => {
        api.get.mockResolvedValue({
            health: {
                environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0,
                enabled_strategy_count: 0, gateway_status: 'READY', capability_mode: 'OBSERVER',
            },
        });
        let renderer!: ReturnType<typeof create>;
        await act(async () => {
            renderer = create(createElement(AutotradeObserverPanels, { kind: 'strategy_control' }));
            await Promise.resolve();
            await Promise.resolve();
        });
        await vi.waitFor(() => expect(JSON.stringify(renderer.toJSON())).toContain('遠端 observer mode'));
        const output = JSON.stringify(renderer.toJSON());
        expect(output).not.toContain('啟用 Kill Switch');
        expect(output).not.toContain('驗證策略');
        expect(output).not.toContain('開始');
        await act(async () => renderer.unmount());
    });

    it('offers a targeted refresh only for an operator-visible reconciliation case', async () => {
        api.get.mockResolvedValue({
            health: {
                environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0,
                enabled_strategy_count: 0, gateway_status: 'READY', capability_mode: 'OPERATOR',
            },
            reconciliation: [{
                intent_id: 'unknown-1', correlation_id: 'correlation-1', source: 'MANUAL_UI',
                status: 'UNKNOWN', symbol: '2330', side: 'BUY', quantity: 1, limit_price: '1000',
                order_price_type: 'LMT', strategy_id: null, run_id: null, broker_order_id: 'bound-id',
                operation: 'PLACE', target_order_id: null, operation_payload: null, instrument: null,
                risk_reasons: [], updated_at: '2026-09-15T00:00:00+00:00',
                reconciliation_case: {
                    state: 'OPEN', reason: 'timeout', opened_at: '2026-09-15T00:00:00+00:00',
                    updated_at: '2026-09-15T00:00:00+00:00',
                },
            }],
        });
        let renderer!: ReturnType<typeof create>;
        await act(async () => {
            renderer = create(createElement(AutotradeObserverPanels, { kind: 'reconciliation' }));
            await Promise.resolve();
            await Promise.resolve();
        });
        await vi.waitFor(() => expect(JSON.stringify(renderer.toJSON())).toContain('重新查詢對帳'));
        expect(JSON.stringify(renderer.toJSON())).toContain('不會重送或建立委託');
        await act(async () => renderer.unmount());
    });
});

function textOf(node: { children?: unknown[] } | string): string {
    if (typeof node === 'string') return node;
    return (node.children ?? []).map((child) => textOf(child as { children?: unknown[] } | string)).join('');
}

function order(fields: Record<string, unknown>) {
    return {
        intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'STRATEGY', status: 'APPROVED',
        symbol: '2330', side: 'BUY', quantity: 1, limit_price: '1000', order_price_type: 'LMT',
        strategy_id: 'demo', run_id: 'run-1', broker_order_id: null, operation: 'PLACE',
        target_order_id: null, operation_payload: null, instrument: null, risk_reasons: [],
        updated_at: '2026-10-01T01:00:00+00:00', created_at: '2026-10-01T01:00:00+00:00',
        ...fields,
    };
}

describe('Spec panel helpers', () => {
    it('distinguishes loading from an error and from empty data', () => {
        expect(panelSummary('auto_orders', null, null)).toBe('載入中…');
        expect(panelSummary('auto_orders', null, 'Gateway 尚未連線')).toBe('Gateway 尚未連線');
        expect(panelSummary('auto_orders', normalizeAutotradeOverview({}), null)).toContain('尚無自動委託');
    });

    it('chains a filled order from trigger to fill with the broker id and average price', () => {
        const overview = normalizeAutotradeOverview({
            decisions: [order({ status: 'FILLED', allowed: true, decided_at: '2026-10-01T01:00:01+00:00' })],
            orders: [order({ status: 'FILLED', broker_order_id: 'B1', filled_quantity: 1000, avg_fill_price: '999', quantity_unit: '張' })],
            audit_events: [{ event_id: 'e1', intent_id: 'intent-1', event_type: 'BROKER_ORDER_BOUND', details: {}, occurred_at: '2026-10-01T01:00:02+00:00' }],
        });
        const chain = decisionChain(overview, overview.decisions[0]!);
        expect(chain.steps.map((step) => step.state)).toEqual(['done', 'done', 'done', 'done', 'done']);
        expect(chain.steps[2]!.text).toContain('B1');
        expect(chain.steps[4]!.text).toBe('全部成交 1000股（1張） · 均價 999');
        expect(chain.title).toContain('2330 買 1張 @ 1000');
    });

    it('shows a queued same-product order as waiting for the earlier broker acknowledgement', () => {
        const overview = normalizeAutotradeOverview({ decisions: [order({ allowed: true })], orders: [order({})] });
        const send = decisionChain(overview, overview.decisions[0]!).steps[2]!;
        expect(send.state).toBe('waiting');
        expect(send.text).toContain('同商品前一筆委託取得券商確認後才送出');
    });

    it('stops the chain at a risk rejection and at an uncertain delivery', () => {
        const rejected = normalizeAutotradeOverview({
            decisions: [order({ status: 'RISK_REJECTED', allowed: false, risk_reasons: ['trading_gate_not_armed'] })],
        });
        const steps = decisionChain(rejected, rejected.decisions[0]!).steps;
        expect(steps[1]!).toMatchObject({ state: 'failed', text: '拒絕（交易閘門尚未啟用）' });
        expect(steps.slice(2).every((step) => step.state === 'skipped')).toBe(true);

        const unknown = normalizeAutotradeOverview({ decisions: [order({ status: 'UNKNOWN', allowed: true })] });
        const send = decisionChain(unknown, unknown.decisions[0]!).steps[2]!;
        expect(send.state).toBe('failed');
        expect(send.text).toContain('不會自動重送');
    });

    it('shows fills in shares, with board lots alongside, because fills are recorded in risk units', () => {
        expect(filledText({ filled_quantity: 2000, quantity_unit: '張' })).toBe('2000股（2張）');
        expect(filledText({ filled_quantity: 500, quantity_unit: '股' })).toBe('500股');
        expect(filledText({ filled_quantity: 1, quantity_unit: '口' })).toBe('1口');
        expect(filledText({ filled_quantity: 2 })).toBe('2');
    });

    it('puts uncertain orders first, then the newest', () => {
        const sorted = sortOrdersForReview([
            order({ intent_id: 'old', created_at: '2026-10-01T01:00:00+00:00' }),
            order({ intent_id: 'new', created_at: '2026-10-01T02:00:00+00:00' }),
            order({ intent_id: 'unknown', status: 'UNKNOWN', created_at: '2026-10-01T00:00:00+00:00' }),
        ] as never[]) as { intent_id: string }[];
        expect(sorted.map((item) => item.intent_id)).toEqual(['unknown', 'new', 'old']);
    });

    it('reports remaining headroom only from a fresh account snapshot', () => {
        const limits = { max_orders_per_minute: 10, max_daily_loss: '100000', max_gross_exposure: '10000000' };
        const stale = riskUsageRows(normalizeAutotradeOverview({
            risk: { limits, usage: { orders_last_minute: 3, pending_exposure: '5000', authoritative_remaining_available: false } },
        }));
        expect(stale[0]!).toEqual({ label: '每分鐘委託數', used: '3', limit: '10', remaining: '7' });
        expect(stale[1]!.remaining).toBe('需最新帳務快照');
        const fresh = riskUsageRows(normalizeAutotradeOverview({
            risk: { limits, usage: {
                orders_last_minute: 0, pending_exposure: '5000', authoritative_remaining_available: true,
                daily_loss_used: '40000', daily_loss_remaining: '60000',
                gross_exposure: '2000000', gross_exposure_remaining: '7995000',
            } },
        }));
        expect(fresh[1]!).toMatchObject({ used: '40000', remaining: '60000' });
        expect(fresh[2]!).toMatchObject({ used: '2000000 ＋ 5000', remaining: '7995000' });
    });

    it('warns when a running strategy stops sending heartbeats', () => {
        const now = Date.parse('2026-10-01T01:01:00+00:00');
        const run = {
            run_id: 'run-1', strategy_id: 'demo', version: 'v1', config_checksum: null, state: 'RUNNING',
            last_intent_id: null, last_status: null, started_at: null, ended_at: null,
            updated_at: '2026-10-01T01:00:00+00:00', intent_count: 0,
        };
        expect(heartbeatText({ ...run, heartbeat_at: '2026-10-01T01:00:55+00:00' }, 'RUNNING', now)).toEqual({ text: '5 秒前', stale: false });
        expect(heartbeatText({ ...run, heartbeat_at: '2026-10-01T01:00:00+00:00' }, 'RUNNING', now).stale).toBe(true);
        expect(heartbeatText({ ...run, heartbeat_at: '2026-10-01T01:00:00+00:00' }, 'PAUSED', now).stale).toBe(false);
    });
});

describe('Spec panel interactions', () => {
    const health = {
        api_version: 2, environment: 'PAPER', trading_gate: 'DISARMED', strategy_count: 1,
        enabled_strategy_count: 0, gateway_status: 'READY', capability_mode: 'OPERATOR',
    };

    async function render(kind: Parameters<typeof AutotradeObserverPanels>[0]['kind'], overview: unknown, onSelectCode?: (code: string) => void) {
        api.get.mockImplementation(async (path: string) => {
            if (path === '/api/v1/autotrade/overview') return overview;
            if (path === '/api/v1/autotrade/runtime') return { mode: 'PAPER', execution_available: true, running: true, last_tick: null, last_error: null, trading_gate: 'DISARMED' };
            return { config: { quantity: 1 }, config_schema: [{ name: 'quantity', label: '數量', type: 'integer' }] };
        });
        let renderer!: ReturnType<typeof create>;
        await act(async () => {
            renderer = create(createElement(AutotradeObserverPanels, { kind, onSelectCode }));
            await Promise.resolve();
        });
        await vi.waitFor(() => expect(JSON.stringify(renderer.toJSON())).not.toContain('載入中'));
        return renderer;
    }

    function button(renderer: ReturnType<typeof create>, label: string) {
        const found = renderer.root.findAllByType('button').find((node) => textOf(node as never) === label);
        if (!found) throw new Error(`button ${label} not found`);
        return found;
    }

    it('flags uncertain auto orders first and links a row to the K-line', async () => {
        const onSelectCode = vi.fn();
        const renderer = await render('auto_orders', {
            health: { ...health, environment: 'SIM' },
            orders: [order({ intent_id: 'ok', symbol: '2317' }), order({ intent_id: 'lost', status: 'UNKNOWN', account_id: 'SIM-001' })],
        }, onSelectCode);
        const output = JSON.stringify(renderer.toJSON());
        expect(output).toContain('筆委託無法確定是否已到券商');
        expect(output).toContain('帳號 SIM-001');
        await act(async () => button(renderer, '在 K 線顯示').props.onClick());
        expect(onSelectCode).toHaveBeenCalledWith('2330');
        await act(async () => renderer.unmount());
    });

    it('offers no K-line link in PAPER, which has no broker market data', async () => {
        const renderer = await render('auto_orders', { health, orders: [order({})] }, vi.fn());
        expect(renderer.root.findAllByType('button').some((node) => textOf(node as never) === '在 K 線顯示')).toBe(false);
        expect(textOf(renderer.toJSON() as never)).toContain('策略 demo · 執行 run-1');
        await act(async () => renderer.unmount());
    });

    it('requires a dry run and an explicit confirmation before starting a strategy', async () => {
        api.post.mockReset();
        api.post.mockImplementation(async (path: string) => path.endsWith('/dry-run')
            ? { mode: 'DRY_RUN', risk: { allowed: true, reasons: [] }, market_ready: true, condition_met: false, proposals: [] }
            : { strategy_id: 'demo', action: 'start', reason: '測試', state: 'RUNNING', enabled: true, run_id: 'run-1234567890', applied: true });
        const renderer = await render('strategy_control', {
            health,
            strategies: [{
                strategy_id: 'demo', display_name: '示範策略', version: 'v1', state: 'DISABLED', enabled: false,
                allowed_symbols: ['2330'], config: { quantity: 1 }, config_schema: [{ name: 'quantity', label: '數量' }],
                market_driven: true, lifecycle_enabled: true,
            }],
            risk: { limits: { max_order_quantity: 10000, max_orders_per_minute: 10 } },
        });
        const reason = renderer.root.findAllByType('input').find((node) => node.props.placeholder === '閘門與策略操作的紀錄原因')!;
        await act(async () => reason.props.onChange({ target: { value: '測試' } }));
        await act(async () => { button(renderer, '開始').props.onClick(); await Promise.resolve(); });
        await vi.waitFor(() => expect(JSON.stringify(renderer.toJSON())).toContain('條件：目前不成立'));
        const output = JSON.stringify(renderer.toJSON());
        expect(output).toContain('數量：1');
        expect(output).toContain('每分鐘最多委託數 10');
        expect(api.post).toHaveBeenCalledTimes(1);
        await act(async () => { button(renderer, '確認啟動').props.onClick(); await Promise.resolve(); });
        await vi.waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
        expect(api.post.mock.calls[1]![0]).toBe('/api/v1/autotrade/strategies/demo/start');
        await act(async () => renderer.unmount());
    });

    it('records an operator reconciliation conclusion without touching the order', async () => {
        api.post.mockReset();
        api.post.mockResolvedValue({});
        const renderer = await render('reconciliation', {
            health,
            reconciliation: [{ ...order({ status: 'UNKNOWN' }), operator_note: { conclusion: '先前結論', noted_at: null } }],
        });
        expect(textOf(renderer.toJSON() as never)).toContain('人工結論：先前結論');
        const input = renderer.root.findAllByType('input').find((node) => node.props['aria-label'] === '對帳結論')!;
        await act(async () => input.props.onChange({ target: { value: '券商查無此單' } }));
        const form = renderer.root.findAllByType('form').find((node) => node.props['aria-label'] === '記錄對帳結論')!;
        await act(async () => { form.props.onSubmit({ preventDefault: () => undefined }); await Promise.resolve(); });
        expect(api.post).toHaveBeenCalledWith('/api/v1/autotrade/reconciliation/intent-1/note', { conclusion: '券商查無此單' });
        await act(async () => renderer.unmount());
    });
});
