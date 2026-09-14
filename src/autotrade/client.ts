import { apiGet } from '../lib/api';

export type AutotradeHealth = {
    environment: 'SIM' | 'LIVE' | 'UNKNOWN';
    trading_gate: 'DISARMED' | 'ARMED' | 'KILLED' | 'UNKNOWN';
    strategy_count: number;
    enabled_strategy_count: number;
    gateway_status: 'READY' | 'DEGRADED' | 'UNAVAILABLE';
};

export type AutotradeOverview = {
    health: AutotradeHealth;
    strategies: unknown[];
    signals: unknown[];
    orders: unknown[];
    risk: { mode: string; limits_source: string; last_verdict: unknown };
    reconciliation: unknown[];
    performance: {
        run_count: number;
        filled_order_count: number;
        realized_pnl: string;
        unrealized_pnl: string;
    };
};

const fallback: AutotradeHealth = {
    environment: 'UNKNOWN',
    trading_gate: 'UNKNOWN',
    strategy_count: 0,
    enabled_strategy_count: 0,
    gateway_status: 'UNAVAILABLE',
};

export function normalizeAutotradeHealth(value: unknown): AutotradeHealth {
    if (!value || typeof value !== 'object') return fallback;
    const raw = value as Record<string, unknown>;
    const environment = raw.environment === 'SIM' || raw.environment === 'LIVE'
        ? raw.environment : 'UNKNOWN';
    const trading_gate = raw.trading_gate === 'DISARMED' || raw.trading_gate === 'ARMED' || raw.trading_gate === 'KILLED'
        ? raw.trading_gate : 'UNKNOWN';
    return {
        environment,
        trading_gate,
        strategy_count: typeof raw.strategy_count === 'number' ? raw.strategy_count : 0,
        enabled_strategy_count: typeof raw.enabled_strategy_count === 'number' ? raw.enabled_strategy_count : 0,
        gateway_status: raw.gateway_status === 'READY' || raw.gateway_status === 'DEGRADED'
            ? raw.gateway_status : 'UNAVAILABLE',
    };
}

export async function fetchAutotradeHealth(): Promise<AutotradeHealth> {
    return normalizeAutotradeHealth(await apiGet<unknown>('/api/v1/autotrade/health'));
}

export function normalizeAutotradeOverview(value: unknown): AutotradeOverview {
    const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
    const list = (field: string) => Array.isArray(raw[field]) ? raw[field] : [];
    const risk = raw.risk && typeof raw.risk === 'object' ? raw.risk as Record<string, unknown> : {};
    const performance = raw.performance && typeof raw.performance === 'object'
        ? raw.performance as Record<string, unknown> : {};
    return {
        health: normalizeAutotradeHealth(raw.health),
        strategies: list('strategies'),
        signals: list('signals'),
        orders: list('orders'),
        risk: {
            mode: typeof risk.mode === 'string' ? risk.mode : 'UNKNOWN',
            limits_source: typeof risk.limits_source === 'string' ? risk.limits_source : 'unknown',
            last_verdict: risk.last_verdict ?? null,
        },
        reconciliation: list('reconciliation'),
        performance: {
            run_count: typeof performance.run_count === 'number' ? performance.run_count : 0,
            filled_order_count: typeof performance.filled_order_count === 'number' ? performance.filled_order_count : 0,
            realized_pnl: typeof performance.realized_pnl === 'string' ? performance.realized_pnl : '0',
            unrealized_pnl: typeof performance.unrealized_pnl === 'string' ? performance.unrealized_pnl : '0',
        },
    };
}

export async function fetchAutotradeOverview(): Promise<AutotradeOverview> {
    return normalizeAutotradeOverview(await apiGet<unknown>('/api/v1/autotrade/overview'));
}
