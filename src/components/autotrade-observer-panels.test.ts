import { describe, expect, it } from 'vitest';
import { normalizeAutotradeOverview } from '../autotrade/client';
import { automatedOrderObservations, orderObservationText, panelSummary, riskVerdictText } from './autotrade-observer-panels';

describe('Autotrade observer panel summaries', () => {
    it('does not describe populated simulation limits as unavailable', () => {
        const overview = normalizeAutotradeOverview({
            risk: { limits: { max_order_quantity: 10000 } },
        });

        expect(panelSummary('risk_limits', overview, null)).toContain('已載入 simulation');
        expect(panelSummary('risk_limits', overview, null)).toContain('缺少時會拒絕');
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

    it('renders durable product and strategy attribution without pretending a target mutation has a price', () => {
        expect(orderObservationText({
            operation: 'PLACE', symbol: '2330', side: 'BUY', quantity: 2,
            limit_price: '1000', order_price_type: 'LMT', strategy_id: 'reference', run_id: 'run-1',
        })).toBe('商品：2330 · 買 2 · LMT 1000 · 策略 reference · run run-1');
        expect(orderObservationText({ operation: 'UPDATE_PRICE', symbol: '2330', side: 'BUY', quantity: 2 })).toBeNull();
    });
});
