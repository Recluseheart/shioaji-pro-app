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

    it('accepts an explicitly provisioned Gateway origin but rejects a sidecar origin', async () => {
        const runtime = await import('./runtime');

        expect(runtime.gatewayOriginFromEnvironment('https://gateway.example.ts.net')).toBe('https://gateway.example.ts.net');
        expect(runtime.gatewayOriginFromEnvironment('http://127.0.0.1:18082/')).toBe('http://127.0.0.1:18082');
        expect(runtime.gatewayOriginFromEnvironment('http://127.0.0.1:21322')).toBeNull();
        expect(runtime.gatewayOriginFromEnvironment('http://localhost:8080')).toBeNull();
        expect(runtime.gatewayOriginFromEnvironment('http://broker.example.test/user:password')).toBeNull();
    });

    it('makes a static production build address Gateway instead of its host origin', async () => {
        const runtime = await import('./runtime');

        expect(runtime.browserGatewayApiBase(true, undefined)).toBe('http://127.0.0.1:18082');
        expect(runtime.browserGatewayApiBase(false, undefined)).toBe('');
        expect(runtime.browserGatewayApiBase(true, 'https://observer.example.ts.net')).toBe('https://observer.example.ts.net');
        expect(runtime.browserGatewayApiBase(true, 'http://127.0.0.1:21322')).toBe('http://127.0.0.1:18082');
    });
});
