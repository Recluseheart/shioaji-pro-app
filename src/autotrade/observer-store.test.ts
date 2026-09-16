import { describe, expect, it, vi } from 'vitest';
import { AutotradeObserverStore } from './observer-store';
import type { AutotradeOverview } from './client';

const overview = {
    health: {
    api_version: 2, environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 1,
    enabled_strategy_count: 0, gateway_status: 'READY', capability_mode: 'OPERATOR',
    broker_connection: { state: 'DISCONNECTED', accounts: [], last_updated_at: null },
    },
    strategies: [], signals: [], runs: [], decisions: [], orders: [], positions: [], audit_events: [],
    risk: { mode: 'ENFORCING', limits_source: 'test', limits: {}, last_verdict: null, usage: {
        intent_count: 0, confirmed_fill_count: 0, confirmed_filled_quantity: 0,
        reconciliation_count: 0, authoritative_remaining_available: false,
    } },
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

    it('uses a durable-observation SSE event only to refresh the read model', async () => {
        const fetchOverview = vi.fn().mockResolvedValue(overview);
        let onOrder: (() => void) | undefined;
        const source = {
            addEventListener: vi.fn((type: string, listener: () => void) => {
                if (type === 'autotrade-order') onOrder = listener;
            }),
            close: vi.fn(),
        };
        const store = new AutotradeObserverStore(fetchOverview, 60_000, () => source);
        const unsubscribe = store.subscribe(vi.fn());

        await vi.waitFor(() => expect(fetchOverview).toHaveBeenCalledTimes(1));
        onOrder?.();
        await vi.waitFor(() => expect(fetchOverview).toHaveBeenCalledTimes(2));

        unsubscribe();
        expect(source.close).toHaveBeenCalledOnce();
    });
});
