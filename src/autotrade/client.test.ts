import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../lib/api', () => ({ apiGet: mocks.get, apiPost: mocks.post }));

import {
    fetchAutotradeIntent,
    fetchAutotradeOrder,
    normalizeAutotradeHealth,
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
        expect(normalizeAutotradeHealth({ environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 2, enabled_strategy_count: 0, gateway_status: 'READY' })).toEqual({
            environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 2, enabled_strategy_count: 0, gateway_status: 'READY',
        });
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
