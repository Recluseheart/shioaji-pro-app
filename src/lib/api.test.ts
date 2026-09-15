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

    it('uses the native capability proxy only for enabled desktop mutations', () => {
        expect(
            shouldProxyAgentHarnessMutation(
                true,
                true,
                '/api/v1/order/place_order',
            ),
        ).toBe(true);
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
