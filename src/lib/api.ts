// src/lib/api.ts

import { getApiBase, isTauri } from './runtime';

// resolved per request — the server port can move at runtime (e.g. the boot
// flow discovers the default port occupied and starts on a fallback), and a
// module-load-time capture kept every request on the dead old port
// (the stuck-at-載入交易終端 bug)
const base = () => getApiBase();

const AGENT_HARNESS_MUTATIONS = new Set([
    '/api/v1/order/place_order',
    '/api/v1/order/cancel_order',
    '/api/v1/order/update_price',
    '/api/v1/order/update_qty',
    '/api/v1/order/place_comboorder',
    '/api/v1/order/cancel_comboorder',
    '/api/v1/order/reserve_stock',
    '/api/v1/order/reserve_earmarking',
]);

export function shouldProxyAgentHarnessMutation(
    _desktop: boolean,
    _enabled: boolean,
    _path: string,
): boolean {
    // The native harness sends directly to the Shioaji sidecar.  That is not
    // an admissible route once the Gateway owns all broker mutations.
    return false;
}

export function shouldRejectUnsignedAgentMutation(
    desktop: boolean,
    _enabled: boolean,
    path: string,
    agentInitiated: boolean,
): boolean {
    return (
        desktop &&
        agentInitiated &&
        // Agent-originated browser requests cannot carry a durable
        // strategy/run identity yet.  Reject them regardless of the native
        // harness toggle rather than silently reviving a sidecar bypass.
        AGENT_HARNESS_MUTATIONS.has(path)
    );
}

async function doFetch(url: string, init?: RequestInit): Promise<Response> {
    if (isTauri) {
        const { fetch: tauriFetch } = await import('@tauri-apps/plugin-http');
        return tauriFetch(url, init);
    }
    return fetch(url, init);
}

async function doFetchWithTimeout(
    url: string,
    init: RequestInit,
    timeoutMs?: number,
): Promise<Response> {
    if (!timeoutMs) return doFetch(url, init);
    const controller = new AbortController();
    const timer = globalThis.setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await doFetch(url, { ...init, signal: controller.signal });
    } finally {
        globalThis.clearTimeout(timer);
    }
}

// shioaji errors come back as JSON: {"code":400,"message":"...","details":...}
// surface that message instead of a bare "400 Bad Request" — the message is
// what tells you it's CA / unsigned account / bad params (issue #1 support)
async function throwApiError(res: Response): Promise<never> {
    let detail = '';
    try {
        const data = (await res.json()) as {
            message?: string;
            details?: unknown;
            correlation_id?: unknown;
        };
        detail =
            data.message ??
            (typeof data.details === 'string' ? data.details : '');
        if (data.details && typeof data.details !== 'string') {
            detail += ` ${JSON.stringify(data.details)}`;
        }
        // Gateway risk receipts carry a durable correlation id.  Surface a
        // short, non-sensitive code with the human-readable reason so a user
        // can find the same rejection in the observer timeline without
        // exposing a full account, credential, or broker identifier.
        if (typeof data.correlation_id === 'string' && data.correlation_id) {
            detail += `${detail ? '' : '拒絕的交易請求'}（追蹤 ${data.correlation_id.slice(0, 8)}）`;
        }
    } catch {
        // non-JSON body — fall back to status text
    }
    throw Object.assign(
        new Error(`${res.status} ${detail || res.statusText}`.trim()),
        { status: res.status },
    );
}

export async function apiGet<T>(path: string, opts?: { signal?: AbortSignal; headers?: HeadersInit }): Promise<T> {
    const res = await doFetch(base() + path, opts);
    if (!res.ok) await throwApiError(res);
    return res.json() as Promise<T>;
}

export async function apiPost<T>(
    path: string,
    body: unknown,
    opts?: {
        timeoutMs?: number;
        headers?: HeadersInit;
        agentInitiated?: boolean;
        agentCallId?: string;
        agentAuto?: boolean;
    },
): Promise<T> {
    if (
        shouldRejectUnsignedAgentMutation(
            isTauri,
            false,
            path,
            opts?.agentInitiated === true,
        )
    ) {
        throw Object.assign(
            new Error('Agent 下單尚未移植至 Gateway 的 durable intent 鏈路'),
            { mutationNotStarted: true },
        );
    }
    const res = await doFetchWithTimeout(
        base() + path,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...opts?.headers },
            body: JSON.stringify(body),
        },
        // opt-in only — order paths must never abort an in-flight request
        // (an aborted POST tells us nothing about whether it was executed)
        opts?.timeoutMs,
    );
    if (!res.ok) await throwApiError(res);
    return res.json() as Promise<T>;
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
    const res = await doFetch(base() + path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) await throwApiError(res);
    return res.json() as Promise<T>;
}

export async function apiDelete<T>(path: string, body?: unknown): Promise<T> {
    const res = await doFetch(base() + path, {
        method: 'DELETE',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) await throwApiError(res);
    return res.json() as Promise<T>;
}
