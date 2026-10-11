import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    invoke: vi.fn(),
    tauriFetch: vi.fn(),
}));

vi.mock('./runtime', () => ({
    getApiBase: () => 'http://127.0.0.1:18082',
    isTauri: true,
}));
vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }));
vi.mock('@tauri-apps/plugin-http', () => ({ fetch: mocks.tauriFetch }));

import { apiPost } from './api';

describe('desktop Gateway mutation boundary', () => {
    beforeEach(() => {
        mocks.invoke.mockReset();
        mocks.tauriFetch.mockReset();
    });

    it('sends a manual mutation to the local Gateway, never the native sidecar bridge', async () => {
        mocks.tauriFetch.mockResolvedValue(new Response('{"trade_id":"gateway-receipt"}', {
            status: 200, headers: { 'Content-Type': 'application/json' },
        }));

        await expect(apiPost<{ trade_id: string }>('/api/v1/order/place_order', {
            code: '2330', quantity: 1,
        })).resolves.toEqual({ trade_id: 'gateway-receipt' });

        expect(mocks.tauriFetch).toHaveBeenCalledWith(
            'http://127.0.0.1:18082/api/v1/order/place_order',
            expect.objectContaining({ body: '{"code":"2330","quantity":1}' }),
        );
        expect(mocks.invoke).not.toHaveBeenCalled();
    });

    it('rejects Agent-originated mutations before either HTTP or the native bridge', async () => {
        const error = await apiPost('/api/v1/order/place_order', { code: '2330' }, {
            agentInitiated: true, agentCallId: 'call-1', agentAuto: true,
        }).catch((caught: unknown) => caught);

        expect(error).toBeInstanceOf(Error);
        expect(error).toMatchObject({ mutationNotStarted: true });
        expect((error as Error).message).toContain('Gateway 的 durable intent 鏈路');
        expect(mocks.tauriFetch).not.toHaveBeenCalled();
        expect(mocks.invoke).not.toHaveBeenCalled();
    });
});
