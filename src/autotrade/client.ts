import { apiGet, apiPost } from '../lib/api';

export type AutotradeHealth = {
    api_version: number;
    environment: 'SIM' | 'PAPER' | 'LIVE' | 'UNKNOWN';
    trading_gate: 'DISARMED' | 'ARMED' | 'KILLED' | 'UNKNOWN';
    strategy_count: number;
    enabled_strategy_count: number;
    gateway_status: 'READY' | 'DEGRADED' | 'UNAVAILABLE';
    capability_mode: 'OPERATOR' | 'OBSERVER';
    broker_connection: {
        state: 'DISCONNECTED' | 'CONNECTING' | 'AUTHENTICATED' | 'MARKET_DATA_READY' | 'DEGRADED' | 'LOST';
        accounts: Array<{ account_type: 'S' | 'F'; account_label: string; signed: boolean }>;
        last_updated_at: string | null;
    };
};

export type AutotradeConnection = AutotradeHealth['broker_connection'];

export type AutotradeRuntime = {
    mode: 'PAPER' | 'SIM' | 'OBSERVE' | string;
    execution_available: boolean;
    running: boolean;
    last_tick: string | null;
    last_error: string | null;
    arm_expires_at: number | null;
};

export type AutotradeToday = {
    date: string | null;
    auto_order_count: number;
    auto_rejected_count: number;
    auto_filled_count: number;
    manual_order_count: number;
    open_order_count: number;
};

export type AutotradeRunPerformance = {
    run_id: string;
    strategy_id: string | null;
    version: string | null;
    state: string | null;
    started_at: string | null;
    ended_at: string | null;
    intent_count: number;
    rejected_count: number;
    reject_rate: number | null;
    fill_count: number;
    closed_trade_count: number;
    win_rate: number | null;
    realized_pnl: string | null;
    max_drawdown: string | null;
    avg_slippage_bps: string | null;
    avg_ack_latency_ms: number | null;
    avg_fill_latency_ms: number | null;
};

export type AutotradeOverview = {
    runtime: AutotradeRuntime;
    today: AutotradeToday;
    health: AutotradeHealth;
    strategies: unknown[];
    signals: AutotradeSignal[];
    runs: AutotradeRun[];
    decisions: AutotradeOrderObservation[];
    orders: AutotradeOrderObservation[];
    audit_events: AutotradeAuditEvent[];
    positions: AutotradePositionObservation[];
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
            orders_last_minute?: number;
            pending_exposure?: string | null;
            daily_loss_used?: string | null;
            daily_loss_remaining?: string | null;
            gross_exposure?: string | null;
            gross_exposure_remaining?: string | null;
            orders_per_minute_remaining?: number | null;
        };
    };
    reconciliation: AutotradeReconciliationObservation[];
    performance: {
        run_count: number;
        filled_order_count: number;
        realized_pnl: string | null;
        unrealized_pnl: string | null;
        source: string;
        stale: boolean;
        observed_at: string | null;
        gross_exposure?: string | null;
        runs: AutotradeRunPerformance[];
    };
};

export type AutotradePositionObservation = {
    snapshot_id: string;
    account_type: 'S' | 'F';
    account_label: string;
    symbol: string;
    side: 'LONG' | 'SHORT';
    quantity: string;
    average_price: string;
    market_price: string;
    unrealized_pnl: string;
    observed_at: string;
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
    account_id?: string;
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
    avg_fill_price?: string | null;
    created_at?: string;
    /** 張 for board lots, 股 for odd lots or plain stock units, 口 for futures contracts. */
    quantity_unit?: '張' | '股' | '口';
    updated_at: string;
};

export type AutotradeReconciliationObservation = AutotradeOrderObservation & {
    operator_note?: { conclusion: string; noted_at: string | null } | null;
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
    /** Plain-language facts behind a dry run: market, condition, and each proposed order. */
    details?: string[];
};

export type AutotradeKillSwitchReceipt = {
    trading_gate: 'KILLED';
    reason: string;
    applied: boolean;
};

export type ReconciliationRefreshReceipt = {
    intent_id: string;
    reason: string;
    status: string;
    observed_terminal_facts: number;
    resolved_intents: number;
    applied: boolean;
};

export type PositionSnapshotRefreshReceipt = {
    item_count: number;
    observed_at: string | null;
};

export type StrategyControlAction = 'start' | 'pause' | 'resume' | 'stop';

export type StrategyControlReceipt = {
    strategy_id: string;
    action: StrategyControlAction;
    reason: string;
    state: 'RUNNING' | 'PAUSED' | 'STOPPED';
    enabled: boolean;
    run_id: string;
    applied: boolean;
};

/** The operator chose to see full account numbers; any short, plain identifier is shown as-is. */
export function isAccountLabel(value: unknown): value is string {
    return typeof value === 'string' && /^[A-Za-z0-9*_-]{1,32}$/.test(value);
}

export function riskReasonLabel(reason: string): string {
    return ({
        trading_gate_not_armed: '交易閘門尚未啟用',
        target_order_account_unverified: '目標委託的帳戶尚未驗證',
        target_order_id_required: '缺少目標委託識別碼',
        simulation_account_not_allowed: '此 simulation 帳戶不在 allowlist',
        symbol_not_allowed: '商品不在 allowlist',
        quantity_limit_exceeded: '數量超過單筆限額',
        notional_limit_exceeded: '名目金額超過單筆限額',
        risk_context_unavailable: '缺少權威市場與帳務風控快照',
        stale_quote: '行情已過期或不可用',
        price_deviation_exceeded: '委託價格偏離最新行情過大',
        position_limit_exceeded: '預估商品部位超過限額',
        gross_exposure_limit_exceeded: '預估總曝險超過限額',
        daily_loss_limit_exceeded: '每日損失已達限額',
        instrument_risk_unavailable: '此商品或委託型態沒有風控模型',
        pnl_unavailable: '無法取得帳戶損益',
        order_cooldown: '委託間隔未滿冷卻時間',
        order_rate_exceeded: '每分鐘委託數已達上限',
        outstanding_order_requires_reconciliation: '仍有未完成或待對帳的委託',
        execution_authority_expired: '送單時交易閘門已關閉或逾時',
        odd_lot_quantity_exceeded: '零股單筆超過 999 股',
        odd_lot_not_supported: '此商品（CB）不能下零股',
        invalid_quantity: '數量無效',
        no_signal_condition: '目前行情未觸發策略條件',
        symbol_not_configured: '商品不在策略設定內',
        strategy_error: '策略程式錯誤',
    } as Record<string, string>)[reason] ?? reason;
}

const fallback: AutotradeHealth = {
    api_version: 0,
    environment: 'UNKNOWN',
    trading_gate: 'UNKNOWN',
    strategy_count: 0,
    enabled_strategy_count: 0,
    gateway_status: 'UNAVAILABLE',
    capability_mode: 'OBSERVER',
    broker_connection: { state: 'DISCONNECTED', accounts: [], last_updated_at: null },
};

export function normalizeAutotradeHealth(value: unknown): AutotradeHealth {
    if (!value || typeof value !== 'object') return fallback;
    const raw = value as Record<string, unknown>;
    const environment = raw.environment === 'SIM' || raw.environment === 'PAPER' || raw.environment === 'LIVE'
        ? raw.environment : 'UNKNOWN';
    const trading_gate = raw.trading_gate === 'DISARMED' || raw.trading_gate === 'ARMED' || raw.trading_gate === 'KILLED'
        ? raw.trading_gate : 'UNKNOWN';
    const rawConnection = raw.broker_connection && typeof raw.broker_connection === 'object'
        ? raw.broker_connection as Record<string, unknown> : {};
    const connectionState = rawConnection.state === 'CONNECTING' || rawConnection.state === 'AUTHENTICATED'
        || rawConnection.state === 'MARKET_DATA_READY' || rawConnection.state === 'DEGRADED'
        || rawConnection.state === 'LOST' ? rawConnection.state : 'DISCONNECTED';
    const connectionAccounts = Array.isArray(rawConnection.accounts)
        ? rawConnection.accounts.flatMap((account): AutotradeHealth['broker_connection']['accounts'] => {
            if (!account || typeof account !== 'object') return [];
            const item = account as Record<string, unknown>;
            if ((item.account_type !== 'S' && item.account_type !== 'F') || !isAccountLabel(item.account_label)
                || typeof item.signed !== 'boolean') return [];
            return [{ account_type: item.account_type, account_label: item.account_label, signed: item.signed }];
        }) : [];
    return {
        api_version: typeof raw.api_version === 'number' && Number.isInteger(raw.api_version) && raw.api_version >= 0
            ? raw.api_version : 0,
        environment,
        trading_gate,
        strategy_count: typeof raw.strategy_count === 'number' ? raw.strategy_count : 0,
        enabled_strategy_count: typeof raw.enabled_strategy_count === 'number' ? raw.enabled_strategy_count : 0,
        gateway_status: raw.gateway_status === 'READY' || raw.gateway_status === 'DEGRADED'
            ? raw.gateway_status : 'UNAVAILABLE',
        capability_mode: raw.capability_mode === 'OPERATOR' ? 'OPERATOR' : 'OBSERVER',
        broker_connection: {
            state: connectionState,
            accounts: connectionAccounts,
            last_updated_at: typeof rawConnection.last_updated_at === 'string' ? rawConnection.last_updated_at : null,
        },
    };
}

export async function fetchAutotradeHealth(): Promise<AutotradeHealth> {
    return normalizeAutotradeHealth(await apiGet<unknown>('/api/v1/autotrade/health'));
}

export function normalizeAutotradeConnection(value: unknown): AutotradeConnection {
    return normalizeAutotradeHealth({ broker_connection: value }).broker_connection;
}

export async function fetchAutotradeConnection(): Promise<AutotradeConnection> {
    return normalizeAutotradeConnection(await apiGet<unknown>('/api/v1/autotrade/connection'));
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
            ...(isAccountLabel(order.account_id) ? { account_id: order.account_id } : {}),
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
            ...(typeof order.avg_fill_price === 'string' ? { avg_fill_price: order.avg_fill_price } : {}),
            ...(typeof order.created_at === 'string' && order.created_at ? { created_at: order.created_at } : {}),
            ...(order.quantity_unit === '張' || order.quantity_unit === '股' || order.quantity_unit === '口'
                ? { quantity_unit: order.quantity_unit } : {}),
            updated_at: typeof order.updated_at === 'string' ? order.updated_at : '',
        }];
    }) : [];
    const reconciliation = Array.isArray(raw.reconciliation) ? raw.reconciliation.flatMap((item): AutotradeReconciliationObservation[] => {
        const normalized = normalizeOrders([item])[0];
        if (!normalized || !item || typeof item !== 'object') return [];
        const rawNote = (item as Record<string, unknown>).operator_note;
        const note = rawNote && typeof rawNote === 'object' && typeof (rawNote as Record<string, unknown>).conclusion === 'string'
            ? {
                conclusion: (rawNote as Record<string, unknown>).conclusion as string,
                noted_at: typeof (rawNote as Record<string, unknown>).noted_at === 'string'
                    ? (rawNote as Record<string, unknown>).noted_at as string : null,
            } : null;
        const order = { ...normalized, operator_note: note };
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
    const positions = Array.isArray(raw.positions) ? raw.positions.flatMap((item): AutotradePositionObservation[] => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
        const position = item as Record<string, unknown>;
        const decimal = (field: string) => typeof position[field] === 'string'
            && /^-?\d+(?:\.\d+)?$/.test(position[field]) ? position[field] : null;
        if (typeof position.snapshot_id !== 'string' || !position.snapshot_id
            || (position.account_type !== 'S' && position.account_type !== 'F')
            || !isAccountLabel(position.account_label)
            || typeof position.symbol !== 'string' || !position.symbol
            || (position.side !== 'LONG' && position.side !== 'SHORT')
            || typeof position.observed_at !== 'string') return [];
        const quantity = decimal('quantity');
        const averagePrice = decimal('average_price');
        const marketPrice = decimal('market_price');
        const unrealizedPnl = decimal('unrealized_pnl');
        if (!quantity || !averagePrice || !marketPrice || !unrealizedPnl) return [];
        return [{
            snapshot_id: position.snapshot_id, account_type: position.account_type,
            account_label: position.account_label, symbol: position.symbol, side: position.side,
            quantity, average_price: averagePrice, market_price: marketPrice,
            unrealized_pnl: unrealizedPnl, observed_at: position.observed_at,
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
        positions,
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
                orders_last_minute: nonNegativeInteger(usage.orders_last_minute),
                pending_exposure: textOrNull(usage.pending_exposure),
                daily_loss_used: textOrNull(usage.daily_loss_used),
                daily_loss_remaining: textOrNull(usage.daily_loss_remaining),
                gross_exposure: textOrNull(usage.gross_exposure),
                gross_exposure_remaining: textOrNull(usage.gross_exposure_remaining),
                orders_per_minute_remaining: typeof usage.orders_per_minute_remaining === 'number'
                    ? usage.orders_per_minute_remaining : null,
            },
        },
        reconciliation,
        performance: {
            run_count: typeof performance.run_count === 'number' ? performance.run_count : 0,
            filled_order_count: typeof performance.filled_order_count === 'number' ? performance.filled_order_count : 0,
            realized_pnl: typeof performance.realized_pnl === 'string' ? performance.realized_pnl : null,
            unrealized_pnl: typeof performance.unrealized_pnl === 'string' ? performance.unrealized_pnl : null,
            source: typeof performance.source === 'string' ? performance.source : 'UNAVAILABLE',
            stale: performance.stale !== false,
            observed_at: typeof performance.observed_at === 'string' ? performance.observed_at : null,
            gross_exposure: textOrNull(performance.gross_exposure),
            runs: Array.isArray(performance.runs) ? performance.runs.flatMap(normalizeRunPerformance) : [],
        },
        runtime: normalizeRuntime(raw.runtime),
        today: normalizeToday(raw.today),
    };
}

function textOrNull(value: unknown): string | null {
    return typeof value === 'string' && value ? value : null;
}

function numberOrNull(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function normalizeRuntime(value: unknown): AutotradeRuntime {
    const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
    return {
        mode: typeof raw.mode === 'string' ? raw.mode : 'OBSERVE',
        execution_available: raw.execution_available === true,
        running: raw.running === true,
        last_tick: textOrNull(raw.last_tick),
        last_error: textOrNull(raw.last_error),
        arm_expires_at: numberOrNull(raw.arm_expires_at),
    };
}

function normalizeToday(value: unknown): AutotradeToday {
    const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
    const count = (field: string) => typeof raw[field] === 'number' && Number.isInteger(raw[field]) && (raw[field] as number) >= 0
        ? raw[field] as number : 0;
    return {
        date: textOrNull(raw.date),
        auto_order_count: count('auto_order_count'),
        auto_rejected_count: count('auto_rejected_count'),
        auto_filled_count: count('auto_filled_count'),
        manual_order_count: count('manual_order_count'),
        open_order_count: count('open_order_count'),
    };
}

function normalizeRunPerformance(value: unknown): AutotradeRunPerformance[] {
    if (!value || typeof value !== 'object') return [];
    const raw = value as Record<string, unknown>;
    if (typeof raw.run_id !== 'string') return [];
    const count = (field: string) => typeof raw[field] === 'number' && Number.isInteger(raw[field]) ? raw[field] as number : 0;
    return [{
        run_id: raw.run_id,
        strategy_id: textOrNull(raw.strategy_id),
        version: textOrNull(raw.version),
        state: textOrNull(raw.state),
        started_at: textOrNull(raw.started_at),
        ended_at: textOrNull(raw.ended_at),
        intent_count: count('intent_count'),
        rejected_count: count('rejected_count'),
        reject_rate: numberOrNull(raw.reject_rate),
        fill_count: count('fill_count'),
        closed_trade_count: count('closed_trade_count'),
        win_rate: numberOrNull(raw.win_rate),
        realized_pnl: textOrNull(raw.realized_pnl),
        max_drawdown: textOrNull(raw.max_drawdown),
        avg_slippage_bps: textOrNull(raw.avg_slippage_bps),
        avg_ack_latency_ms: numberOrNull(raw.avg_ack_latency_ms),
        avg_fill_latency_ms: numberOrNull(raw.avg_fill_latency_ms),
    }];
}

/** Records an operator's reconciliation conclusion; it never changes the order or contacts the broker. */
export async function recordReconciliationNote(intentId: string, conclusion: string): Promise<void> {
    await apiPost<unknown>(
        `/api/v1/autotrade/reconciliation/${encodeURIComponent(intentId)}/note`,
        { conclusion },
    );
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

export async function refreshAutotradeReconciliation(
    intentId: string,
    reason: string,
    idempotencyKey: string,
): Promise<ReconciliationRefreshReceipt> {
    const document = await apiPost<Record<string, unknown>>(
        `/api/v1/autotrade/reconciliation/${encodeURIComponent(intentId)}/refresh`,
        { reason }, { headers: { 'Idempotency-Key': idempotencyKey } },
    );
    if (document.intent_id !== intentId || typeof document.reason !== 'string'
        || typeof document.status !== 'string' || typeof document.observed_terminal_facts !== 'number'
        || typeof document.resolved_intents !== 'number' || typeof document.applied !== 'boolean') {
        throw new Error('Gateway returned an invalid reconciliation refresh receipt');
    }
    return {
        intent_id: intentId, reason: document.reason, status: document.status,
        observed_terminal_facts: document.observed_terminal_facts,
        resolved_intents: document.resolved_intents, applied: document.applied,
    };
}

/** Request one explicit local SIM read; it cannot create or dispatch an order. */
export async function refreshAutotradePositionSnapshots(): Promise<PositionSnapshotRefreshReceipt> {
    const document = await apiPost<Record<string, unknown>>('/api/v1/autotrade/positions/refresh', {});
    if (typeof document.item_count !== 'number' || !Number.isInteger(document.item_count) || document.item_count < 0
        || (document.observed_at !== null && typeof document.observed_at !== 'string')) {
        throw new Error('Gateway returned an invalid position snapshot receipt');
    }
    return { item_count: document.item_count, observed_at: document.observed_at };
}

export async function previewReferenceStrategy(action: 'validate' | 'dry-run', strategyId = 'reference-fixture'): Promise<StrategyPreview> {
    const document = await apiPost<Record<string, unknown>>(
        `/api/v1/autotrade/strategies/${encodeURIComponent(strategyId)}/${action}`,
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
        message: `Dry Run：不建立 intent／outbox、不送 broker；風控 ${risk.allowed === true ? '允許' : '拒絕'}${reasons.length ? `（${reasons.map(riskReasonLabel).join('、')}）` : ''}`,
        details: dryRunDetails(document),
    };
}

const LOT_TEXT: Record<string, (quantity: number) => string> = {
    Common: (quantity) => `整股 ${quantity} 張`,
    IntradayOdd: (quantity) => `零股 ${quantity} 股`,
};

/** Turns a dry-run document into sentences; it never includes strategy source code. */
export function dryRunDetails(document: Record<string, unknown>): string[] {
    const details: string[] = [];
    if (typeof document.market_ready === 'boolean') {
        details.push(document.market_ready ? '行情：已取得有效成交價' : '行情：尚無有效成交價（非交易時間或行情過期），啟動後不會下單');
    }
    if (typeof document.condition_met === 'boolean') {
        details.push(document.condition_met ? '條件：目前成立，啟動並啟用交易閘門後可能立即下單' : '條件：目前不成立，啟動後持續監測');
    }
    const proposals = Array.isArray(document.proposals) ? document.proposals : [];
    for (const item of proposals) {
        if (!item || typeof item !== 'object') continue;
        const raw = item as Record<string, unknown>;
        const proposal = raw.proposal && typeof raw.proposal === 'object' ? raw.proposal as Record<string, unknown> : {};
        const verdict = raw.risk && typeof raw.risk === 'object' ? raw.risk as Record<string, unknown> : {};
        const side = proposal.side === 'BUY' ? '買' : proposal.side === 'SELL' ? '賣' : String(proposal.side ?? '');
        const parts = (Array.isArray(raw.orders) ? raw.orders : [])
            .map((part) => part && typeof part === 'object' ? part as Record<string, unknown> : null)
            .filter((part): part is Record<string, unknown> => part !== null && typeof part.quantity === 'number')
            .map((part) => (LOT_TEXT[String(part.lot)] ?? ((quantity: number) => `${String(part.lot)} ${quantity}`))(part.quantity as number));
        const reasons = Array.isArray(verdict.reasons)
            ? verdict.reasons.filter((reason): reason is string => typeof reason === 'string') : [];
        details.push(
            `預計委託：${String(proposal.symbol ?? '')} ${side} ${String(proposal.quantity ?? '')} 股 @ ${String(proposal.price ?? '')}`
            + (parts.length ? `（${parts.join('＋')}）` : '')
            + ` · 風控${verdict.allowed === true ? '允許' : `拒絕${reasons.length ? `（${reasons.map(riskReasonLabel).join('、')}）` : ''}`}`,
        );
    }
    return details;
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
    strategyId = 'reference-fixture',
): Promise<StrategyControlReceipt> {
    const document = await apiPost<Record<string, unknown>>(
        `/api/v1/autotrade/strategies/${encodeURIComponent(strategyId)}/${action}`,
        { reason }, { headers: { 'Idempotency-Key': idempotencyKey } },
    );
    const validState = document.state === 'RUNNING' || document.state === 'PAUSED' || document.state === 'STOPPED';
    if (document.strategy_id !== strategyId || document.action !== action
        || typeof document.reason !== 'string' || !validState || typeof document.enabled !== 'boolean'
        || typeof document.run_id !== 'string' || typeof document.applied !== 'boolean') {
        throw new Error('Gateway returned an invalid strategy control receipt');
    }
    return {
        strategy_id: strategyId, action, reason: document.reason,
        state: document.state as StrategyControlReceipt['state'], enabled: document.enabled, run_id: document.run_id,
        applied: document.applied,
    };
}
