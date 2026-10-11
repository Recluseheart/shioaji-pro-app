import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../lib/api', () => ({ apiGet: mocks.get, apiPost: mocks.post }));

import {
    fetchAutotradeIntent,
    fetchAutotradeOrder,
    applyAutotradeKillSwitch,
    applyReferenceStrategyControl,
    refreshAutotradeReconciliation,
    refreshAutotradePositionSnapshots,
    normalizeAutotradeHealth,
    normalizeAutotradeConnection,
    normalizeAutotradeOverview,
} from './client';

beforeEach(() => {
    mocks.get.mockReset();
    mocks.post.mockReset();
});

describe('Autotrade Gateway health contract', () => {
    it('fails closed for malformed or production-shaped values', () => {
        expect(normalizeAutotradeHealth({ environment: 'LIVE', trading_gate: 'ARMED' })).toMatchObject({
            environment: 'LIVE', trading_gate: 'ARMED', gateway_status: 'UNAVAILABLE',
        });
        expect(normalizeAutotradeHealth(null)).toMatchObject({ environment: 'UNKNOWN', trading_gate: 'UNKNOWN' });
    });

    it('accepts the versioned read-only shape', () => {
        expect(normalizeAutotradeHealth({ environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 2, enabled_strategy_count: 0, gateway_status: 'READY', capability_mode: 'OPERATOR' })).toEqual({
            environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 2, enabled_strategy_count: 0, gateway_status: 'READY', capability_mode: 'OPERATOR', api_version: 0,
            broker_connection: { state: 'DISCONNECTED', accounts: [], last_updated_at: null },
        });
    });

    it('shows full account numbers but drops other sidecar metadata', () => {
        expect(normalizeAutotradeConnection({
                state: 'MARKET_DATA_READY', last_updated_at: '2026-09-16T00:00:00+00:00',
                accounts: [
                    { account_type: 'S', account_label: '12345678', signed: true, sensitive_metadata: 'must-drop' },
                    { account_type: 'S', account_label: 'has spaces <script>', signed: true },
                ],
        })).toEqual({
            state: 'MARKET_DATA_READY', last_updated_at: '2026-09-16T00:00:00+00:00',
            accounts: [{ account_type: 'S', account_label: '12345678', signed: true }],
        });
    });

    it('fails closed to observer capability when the mode is absent or malformed', () => {
        expect(normalizeAutotradeHealth({ environment: 'SIM', trading_gate: 'DISARMED', capability_mode: 'LIVE_OPERATOR' }))
            .toMatchObject({ capability_mode: 'OBSERVER' });
    });

    it('submits the one-way kill switch with a caller-supplied idempotency key', async () => {
        mocks.post.mockResolvedValue({ trading_gate: 'KILLED', reason: 'operator test', applied: true });
        await expect(applyAutotradeKillSwitch('operator test', 'kill-test-1')).resolves.toEqual({
            trading_gate: 'KILLED', reason: 'operator test', applied: true,
        });
        expect(mocks.post).toHaveBeenCalledWith(
            '/api/v1/autotrade/kill-switch',
            { reason: 'operator test' },
            { headers: { 'Idempotency-Key': 'kill-test-1' } },
        );
    });

    it('submits strategy control with an explicit reason and idempotency key', async () => {
        mocks.post.mockResolvedValue({
            strategy_id: 'reference-fixture', action: 'start', reason: 'operator test',
            state: 'RUNNING', enabled: true, run_id: 'run-1', applied: true,
        });
        await expect(applyReferenceStrategyControl('start', 'operator test', 'strategy-start-1')).resolves.toEqual({
            strategy_id: 'reference-fixture', action: 'start', reason: 'operator test',
            state: 'RUNNING', enabled: true, run_id: 'run-1', applied: true,
        });
        expect(mocks.post).toHaveBeenCalledWith(
            '/api/v1/autotrade/strategies/reference-fixture/start', { reason: 'operator test' },
            { headers: { 'Idempotency-Key': 'strategy-start-1' } },
        );
    });

    it('submits a single durable reconciliation refresh with an idempotency key', async () => {
        mocks.post.mockResolvedValue({
            intent_id: 'unknown-1', reason: 'operator check', status: 'RECONCILING',
            observed_terminal_facts: 0, resolved_intents: 0, applied: true,
        });
        await expect(refreshAutotradeReconciliation('unknown-1', 'operator check', 'refresh-1')).resolves.toEqual({
            intent_id: 'unknown-1', reason: 'operator check', status: 'RECONCILING',
            observed_terminal_facts: 0, resolved_intents: 0, applied: true,
        });
        expect(mocks.post).toHaveBeenCalledWith(
            '/api/v1/autotrade/reconciliation/unknown-1/refresh', { reason: 'operator check' },
            { headers: { 'Idempotency-Key': 'refresh-1' } },
        );
    });

    it('requests only the narrow SIM position refresh receipt', async () => {
        mocks.post.mockResolvedValue({ item_count: 2, observed_at: '2026-09-16T00:00:00+00:00' });
        await expect(refreshAutotradePositionSnapshots()).resolves.toEqual({
            item_count: 2, observed_at: '2026-09-16T00:00:00+00:00',
        });
        expect(mocks.post).toHaveBeenCalledWith('/api/v1/autotrade/positions/refresh', {});
    });
});

describe('normalizeAutotradeOverview', () => {
    it('fails closed to empty, non-trading observation data', () => {
        expect(normalizeAutotradeOverview({ health: { environment: 'SIM', trading_gate: 'DISARMED' } })).toMatchObject({
            health: { environment: 'SIM', trading_gate: 'DISARMED' },
            strategies: [], orders: [], reconciliation: [],
            performance: { run_count: 0, filled_order_count: 0 },
        });
    });

    it('accepts durable SIM position observations with full account numbers', () => {
        const overview = normalizeAutotradeOverview({
            positions: [{
                snapshot_id: 'snapshot-1', account_type: 'S', account_label: 'SIM-ACCOUNT-1234', symbol: '2330', side: 'LONG',
                quantity: '2', average_price: '1000', market_price: '1001', unrealized_pnl: '2',
                observed_at: '2026-09-16T00:00:00+00:00',
            }, {
                snapshot_id: 'malformed', account_type: 'S', account_label: '', symbol: '2330', side: 'LONG',
                quantity: '2', average_price: '1000', market_price: '1001', unrealized_pnl: '2',
                observed_at: '2026-09-16T00:00:00+00:00',
            }],
        });
        expect(overview.positions).toEqual([expect.objectContaining({
            snapshot_id: 'snapshot-1', account_label: 'SIM-ACCOUNT-1234', symbol: '2330', quantity: '2',
        })]);
    });

    it('keeps an adapter-provided broker identity as read-only observation data', () => {
        const overview = normalizeAutotradeOverview({
            health: { environment: 'SIM', trading_gate: 'DISARMED' },
            orders: [{
                intent_id: 'intent-1', correlation_id: 'correlation-1',
                source: 'MANUAL_UI', status: 'PENDING_SUBMIT',
                broker_order_id: 'MOCK-ORDER-1',
                risk_reasons: ['trading_gate_not_armed'],
                symbol: '2330', side: 'BUY', quantity: 1,
                limit_price: '1000', order_price_type: 'LMT',
            }],
        });
        expect(overview.orders).toEqual([expect.objectContaining({
            intent_id: 'intent-1', broker_order_id: 'MOCK-ORDER-1',
            risk_reasons: ['trading_gate_not_armed'],
            symbol: '2330', side: 'BUY', quantity: 1,
            limit_price: '1000', order_price_type: 'LMT',
        })]);
    });

    it('keeps Gateway-enforced static limits as read-only facts', () => {
        const overview = normalizeAutotradeOverview({
            risk: {
                mode: 'ENFORCING', limits_source: 'gateway_static_simulation_v1',
                limits: { max_order_quantity: 10000, max_order_notional: '5000000' },
            },
        });
        expect(overview.risk).toMatchObject({
            limits_source: 'gateway_static_simulation_v1',
            limits: { max_order_quantity: 10000, max_order_notional: '5000000' },
        });
    });

    it('normalizes the latest durable risk verdict without accepting a malformed one', () => {
        const overview = normalizeAutotradeOverview({
            risk: {
                last_verdict: {
                    intent_id: 'intent-1', correlation_id: 'correlation-1',
                    status: 'RISK_REJECTED', allowed: false,
                    reasons: ['trading_gate_not_armed'], source: 'MANUAL_UI', updated_at: '2026-01-01T00:00:00+00:00',
                },
            },
        });
        expect(overview.risk.last_verdict).toEqual(expect.objectContaining({
            allowed: false, reasons: ['trading_gate_not_armed'], correlation_id: 'correlation-1',
        }));
        expect(normalizeAutotradeOverview({ risk: { last_verdict: { allowed: false } } }).risk.last_verdict).toBeNull();
    });

    it('normalizes the durable decision timeline separately from orders', () => {
        const overview = normalizeAutotradeOverview({
            decisions: [{
                intent_id: 'decision-1', correlation_id: 'correlation-1',
                source: 'MANUAL_UI', status: 'RISK_REJECTED',
                risk_reasons: ['trading_gate_not_armed'],
                allowed: false, decided_at: '2026-09-15T00:00:00+00:00',
            }],
        });
        expect(overview.decisions).toEqual([expect.objectContaining({
            intent_id: 'decision-1', risk_reasons: ['trading_gate_not_armed'],
            allowed: false, decided_at: '2026-09-15T00:00:00+00:00',
        })]);
        expect(overview.orders).toEqual([]);
    });

    it('keeps immutable audit lifecycle events separate from risk decisions', () => {
        const overview = normalizeAutotradeOverview({
            audit_events: [{
                event_id: 'event-1', intent_id: 'intent-1', event_type: 'FILL_RECORDED',
                details: { fill_id: 'fill-1' }, occurred_at: '2026-09-15T00:00:00+00:00',
            }],
        });
        expect(overview.audit_events).toEqual([{
            event_id: 'event-1', intent_id: 'intent-1', event_type: 'FILL_RECORDED',
            details: { fill_id: 'fill-1' }, occurred_at: '2026-09-15T00:00:00+00:00',
        }]);
        expect(overview.decisions).toEqual([]);
    });

    it('keeps broker-confirmed fill totals on their durable order projection', () => {
        const overview = normalizeAutotradeOverview({
            orders: [{
                intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'STRATEGY', status: 'FILLED',
                filled_quantity: 3, fill_count: 2,
            }],
        });

        expect(overview.orders[0]).toMatchObject({ filled_quantity: 3, fill_count: 2 });
    });

    it('keeps durable reconciliation case evidence with its unknown intent', () => {
        const overview = normalizeAutotradeOverview({
            reconciliation: [{
                intent_id: 'unknown-1', correlation_id: 'correlation-1', source: 'MANUAL_UI',
                status: 'UNKNOWN', reconciliation_case: {
                    state: 'OPEN', reason: 'restart_recovery',
                    opened_at: '2026-09-15T00:00:00+00:00', updated_at: '2026-09-15T00:00:00+00:00',
                },
            }],
        });
        expect(overview.reconciliation[0]).toEqual(expect.objectContaining({
            intent_id: 'unknown-1', reconciliation_case: expect.objectContaining({ reason: 'restart_recovery' }),
        }));
    });

    it('normalizes durable strategy lifecycle and heartbeat facts', () => {
        const overview = normalizeAutotradeOverview({
            runs: [{
                run_id: 'run-1', strategy_id: 'reference-fixture', version: 'v1',
                config_checksum: 'fixture-checksum', state: 'FIXTURE_COMPLETED',
                last_status: 'RISK_REJECTED', intent_count: 1,
                started_at: '2026-09-15T00:00:00+00:00',
                heartbeat_at: '2026-09-15T00:00:01+00:00',
                ended_at: '2026-09-15T00:00:02+00:00', updated_at: '2026-09-15T00:00:02+00:00',
            }],
        });
        expect(overview.runs).toEqual([expect.objectContaining({
            run_id: 'run-1', strategy_id: 'reference-fixture', version: 'v1',
            state: 'FIXTURE_COMPLETED', heartbeat_at: '2026-09-15T00:00:01+00:00', intent_count: 1,
        })]);
    });

    it('accepts only complete durable signals linked to an intent', () => {
        const overview = normalizeAutotradeOverview({
            signals: [{
                signal_id: 'signal-1', strategy_id: 'reference-fixture', run_id: 'run-1',
                event_id: 'event-1', signal_type: 'REFERENCE_SIGNAL', symbol: '2330', side: 'BUY',
                intent_id: 'intent-1', correlation_id: 'correlation-1', status: 'RISK_REJECTED',
                risk_reasons: ['trading_gate_not_armed'], observed_at: '2026-09-15T00:00:00+00:00',
            }, { signal_id: 'incomplete' }],
        });
        expect(overview.signals).toEqual([expect.objectContaining({
            signal_id: 'signal-1', intent_id: 'intent-1', risk_reasons: ['trading_gate_not_armed'],
        })]);
    });

    it('preserves a target-order change as immutable observation data', () => {
        const overview = normalizeAutotradeOverview({
            health: { environment: 'SIM', trading_gate: 'DISARMED' },
            orders: [{
                intent_id: 'intent-change', correlation_id: 'correlation-change',
                source: 'MANUAL_UI', status: 'RISK_REJECTED',
                operation: 'UPDATE_PRICE', target_order_id: 'broker-order-1',
                operation_payload: { price: 1000 },
            }],
        });
        expect(overview.orders).toEqual([expect.objectContaining({
            operation: 'UPDATE_PRICE', target_order_id: 'broker-order-1',
            operation_payload: { price: 1000 },
        })]);
    });
});

describe('durable observation detail clients', () => {
    it('reads an intent from its encoded Gateway resource and normalizes it', async () => {
        mocks.get.mockResolvedValue({
            intent_id: 'intent/with space', correlation_id: 'correlation-1',
            source: 'MANUAL_UI', status: 'RISK_REJECTED',
            symbol: '2330', side: 'BUY', quantity: 1,
            limit_price: '1000', order_price_type: 'LMT',
        });

        await expect(fetchAutotradeIntent('intent/with space')).resolves.toEqual(expect.objectContaining({
            intent_id: 'intent/with space', status: 'RISK_REJECTED',
        }));
        expect(mocks.get).toHaveBeenCalledWith('/api/v1/autotrade/intents/intent%2Fwith%20space');
    });

    it('keeps order details fail-closed when the Gateway response lacks a durable identity', async () => {
        mocks.get.mockResolvedValue({ status: 'PENDING_SUBMIT', broker_order_id: 'untrusted' });

        await expect(fetchAutotradeOrder('intent-2')).resolves.toBeNull();
        expect(mocks.get).toHaveBeenCalledWith('/api/v1/autotrade/orders/intent-2');
    });

    it('does not turn a failed Gateway read into a synthetic order observation', async () => {
        mocks.get.mockRejectedValue(new Error('Gateway unavailable'));

        await expect(fetchAutotradeOrder('intent-3')).rejects.toThrow('Gateway unavailable');
    });
});
