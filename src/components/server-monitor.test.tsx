import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import metrics from '../lib/__fixtures__/monitor-175.json';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../lib/api', () => ({ apiGet: mocks.get }));

import { ServerMonitor } from './server-monitor';

const usage = { connections: 3, bytes: 29637667, limit_bytes: 2147483648, remaining_bytes: 2117845981 };
const subscriptions = { total: 71, market_data: 71, trade_accounts: 0, by_type: { tick: 35, bidask: 35, quote: 1 }, partial: false, unavailable: false, items: [{}] };

describe('server monitor Gateway-only lifecycle', () => {
    let renderer: ReactTestRenderer;
    let visibility: string;
    let changed: () => void;

    beforeEach(() => {
        vi.useFakeTimers();
        visibility = 'visible';
        vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
        vi.stubGlobal('document', {
            get visibilityState() { return visibility; },
            addEventListener: (_: string, fn: () => void) => { changed = fn; },
            removeEventListener: vi.fn(),
        });
        mocks.get.mockReset().mockImplementation(async (path: string) =>
            path.includes('/usage') ? usage : path.includes('/subscriptions') ? subscriptions : metrics,
        );
    });

    afterEach(async () => {
        if (renderer) await act(async () => renderer.unmount());
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    const mount = async () => { await act(async () => { renderer = create(createElement(ServerMonitor)); }); };
    const button = (label: string) => renderer.root.findAllByType('button').find(node => node.children.join('').includes(label))!;
    const text = () => JSON.stringify(renderer.toJSON());

    it('uses Gateway monitor data and explains the evidence caveats', async () => {
        await mount();
        expect(text()).toContain('1.4%');
        expect(text()).toContain('71');
        expect(text()).toContain('並非券商限流桶');
        expect(text()).toContain('不嵌入 WebView');
        expect(mocks.get.mock.calls.every(([, options]) => options.headers['X-Shioaji-Activity'] === 'dashboard')).toBe(true);
    });

    it('never embeds a direct-sidecar iframe', async () => {
        await mount();
        expect(renderer.root.findAllByType('iframe')).toHaveLength(0);
    });

    it('preserves unknown values on malformed or failed Gateway reads', async () => {
        mocks.get.mockRejectedValue(new Error('not logged in'));
        await mount();
        expect(text()).toContain('監控無法取得');
        expect(text()).toContain('not logged in');
        expect(renderer.root.findByProps({ role: 'meter' }).props['aria-valuenow']).toBeUndefined();
    });

    it('pauses and aborts Gateway reads when hidden or paused', async () => {
        await mount();
        const signals = mocks.get.mock.calls.map(([, options]) => options.signal as AbortSignal);
        await act(async () => { visibility = 'hidden'; changed(); });
        const count = mocks.get.mock.calls.length;
        await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
        expect(mocks.get).toHaveBeenCalledTimes(count);
        expect(signals.every(signal => signal.aborted)).toBe(true);
        await act(async () => { visibility = 'visible'; changed(); });
        await act(async () => button('暫停').props.onClick());
        expect(text()).toContain('上次觀測');
    });
});
