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
    signals: AutotradeSignal[];
    runs: AutotradeRun[];
    decisions: AutotradeOrderObservation[];
    orders: AutotradeOrderObservation[];
    audit_events: AutotradeAuditEvent[];
    risk: {
        mode: string;
        limits_source: string;
        limits: Record<string, unknown>;
        last_verdict: AutotradeRiskVerdict | null;
        usage: {
            intent_count: number;
            confirmed_fill_count: number;
            confirmed_filled_quantity: number;
            reconciliation_count: number;
            authoritative_remaining_available: boolean;
        };
    };
    reconciliation: AutotradeReconciliationObservation[];
    performance: {
        run_count: number;
        filled_order_count: number;
        realized_pnl: string;
        unrealized_pnl: string;
    };
};

export type AutotradeRiskVerdict = {
    intent_id: string;
    correlation_id: string;
    status: string;
    allowed: boolean;
    reasons: string[];
    source: string;
    updated_at: string;
};

export type AutotradeRun = {
    run_id: string;
    strategy_id: string | null;
    version: string | null;
    config_checksum: string | null;
    state: string | null;
    last_intent_id: string | null;
    last_status: string | null;
    started_at: string | null;
    heartbeat_at: string | null;
    ended_at: string | null;
    updated_at: string;
    intent_count: number;
};

export type AutotradeSignal = {
    signal_id: string;
    strategy_id: string;
    run_id: string;
    event_id: string;
    signal_type: string;
    symbol: string;
    side: string;
    intent_id: string;
    correlation_id: string;
    status: string;
    risk_reasons: string[];
    observed_at: string;
};

export type AutotradeAuditEvent = {
    event_id: string;
    intent_id: string | null;
    event_type: string;
    details: Record<string, unknown>;
    occurred_at: string;
};

export type AutotradeOrderObservation = {
    intent_id: string;
    correlation_id: string;
    source: string;
    status: string;
    symbol: string;
    side: string;
    quantity: number;
    limit_price: string;
    order_price_type: string;
    strategy_id: string | null;
    run_id: string | null;
    broker_order_id: string | null;
    filled_quantity?: number;
    fill_count?: number;
    operation: 'PLACE' | 'CANCEL' | 'UPDATE_PRICE' | 'UPDATE_QUANTITY';
    target_order_id: string | null;
    operation_payload: Record<string, unknown> | null;
    instrument: Record<string, unknown> | null;
    risk_reasons: string[];
    allowed?: boolean | null;
    decided_at?: string | null;
    risk_decision_missing?: boolean;
    updated_at: string;
};

export type AutotradeReconciliationObservation = AutotradeOrderObservation & {
    reconciliation_case: {
        state: string;
        reason: string;
        opened_at: string;
        updated_at: string;
    } | null;
};

export type StrategyPreview = {
    mode: 'VALIDATE' | 'DRY_RUN';
    message: string;
};

export type AutotradeKillSwitchReceipt = {
    trading_gate: 'KILLED';
    reason: string;
    applied: boolean;
};

export type StrategyControlAction = 'start' | 'pause' | 'resume' | 'stop';

export type StrategyControlReceipt = {
    strategy_id: 'reference-fixture';
    action: StrategyControlAction;
    reason: string;
    state: 'RUNNING' | 'PAUSED' | 'STOPPED';
    enabled: boolean;
    run_id: string;
    applied: boolean;
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
            symbol: typeof order.symbol === 'string' ? order.symbol : '',
            side: typeof order.side === 'string' ? order.side : '',
            quantity: typeof order.quantity === 'number' && Number.isFinite(order.quantity) ? order.quantity : 0,
            limit_price: typeof order.limit_price === 'string' ? order.limit_price : '',
            order_price_type: typeof order.order_price_type === 'string' ? order.order_price_type : 'LMT',
            broker_order_id: typeof order.broker_order_id === 'string' ? order.broker_order_id : null,
            filled_quantity: typeof order.filled_quantity === 'number' && Number.isInteger(order.filled_quantity) && order.filled_quantity > 0
                ? order.filled_quantity : 0,
            fill_count: typeof order.fill_count === 'number' && Number.isInteger(order.fill_count) && order.fill_count > 0
                ? order.fill_count : 0,
            operation, target_order_id: typeof order.target_order_id === 'string' ? order.target_order_id : null,
            operation_payload: objectOrNull(order.operation_payload), instrument: objectOrNull(order.instrument),
            risk_reasons: Array.isArray(order.risk_reasons) ? order.risk_reasons.filter((reason): reason is string => typeof reason === 'string') : [],
            allowed: typeof order.allowed === 'boolean' ? order.allowed : null,
            decided_at: typeof order.decided_at === 'string' ? order.decided_at : null,
            risk_decision_missing: order.risk_decision_missing === true,
            updated_at: typeof order.updated_at === 'string' ? order.updated_at : '',
        }];
    }) : [];
    const reconciliation = Array.isArray(raw.reconciliation) ? raw.reconciliation.flatMap((item): AutotradeReconciliationObservation[] => {
        const order = normalizeOrders([item])[0];
        if (!order || !item || typeof item !== 'object') return [];
        const rawCase = (item as Record<string, unknown>).reconciliation_case;
        if (!rawCase || typeof rawCase !== 'object') return [{ ...order, reconciliation_case: null }];
        const caseRecord = rawCase as Record<string, unknown>;
        if (typeof caseRecord.state !== 'string' || typeof caseRecord.reason !== 'string'
            || typeof caseRecord.opened_at !== 'string' || typeof caseRecord.updated_at !== 'string') return [{ ...order, reconciliation_case: null }];
        return [{ ...order, reconciliation_case: {
            state: caseRecord.state, reason: caseRecord.reason,
            opened_at: caseRecord.opened_at, updated_at: caseRecord.updated_at,
        } }];
    }) : [];
    const risk = raw.risk && typeof raw.risk === 'object' ? raw.risk as Record<string, unknown> : {};
    const rawVerdict = risk.last_verdict && typeof risk.last_verdict === 'object'
        ? risk.last_verdict as Record<string, unknown> : null;
    const lastVerdict: AutotradeRiskVerdict | null = rawVerdict
        && typeof rawVerdict.intent_id === 'string'
        && typeof rawVerdict.correlation_id === 'string'
        && typeof rawVerdict.status === 'string'
        && typeof rawVerdict.allowed === 'boolean'
        && typeof rawVerdict.source === 'string'
        && typeof rawVerdict.updated_at === 'string'
        ? {
            intent_id: rawVerdict.intent_id,
            correlation_id: rawVerdict.correlation_id,
            status: rawVerdict.status,
            allowed: rawVerdict.allowed,
            reasons: Array.isArray(rawVerdict.reasons)
                ? rawVerdict.reasons.filter((reason): reason is string => typeof reason === 'string') : [],
            source: rawVerdict.source,
            updated_at: rawVerdict.updated_at,
        }
        : null;
    const usage = risk.usage && typeof risk.usage === 'object' ? risk.usage as Record<string, unknown> : {};
    const nonNegativeInteger = (candidate: unknown) => typeof candidate === 'number' && Number.isInteger(candidate) && candidate >= 0
        ? candidate : 0;
    const performance = raw.performance && typeof raw.performance === 'object'
        ? raw.performance as Record<string, unknown> : {};
    const runs = Array.isArray(raw.runs) ? raw.runs.flatMap((item): AutotradeRun[] => {
        if (!item || typeof item !== 'object') return [];
        const run = item as Record<string, unknown>;
        if (typeof run.run_id !== 'string' || typeof run.intent_count !== 'number') return [];
        return [{
            run_id: run.run_id,
            strategy_id: typeof run.strategy_id === 'string' ? run.strategy_id : null,
            version: typeof run.version === 'string' ? run.version : null,
            config_checksum: typeof run.config_checksum === 'string' ? run.config_checksum : null,
            state: typeof run.state === 'string' ? run.state : null,
            last_intent_id: typeof run.last_intent_id === 'string' ? run.last_intent_id : null,
            last_status: typeof run.last_status === 'string' ? run.last_status : null,
            started_at: typeof run.started_at === 'string' ? run.started_at : null,
            heartbeat_at: typeof run.heartbeat_at === 'string' ? run.heartbeat_at : null,
            ended_at: typeof run.ended_at === 'string' ? run.ended_at : null,
            updated_at: typeof run.updated_at === 'string' ? run.updated_at : '',
            intent_count: run.intent_count,
        }];
    }) : [];
    const signals = Array.isArray(raw.signals) ? raw.signals.flatMap((item): AutotradeSignal[] => {
        if (!item || typeof item !== 'object') return [];
        const signal = item as Record<string, unknown>;
        const required = [
            'signal_id', 'strategy_id', 'run_id', 'event_id', 'signal_type', 'symbol', 'side',
            'intent_id', 'correlation_id', 'status', 'observed_at',
        ] as const;
        if (required.some((field) => typeof signal[field] !== 'string' || !signal[field])) return [];
        return [{
            signal_id: signal.signal_id as string,
            strategy_id: signal.strategy_id as string,
            run_id: signal.run_id as string,
            event_id: signal.event_id as string,
            signal_type: signal.signal_type as string,
            symbol: signal.symbol as string,
            side: signal.side as string,
            intent_id: signal.intent_id as string,
            correlation_id: signal.correlation_id as string,
            status: signal.status as string,
            risk_reasons: Array.isArray(signal.risk_reasons)
                ? signal.risk_reasons.filter((reason): reason is string => typeof reason === 'string') : [],
            observed_at: signal.observed_at as string,
        }];
    }) : [];
    const auditEvents = Array.isArray(raw.audit_events) ? raw.audit_events.flatMap((item): AutotradeAuditEvent[] => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
        const event = item as Record<string, unknown>;
        if (typeof event.event_id !== 'string' || !event.event_id
            || typeof event.event_type !== 'string' || !event.event_type
            || typeof event.occurred_at !== 'string' || !event.occurred_at) return [];
        return [{
            event_id: event.event_id,
            intent_id: typeof event.intent_id === 'string' ? event.intent_id : null,
            event_type: event.event_type,
            details: event.details && typeof event.details === 'object' && !Array.isArray(event.details)
                ? event.details as Record<string, unknown> : {},
            occurred_at: event.occurred_at,
        }];
    }) : [];
    return {
        health: normalizeAutotradeHealth(raw.health),
        strategies: list('strategies'),
        signals,
        runs,
        orders: normalizeOrders(raw.orders),
        decisions: normalizeOrders(raw.decisions),
        audit_events: auditEvents,
        risk: {
            mode: typeof risk.mode === 'string' ? risk.mode : 'UNKNOWN',
            limits_source: typeof risk.limits_source === 'string' ? risk.limits_source : 'unknown',
            limits: risk.limits && typeof risk.limits === 'object' && !Array.isArray(risk.limits)
                ? risk.limits as Record<string, unknown> : {},
            last_verdict: lastVerdict,
            usage: {
                intent_count: nonNegativeInteger(usage.intent_count),
                confirmed_fill_count: nonNegativeInteger(usage.confirmed_fill_count),
                confirmed_filled_quantity: nonNegativeInteger(usage.confirmed_filled_quantity),
                reconciliation_count: nonNegativeInteger(usage.reconciliation_count),
                authoritative_remaining_available: usage.authoritative_remaining_available === true,
            },
        },
        reconciliation,
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

async function fetchAutotradeObservation(resource: 'intents' | 'orders', intentId: string): Promise<AutotradeOrderObservation | null> {
    const value = await apiGet<unknown>(`/api/v1/autotrade/${resource}/${encodeURIComponent(intentId)}`);
    return normalizeAutotradeOverview({ orders: [value] }).orders[0] ?? null;
}

export function fetchAutotradeIntent(intentId: string) {
    return fetchAutotradeObservation('intents', intentId);
}

export function fetchAutotradeOrder(intentId: string) {
    return fetchAutotradeObservation('orders', intentId);
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

export async function applyAutotradeKillSwitch(
    reason: string,
    idempotencyKey: string,
): Promise<AutotradeKillSwitchReceipt> {
    const document = await apiPost<Record<string, unknown>>(
        '/api/v1/autotrade/kill-switch',
        { reason },
        { headers: { 'Idempotency-Key': idempotencyKey } },
    );
    if (document.trading_gate !== 'KILLED' || typeof document.reason !== 'string'
        || typeof document.applied !== 'boolean') {
        throw new Error('Gateway returned an invalid kill switch receipt');
    }
    return {
        trading_gate: 'KILLED', reason: document.reason, applied: document.applied,
    };
}

export async function applyReferenceStrategyControl(
    action: StrategyControlAction,
    reason: string,
    idempotencyKey: string,
): Promise<StrategyControlReceipt> {
    const document = await apiPost<Record<string, unknown>>(
        `/api/v1/autotrade/strategies/reference-fixture/${action}`,
        { reason }, { headers: { 'Idempotency-Key': idempotencyKey } },
    );
    const validState = document.state === 'RUNNING' || document.state === 'PAUSED' || document.state === 'STOPPED';
    if (document.strategy_id !== 'reference-fixture' || document.action !== action
        || typeof document.reason !== 'string' || !validState || typeof document.enabled !== 'boolean'
        || typeof document.run_id !== 'string' || typeof document.applied !== 'boolean') {
        throw new Error('Gateway returned an invalid strategy control receipt');
    }
    return {
        strategy_id: 'reference-fixture', action, reason: document.reason,
        state: document.state as StrategyControlReceipt['state'], enabled: document.enabled, run_id: document.run_id,
        applied: document.applied,
    };
}
