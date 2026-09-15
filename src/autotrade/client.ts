import { apiGet, apiPost } from '../lib/api';

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
    runs: AutotradeRun[];
    decisions: AutotradeOrderObservation[];
    orders: AutotradeOrderObservation[];
    risk: { mode: string; limits_source: string; limits: Record<string, unknown>; last_verdict: unknown };
    reconciliation: unknown[];
    performance: {
        run_count: number;
        filled_order_count: number;
        realized_pnl: string;
        unrealized_pnl: string;
    };
};

export type AutotradeRun = {
    run_id: string;
    strategy_id: string | null;
    last_intent_id: string | null;
    last_status: string | null;
    updated_at: string;
    intent_count: number;
};

export type AutotradeOrderObservation = {
    intent_id: string;
    correlation_id: string;
    source: string;
    status: string;
    strategy_id: string | null;
    run_id: string | null;
    broker_order_id: string | null;
    operation: 'PLACE' | 'CANCEL' | 'UPDATE_PRICE' | 'UPDATE_QUANTITY';
    target_order_id: string | null;
    operation_payload: Record<string, unknown> | null;
    instrument: Record<string, unknown> | null;
    risk_reasons: string[];
    updated_at: string;
};

export type StrategyPreview = {
    mode: 'VALIDATE' | 'DRY_RUN';
    message: string;
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
    const normalizeOrders = (value: unknown): AutotradeOrderObservation[] => Array.isArray(value) ? value.flatMap((item): AutotradeOrderObservation[] => {
        if (!item || typeof item !== 'object') return [];
        const order = item as Record<string, unknown>;
        if (typeof order.intent_id !== 'string' || typeof order.correlation_id !== 'string' || typeof order.source !== 'string' || typeof order.status !== 'string') return [];
        const operation = order.operation === 'CANCEL' || order.operation === 'UPDATE_PRICE' || order.operation === 'UPDATE_QUANTITY'
            ? order.operation : 'PLACE';
        const objectOrNull = (candidate: unknown): Record<string, unknown> | null =>
            candidate && typeof candidate === 'object' && !Array.isArray(candidate)
                ? candidate as Record<string, unknown> : null;
        return [{
            intent_id: order.intent_id, correlation_id: order.correlation_id, source: order.source,
            status: order.status, strategy_id: typeof order.strategy_id === 'string' ? order.strategy_id : null,
            run_id: typeof order.run_id === 'string' ? order.run_id : null,
            broker_order_id: typeof order.broker_order_id === 'string' ? order.broker_order_id : null,
            operation, target_order_id: typeof order.target_order_id === 'string' ? order.target_order_id : null,
            operation_payload: objectOrNull(order.operation_payload), instrument: objectOrNull(order.instrument),
            risk_reasons: Array.isArray(order.risk_reasons) ? order.risk_reasons.filter((reason): reason is string => typeof reason === 'string') : [],
            updated_at: typeof order.updated_at === 'string' ? order.updated_at : '',
        }];
    }) : [];
    const risk = raw.risk && typeof raw.risk === 'object' ? raw.risk as Record<string, unknown> : {};
    const performance = raw.performance && typeof raw.performance === 'object'
        ? raw.performance as Record<string, unknown> : {};
    const runs = Array.isArray(raw.runs) ? raw.runs.flatMap((item): AutotradeRun[] => {
        if (!item || typeof item !== 'object') return [];
        const run = item as Record<string, unknown>;
        if (typeof run.run_id !== 'string' || typeof run.intent_count !== 'number') return [];
        return [{
            run_id: run.run_id,
            strategy_id: typeof run.strategy_id === 'string' ? run.strategy_id : null,
            last_intent_id: typeof run.last_intent_id === 'string' ? run.last_intent_id : null,
            last_status: typeof run.last_status === 'string' ? run.last_status : null,
            updated_at: typeof run.updated_at === 'string' ? run.updated_at : '',
            intent_count: run.intent_count,
        }];
    }) : [];
    return {
        health: normalizeAutotradeHealth(raw.health),
        strategies: list('strategies'),
        signals: list('signals'),
        runs,
        orders: normalizeOrders(raw.orders),
        decisions: normalizeOrders(raw.decisions),
        risk: {
            mode: typeof risk.mode === 'string' ? risk.mode : 'UNKNOWN',
            limits_source: typeof risk.limits_source === 'string' ? risk.limits_source : 'unknown',
            limits: risk.limits && typeof risk.limits === 'object' && !Array.isArray(risk.limits)
                ? risk.limits as Record<string, unknown> : {},
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

export async function previewReferenceStrategy(action: 'validate' | 'dry-run'): Promise<StrategyPreview> {
    const document = await apiPost<Record<string, unknown>>(
        `/api/v1/autotrade/strategies/reference-fixture/${action}`,
        {},
    );
    if (document.mode === 'VALIDATE') {
        const warnings = Array.isArray(document.warnings)
            ? document.warnings.filter((warning): warning is string => typeof warning === 'string') : [];
        return { mode: 'VALIDATE', message: warnings.length ? `驗證完成：${warnings.join('、')}` : '驗證完成' };
    }
    const risk = document.risk && typeof document.risk === 'object' ? document.risk as Record<string, unknown> : {};
    const reasons = Array.isArray(risk.reasons)
        ? risk.reasons.filter((reason): reason is string => typeof reason === 'string') : [];
    return {
        mode: 'DRY_RUN',
        message: `Dry Run：不建立 intent／outbox、不送 broker；風控 ${risk.allowed === true ? '允許' : '拒絕'}${reasons.length ? `（${reasons.join('、')}）` : ''}`,
    };
}
