import { apiGet } from '../lib/api';

export type AutotradeHealth = {
    environment: 'SIM' | 'LIVE' | 'UNKNOWN';
    trading_gate: 'DISARMED' | 'ARMED' | 'KILLED' | 'UNKNOWN';
    strategy_count: number;
    enabled_strategy_count: number;
    gateway_status: 'READY' | 'DEGRADED' | 'UNAVAILABLE';
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
