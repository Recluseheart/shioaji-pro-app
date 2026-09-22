import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiPost, shouldProxyAgentHarnessMutation } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('agent harness mutation routing', () => {
    it('keeps normal UI orders on the direct HTTP path while disabled', () => {
        expect(
            shouldProxyAgentHarnessMutation(
                true,
                false,
                '/api/v1/order/place_order',
            ),
        ).toBe(false);
    });

    it('never lets an enabled desktop harness bypass the Gateway', () => {
        expect(
            shouldProxyAgentHarnessMutation(
                true,
                true,
                '/api/v1/order/place_order',
            ),
        ).toBe(false);
        expect(
            shouldProxyAgentHarnessMutation(
                true,
                true,
                '/api/v1/data/snapshots',
            ),
        ).toBe(false);
        expect(
            shouldProxyAgentHarnessMutation(
                false,
                true,
                '/api/v1/order/place_order',
            ),
        ).toBe(false);
    });
});

describe('Gateway risk receipts', () => {
    it('preserves an explicit idempotency key for the narrow kill-switch control', async () => {
        const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
            status: 200, headers: { 'Content-Type': 'application/json' },
        }));
        vi.stubGlobal('fetch', fetchMock);

        await apiPost('/api/v1/autotrade/kill-switch', { reason: 'operator test' }, {
            headers: { 'Idempotency-Key': 'kill-test-1' },
        });
        expect(fetchMock).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
            headers: expect.objectContaining({
                'Content-Type': 'application/json', 'Idempotency-Key': 'kill-test-1',
            }),
        }));
    });

    it('keeps the human-readable reason and short correlation code on a rejected mutation', async () => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
            message: '風控拒絕：交易閘門尚未啟用',
            correlation_id: 'a1b2c3d4-e5f6-7890-abcd-ef0123456789',
        }), {
            status: 409,
            headers: { 'Content-Type': 'application/json' },
        })));

        await expect(apiPost('/api/v1/order/place_order', {})).rejects.toThrow(
            '409 風控拒絕：交易閘門尚未啟用（追蹤 a1b2c3d4）',
        );
    });
});

describe('accepted Gateway mutation readback', () => {
    it('returns only the broker trade and sends the POST once', async () => {
        const trade = { order: { id: 'broker-1' }, status: { status: 'Submitted' } };
        const request = vi.fn()
            .mockResolvedValueOnce(new Response(JSON.stringify({ intent_id: 'intent-1' }), { status: 202 }))
            .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'SUBMITTED', trade })));
        vi.stubGlobal('fetch', request);
        expect(await apiPost('/api/v1/order/place_order', {})).toEqual(trade);
        expect(request.mock.calls.map(call => call[1].method)).toEqual(['POST', 'GET']);
        expect(request.mock.calls[0][1].headers['Idempotency-Key']).toBeTruthy();
    });
    it('preserves uncertain receipt on failed GET without resending POST', async () => {
        const request = vi.fn()
            .mockResolvedValueOnce(new Response(JSON.stringify({ intent_id: 'intent-2' }), { status: 202 }))
            .mockRejectedValueOnce(new Error('network down'));
        vi.stubGlobal('fetch', request);
        await expect(apiPost('/api/v1/order/cancel_order', {})).rejects.toMatchObject({
            mutationNotStarted: false, intentId: 'intent-2', message: expect.stringContaining('勿重送'),
        });
        expect(request.mock.calls.map(call => call[1].method)).toEqual(['POST', 'GET']);
    });
});
