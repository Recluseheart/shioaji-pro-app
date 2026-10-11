import { describe, expect, it, vi } from 'vitest';
import { normalizeAutotradeOverview } from '../autotrade/client';
import { canLoadPreset, loadWorkspacePreset, registerPresetLoader } from '../autotrade/workspace-commands';
import { armRemaining, commandCenterIssues, connectionFreshness } from './auto-command-center';

describe('Auto Command Center connection freshness', () => {
    const now = Date.parse('2026-09-16T01:00:00+00:00');

    it('marks an old cached sidecar posture stale instead of presenting it as live', () => {
        expect(connectionFreshness('2026-09-16T00:59:50+00:00', now)).toBe('CURRENT');
        expect(connectionFreshness('2026-09-16T00:59:40+00:00', now)).toBe('STALE');
    });

    it('fails closed when the Gateway did not provide a valid observation time', () => {
        expect(connectionFreshness(null, now)).toBe('UNKNOWN');
        expect(connectionFreshness('not-a-timestamp', now)).toBe('UNKNOWN');
    });
});

describe('Auto Command Center spec helpers', () => {
    it('counts down the arm window and stops at zero', () => {
        const now = Date.parse('2026-10-01T01:00:00+00:00');
        expect(armRemaining(now / 1000 + 125, now)).toBe('2:05');
        expect(armRemaining(now / 1000 - 5, now)).toBe('0:00');
        expect(armRemaining(null, now)).toBeNull();
    });

    it('lists every condition an operator must act on in plain language', () => {
        const overview = normalizeAutotradeOverview({
            health: { trading_gate: 'KILLED' },
            runtime: { mode: 'SIM', last_error: '行情中斷' },
            strategies: [{ strategy_id: 'demo', display_name: '示範策略', last_error: '等待新成交' }],
            reconciliation: [{
                intent_id: 'lost', correlation_id: 'c', source: 'STRATEGY', status: 'UNKNOWN',
                symbol: '2330', side: 'BUY', quantity: 1, limit_price: '1', order_price_type: 'LMT',
                operation: 'PLACE', risk_reasons: [], updated_at: '2026-10-01T01:00:00+00:00',
            }],
        });
        expect(commandCenterIssues(overview, null)).toEqual([
            'Kill Switch 已啟用：所有新委託都會被拒絕',
            '執行服務：行情中斷',
            '策略 示範策略：等待新成交',
            '1 筆委託無法確定是否已到券商，需要人工對帳',
        ]);
        expect(commandCenterIssues(null, 'Gateway 尚未連線')).toEqual(['Gateway 尚未連線']);
    });

    it('switches layouts only through a registered loader', () => {
        expect(canLoadPreset()).toBe(false);
        expect(loadWorkspacePreset('自動交易控制')).toBe(false);
        const loader = vi.fn();
        const unregister = registerPresetLoader(loader);
        expect(loadWorkspacePreset('自動交易控制')).toBe(true);
        expect(loader).toHaveBeenCalledWith('自動交易控制');
        unregister();
        expect(canLoadPreset()).toBe(false);
    });
});
