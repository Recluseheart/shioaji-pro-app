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
    performance: { source: 'UNAVAILABLE', stale: true, observed_at: null, run_count: 0, filled_order_count: 0, realized_pnl: '0', unrealized_pnl: '0', runs: [] },
    runtime: { mode: 'OBSERVE', execution_available: false, running: false, last_tick: null, last_error: null, arm_expires_at: null },
    today: { date: null, auto_order_count: 0, auto_rejected_count: 0, auto_filled_count: 0, manual_order_count: 0, open_order_count: 0 },
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
        const store = new AutotradeObserverStore(fetchOverview, 60_000, () => source, 0);
        const unsubscribe = store.subscribe(vi.fn());

        await vi.waitFor(() => expect(fetchOverview).toHaveBeenCalledTimes(1));
        onOrder?.();
        await vi.waitFor(() => expect(fetchOverview).toHaveBeenCalledTimes(2));

        unsubscribe();
        await vi.waitFor(() => expect(source.close).toHaveBeenCalledOnce());
    });

    it('does not restart reads or the SSE stream when a subscriber immediately re-subscribes', async () => {
        // React re-subscribes whenever useSyncExternalStore receives a new
        // subscribe function; that must never become a request storm.
        const fetchOverview = vi.fn().mockResolvedValue(overview);
        const source = { addEventListener: vi.fn(), close: vi.fn() };
        const factory = vi.fn(() => source);
        const store = new AutotradeObserverStore(fetchOverview, 60_000, factory, 50);

        let unsubscribe = store.subscribe(vi.fn());
        for (let render = 0; render < 20; render += 1) {
            unsubscribe();
            unsubscribe = store.subscribe(vi.fn());
            await store.refresh();
        }
        expect(factory).toHaveBeenCalledTimes(1);
        expect(source.close).not.toHaveBeenCalled();
        // 1 initial read + 20 explicit refreshes; no extra reads from restarts.
        expect(fetchOverview).toHaveBeenCalledTimes(21);

        unsubscribe();
        await vi.waitFor(() => expect(source.close).toHaveBeenCalledOnce());
    });
});
