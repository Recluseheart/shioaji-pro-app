import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
});

describe('Gateway API origin', () => {
    it('keeps a desktop WebView on the Gateway while preserving a native-only sidecar origin', async () => {
        vi.stubGlobal('window', { __TAURI_INTERNALS__: {} });

        const runtime = await import('./runtime');

        expect(runtime.isTauri).toBe(true);
        expect(runtime.getApiBase()).toBe('http://127.0.0.1:18082');
        expect(runtime.getSidecarApiBase()).toBe('http://127.0.0.1:21322');
    });

    it('keeps the public Web build on its same-origin Gateway proxy', async () => {
        vi.stubGlobal('window', {});

        const runtime = await import('./runtime');

        expect(runtime.isTauri).toBe(false);
        expect(runtime.getApiBase()).toBe('');
    });
});
