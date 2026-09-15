import { describe, expect, it } from 'vitest';
import { normalizeAutotradeHealth, normalizeAutotradeOverview } from './client';

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
            }],
        });
        expect(overview.orders).toEqual([expect.objectContaining({
            intent_id: 'intent-1', broker_order_id: 'MOCK-ORDER-1',
            risk_reasons: ['trading_gate_not_armed'],
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
