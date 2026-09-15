import { describe, expect, it } from 'vitest';
import { normalizeAutotradeOverview } from '../autotrade/client';
import { panelSummary, riskVerdictText } from './autotrade-observer-panels';

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
});
