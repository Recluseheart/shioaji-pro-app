import { describe, expect, it, vi } from 'vitest';
import { AutotradeObserverStore } from './observer-store';
import type { AutotradeOverview } from './client';

const overview = {
    health: {
        environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 1,
        enabled_strategy_count: 0, gateway_status: 'READY',
    },
    strategies: [], signals: [], runs: [], decisions: [], orders: [],
    risk: { mode: 'ENFORCING', limits_source: 'test', limits: {}, last_verdict: null },
    reconciliation: [],
    performance: { run_count: 0, filled_order_count: 0, realized_pnl: '0', unrealized_pnl: '0' },
} satisfies AutotradeOverview;

describe('AutotradeObserverStore', () => {
    it('shares one initial read and preserves durable data when a refresh fails', async () => {
        const fetchOverview = vi.fn()
            .mockResolvedValueOnce(overview)
            .mockRejectedValueOnce(new Error('temporary gateway failure'));
        const store = new AutotradeObserverStore(fetchOverview, 60_000);
        const first = vi.fn();
        const second = vi.fn();
        const unsubscribeFirst = store.subscribe(first);
        const unsubscribeSecond = store.subscribe(second);

        await vi.waitFor(() => expect(store.snapshot().overview).toBe(overview));
        expect(fetchOverview).toHaveBeenCalledTimes(1);
        await store.refresh();

        expect(store.snapshot()).toMatchObject({
            overview,
            error: 'Gateway 更新暫時失敗',
            stale: true,
        });
        expect(first).toHaveBeenCalled();
        expect(second).toHaveBeenCalled();
        unsubscribeFirst();
        unsubscribeSecond();
    });
});
