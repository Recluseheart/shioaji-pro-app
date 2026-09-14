import { describe, expect, it } from 'vitest';
import { normalizeAutotradeHealth } from './client';

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
