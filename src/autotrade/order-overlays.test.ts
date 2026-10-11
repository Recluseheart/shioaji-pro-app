import { describe, expect, it } from 'vitest';
import type { AutotradeOrderObservation } from './client';
import { autotradeOrderOverlays } from './order-overlays';

const order = (overrides: Partial<AutotradeOrderObservation> = {}): AutotradeOrderObservation => ({
    intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'STRATEGY', status: 'SUBMITTED',
    symbol: '2330', side: 'BUY', quantity: 2, limit_price: '1000', order_price_type: 'LMT',
    strategy_id: 'reference', run_id: 'run-1', broker_order_id: null, operation: 'PLACE',
    target_order_id: null, operation_payload: null, instrument: null, risk_reasons: [], updated_at: '',
    ...overrides,
});

describe('autotradeOrderOverlays', () => {
    it('only exposes priced automatic PLACE intents for the selected symbol', () => {
        expect(autotradeOrderOverlays([
            order(),
            order({ intent_id: 'manual', source: 'MANUAL_UI' }),
            order({ intent_id: 'other', symbol: '2317' }),
            order({ intent_id: 'market', limit_price: 'MKT' }),
            order({ intent_id: 'cancel', operation: 'CANCEL' }),
        ], '2330')).toEqual([{
            intentId: 'intent-1', price: 1000, side: 'BUY', quantity: 2, status: 'SUBMITTED',
        }]);
    });
});
