import { TradingRuntimeControls } from './trading-runtime-controls';
import { useEffect, useState } from 'react';
import {
    applyAutotradeKillSwitch,
    applyReferenceStrategyControl,
    fetchAutotradeOrder,
    previewReferenceStrategy,
    recordReconciliationNote,
    refreshAutotradeReconciliation,
    refreshAutotradePositionSnapshots,
    riskReasonLabel,
    type AutotradeOverview,
    type AutotradeOrderObservation,
    type AutotradePositionObservation,
    type AutotradeReconciliationObservation,
    type AutotradeRiskVerdict,
    type AutotradeRun,
    type AutotradeRunPerformance,
    type AutotradeSignal,
    type StrategyControlAction,
    type StrategyPreview,
} from '../autotrade/client';
import { autotradeObserverStore, type AutotradeObserverState } from '../autotrade/observer-store';

export type AutotradeObserverPanel =
    | 'strategy_control'
    | 'signal_timeline'
    | 'auto_orders'
    | 'risk_limits'
    | 'reconciliation'
    | 'run_performance';

const PANEL_COPY: Record<AutotradeObserverPanel, { title: string; empty: string }> = {
    strategy_control: {
        title: '策略控制',
        empty: '策略 runtime 尚未啟用。控制只改變策略 lifecycle；不會 ARM 或送出委託。',
    },
    signal_timeline: {
        title: '訊號與決策時間線',
        empty: '尚無訊號或風控決策；沒有以示範資料取代真實事件。',
    },
    auto_orders: {
        title: '自動委託與持倉',
        empty: '尚無自動委託或策略歸屬的持倉。',
    },
    risk_limits: {
        title: '風控與限額',
        empty: 'Risk Engine 限額尚未載入；所有新 mutation 仍由 Gateway 拒絕。',
    },
    reconciliation: {
        title: '對帳佇列',
        empty: '沒有 UNKNOWN、RECONCILING 或 MANUAL_REVIEW 委託。',
    },
    run_performance: {
        title: '執行績效',
        empty: '尚無已完成策略執行；損益不以估算資料呈現。',
    },
};

const LOADING_TEXT = '載入中…';

/** Statuses whose broker outcome is not known; they are never resent automatically. */
const UNCERTAIN_STATUSES = new Set(['UNKNOWN', 'RECONCILING', 'MANUAL_REVIEW']);

export const ORDER_STATUS_LABELS: Record<string, string> = {
    APPROVED: '排隊待送',
    SENDING: '送出中',
    PENDING_SUBMIT: '已送出，等待券商確認',
    SUBMITTED: '券商已接受',
    PART_FILLED: '部分成交',
    FILLED: '全部成交',
    CANCELLED: '已取消',
    BROKER_REJECTED: '券商拒絕',
    RISK_REJECTED: '風控拒絕',
    UNKNOWN: '狀態不明',
    RECONCILING: '對帳中',
    MANUAL_REVIEW: '待人工審查',
};

export function orderStatusLabel(status: string): string {
    return ORDER_STATUS_LABELS[status] ?? status;
}

const SOURCE_LABELS: Record<string, string> = {
    STRATEGY: '策略', SYSTEM_TRIGGER: '系統觸發', MANUAL_UI: '手動',
};

const STRATEGY_STATE_LABELS: Record<string, string> = {
    RUNNING: '運行中', PAUSED: '已暫停', STOPPED: '已停止', DISABLED: '未啟動', COMPLETED: '已完成',
};

function statusTone(status: string): string {
    if (UNCERTAIN_STATUSES.has(status) || status === 'BROKER_REJECTED') return '#ef4444';
    if (status === 'RISK_REJECTED') return '#f0b44d';
    if (status === 'FILLED' || status === 'PART_FILLED') return '#73c991';
    return '#8892a6';
}

function sideText(side: string | undefined): string {
    return side === 'BUY' ? '買' : side === 'SELL' ? '賣' : side ?? '';
}

/** A Taipei wall-clock time for display; unparseable values are shown as received. */
export function timeText(value: string | null | undefined): string {
    if (!value) return '時間未知';
    const parsed = Date.parse(value);
    if (!Number.isFinite(parsed)) return value;
    return new Date(parsed).toLocaleString('zh-TW', {
        timeZone: 'Asia/Taipei', hour12: false,
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
}

export type TimelineFilters = {
    strategy: string;
    symbol: string;
    status: string;
    from: string;
    to: string;
};

const EMPTY_TIMELINE_FILTERS: TimelineFilters = {
    strategy: '', symbol: '', status: '', from: '', to: '',
};

function matchesTimelineFilter(query: string, value: string | null | undefined): boolean {
    return !query.trim() || Boolean(value && value.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
}

function detailString(details: Record<string, unknown>, field: string): string | null {
    return typeof details[field] === 'string' ? details[field] as string : null;
}

function occursWithin(value: string | null | undefined, from: string, to: string): boolean {
    if (!from && !to) return true;
    if (!value) return false;
    const occurredAt = Date.parse(value);
    if (!Number.isFinite(occurredAt)) return false;
    if (from) {
        const fromAt = Date.parse(from);
        if (!Number.isFinite(fromAt) || occurredAt < fromAt) return false;
    }
    if (to) {
        const toAt = Date.parse(to);
        if (!Number.isFinite(toAt) || occurredAt > toAt) return false;
    }
    return true;
}

/** Filters durable facts locally; it never changes a cursor, broker query, or order state. */
export function filterTimeline(overview: AutotradeOverview, filters: TimelineFilters) {
    const signalMatches = (signal: AutotradeSignal) =>
        matchesTimelineFilter(filters.strategy, signal.strategy_id)
        && matchesTimelineFilter(filters.symbol, signal.symbol)
        && matchesTimelineFilter(filters.status, signal.status)
        && occursWithin(signal.observed_at, filters.from, filters.to);
    const decisionMatches = (decision: AutotradeOrderObservation) =>
        matchesTimelineFilter(filters.strategy, decision.strategy_id)
        && matchesTimelineFilter(filters.symbol, decision.symbol)
        && (matchesTimelineFilter(filters.status, decision.status)
            || matchesTimelineFilter(filters.status, orderStatusLabel(decision.status)))
        && occursWithin(decision.decided_at ?? decision.updated_at, filters.from, filters.to);
    const eventMatches = (event: AutotradeOverview['audit_events'][number]) =>
        matchesTimelineFilter(filters.strategy, detailString(event.details, 'strategy_id'))
        && matchesTimelineFilter(filters.symbol, detailString(event.details, 'symbol'))
        && matchesTimelineFilter(filters.status, detailString(event.details, 'status') ?? event.event_type)
        && occursWithin(event.occurred_at, filters.from, filters.to);
    return {
        signals: overview.signals.filter(signalMatches),
        decisions: overview.decisions.filter(decisionMatches),
        auditEvents: overview.audit_events.filter(eventMatches),
    };
}

export type ChainStepState = 'done' | 'failed' | 'waiting' | 'skipped';
export type ChainStep = { label: string; state: ChainStepState; text: string; at?: string | null };
export type DecisionChain = {
    intentId: string;
    title: string;
    status: string;
    steps: ChainStep[];
};

/**
 * One intent's path from trigger to fill, built only from durable facts:
 * 觸發 → 風控 → 送出 → 券商確認 → 成交.  After a failed step the rest are skipped.
 */
export function decisionChain(overview: AutotradeOverview, decision: AutotradeOrderObservation): DecisionChain {
    const order = overview.orders.find((item) => item.intent_id === decision.intent_id) ?? decision;
    const status = order.status || decision.status;
    const signal = overview.signals.find((item) => item.intent_id === decision.intent_id);
    const events = overview.audit_events.filter((event) => event.intent_id === decision.intent_id);
    const event = (type: string) => events.find((item) => item.event_type === type);
    const steps: ChainStep[] = [];
    let blocked = false;
    const push = (label: string, state: ChainStepState, text: string, at?: string | null) => {
        if (blocked) {
            steps.push({ label, state: 'skipped', text: '—' });
            return;
        }
        steps.push({ label, state, text, at });
        if (state === 'failed') blocked = true;
    };

    push('觸發', 'done', signal
        ? `${signal.signal_type} · ${signal.symbol} ${sideText(signal.side)}`
        : `${SOURCE_LABELS[decision.source] ?? decision.source}委託`, signal?.observed_at ?? order.created_at ?? null);

    const reasons = decision.risk_reasons.map(riskReasonLabel).join('、');
    if (decision.risk_decision_missing) push('風控', 'failed', '風控判定紀錄缺失；不得據此重送');
    else if (decision.allowed === false || status === 'RISK_REJECTED') {
        push('風控', 'failed', `拒絕${reasons ? `（${reasons}）` : ''}`, decision.decided_at);
    } else if (decision.allowed === true) push('風控', 'done', '允許', decision.decided_at);
    else push('風控', 'waiting', '尚無判定');

    const refused = event('DISPATCH_REFUSED');
    const bound = event('BROKER_ORDER_BOUND');
    if (refused) {
        const refusedReasons = Array.isArray(refused.details.risk_reasons)
            ? (refused.details.risk_reasons as unknown[]).filter((item): item is string => typeof item === 'string').map(riskReasonLabel)
            : [];
        push('送出', 'failed', `送出前複核拒絕${refusedReasons.length ? `（${refusedReasons.join('、')}）` : ''}`, refused.occurred_at);
    } else if (UNCERTAIN_STATUSES.has(status) || event('DELIVERY_UNKNOWN')) {
        push('送出', 'failed', '無法確定是否已到券商；不會自動重送，請到對帳佇列處理', event('DELIVERY_UNKNOWN')?.occurred_at);
    } else if (bound || order.broker_order_id) {
        push('送出', 'done', `已送出${order.broker_order_id ? ` · 券商單號 ${order.broker_order_id}` : ''}`, bound?.occurred_at);
    } else if (status === 'APPROVED') {
        push('送出', 'waiting', '排隊中：同商品前一筆委託取得券商確認後才送出');
    } else if (status === 'SENDING') push('送出', 'waiting', '送出中');
    else push('送出', 'waiting', '尚未送出');

    if (status === 'BROKER_REJECTED') push('券商確認', 'failed', '券商拒絕');
    else if (['SUBMITTED', 'PART_FILLED', 'FILLED', 'CANCELLED'].includes(status)) push('券商確認', 'done', '券商已接受');
    else push('券商確認', 'waiting', status === 'PENDING_SUBMIT' ? '等待券商確認' : '—');

    const filled = order.filled_quantity ?? 0;
    const unit = order.quantity_unit ?? '';
    const average = order.avg_fill_price ? ` · 均價 ${order.avg_fill_price}` : '';
    if (status === 'FILLED') push('成交', 'done', `全部成交 ${filled ? filledText(order) : `${order.quantity}${unit}`}${average}`);
    else if (filled > 0) push('成交', 'waiting', `部分成交 ${filledText(order)}（委託 ${order.quantity}${unit}）${average}`);
    else if (status === 'CANCELLED') push('成交', 'skipped', '已取消，未成交');
    else push('成交', 'waiting', '尚未成交');

    return {
        intentId: decision.intent_id,
        title: `${SOURCE_LABELS[decision.source] ?? decision.source}${decision.strategy_id ? ` ${decision.strategy_id}` : ''} · `
            + `${decision.symbol} ${sideText(decision.side)} ${decision.quantity}${unit}${decision.limit_price ? ` @ ${decision.limit_price}` : ''}`,
        status,
        steps,
    };
}

const BOARD_LOT_SHARES = 1000;

/**
 * Fill quantities are recorded in risk units: shares for stocks and CBs,
 * contracts for futures. A board-lot order (張) therefore fills in shares.
 */
export function filledText(order: Pick<AutotradeOrderObservation, 'filled_quantity' | 'quantity_unit'>, filled = order.filled_quantity ?? 0): string {
    if (order.quantity_unit === '張') {
        const lots = filled / BOARD_LOT_SHARES;
        return `${filled}股${Number.isInteger(lots) ? `（${lots}張）` : ''}`;
    }
    return `${filled}${order.quantity_unit ?? ''}`;
}

/** Orders an operator must look at first: uncertain outcomes, then newest. */
export function sortOrdersForReview<T extends Partial<AutotradeOrderObservation>>(orders: T[]): T[] {
    const at = (order: T) => Date.parse(order.created_at ?? order.updated_at ?? '') || 0;
    return [...orders].sort((left, right) => {
        const urgent = Number(UNCERTAIN_STATUSES.has(right.status ?? '')) - Number(UNCERTAIN_STATUSES.has(left.status ?? ''));
        return urgent || at(right) - at(left);
    });
}

export type RiskUsageRow = { label: string; used: string; limit: string; remaining: string };

function decimalText(value: unknown): string | null {
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return value;
    return null;
}

/** Used versus allowed; remaining headroom only from a fresh account snapshot. */
export function riskUsageRows(overview: AutotradeOverview): RiskUsageRow[] {
    const { limits, usage } = overview.risk;
    const rows: RiskUsageRow[] = [];
    const perMinute = decimalText(limits.max_orders_per_minute);
    if (perMinute !== null && typeof usage.orders_last_minute === 'number') {
        rows.push({
            label: '每分鐘委託數', used: String(usage.orders_last_minute), limit: perMinute,
            remaining: String(Math.max(0, Number(perMinute) - usage.orders_last_minute)),
        });
    }
    const authoritative = usage.authoritative_remaining_available;
    const dailyLoss = decimalText(limits.max_daily_loss);
    if (dailyLoss !== null) {
        rows.push({
            label: '每日損失', used: authoritative ? usage.daily_loss_used ?? '—' : '—', limit: dailyLoss,
            remaining: authoritative ? usage.daily_loss_remaining ?? '—' : '需最新帳務快照',
        });
    }
    const gross = decimalText(limits.max_gross_exposure);
    if (gross !== null) {
        const pending = usage.pending_exposure ?? '0';
        rows.push({
            label: '總曝險（持倉＋未成交預留）',
            used: authoritative && usage.gross_exposure ? `${usage.gross_exposure} ＋ ${pending}` : `— ＋ ${pending}`,
            limit: gross,
            remaining: authoritative ? usage.gross_exposure_remaining ?? '—' : '需最新帳務快照',
        });
    }
    return rows;
}

type StrategyObservation = {
    strategy_id?: string;
    display_name?: string;
    version?: string;
    state?: string;
    enabled?: boolean;
    allowed_symbols?: unknown;
    config?: Record<string, unknown>;
    config_schema?: { name: string; label: string }[];
    last_error?: string | null;
    last_market_at?: string | null;
    market_driven?: boolean;
    lifecycle_enabled?: boolean;
    private_strategy?: boolean;
};

const HEARTBEAT_STALE_MS = 30_000;

function latestRun(runs: AutotradeRun[], strategyId: string | undefined): AutotradeRun | null {
    const own = runs.filter((run) => run.strategy_id === strategyId);
    return own.sort((left, right) => (Date.parse(right.updated_at) || 0) - (Date.parse(left.updated_at) || 0))[0] ?? null;
}

/** Heartbeat age text, with a warning when a running strategy stopped beating. */
export function heartbeatText(run: AutotradeRun | null, state: string | undefined, now: number = Date.now()): { text: string; stale: boolean } {
    if (!run?.heartbeat_at) return { text: '尚無心跳紀錄', stale: state === 'RUNNING' };
    const age = now - Date.parse(run.heartbeat_at);
    if (!Number.isFinite(age)) return { text: run.heartbeat_at, stale: false };
    const seconds = Math.max(0, Math.round(age / 1000));
    const stale = state === 'RUNNING' && age > HEARTBEAT_STALE_MS;
    return { text: `${seconds} 秒前${stale ? '（超過 30 秒未更新，執行服務可能已停止）' : ''}`, stale };
}

function configText(strategy: StrategyObservation): string {
    const config = strategy.config ?? {};
    const labels = new Map((strategy.config_schema ?? []).map((field) => [field.name, field.label]));
    const entries = Object.entries(config);
    return entries.length
        ? entries.map(([key, value]) => `${labels.get(key) ?? key}：${typeof value === 'string' || typeof value === 'number' ? value : JSON.stringify(value)}`).join('；')
        : '使用預設設定';
}

const START_LIMIT_KEYS = ['max_order_quantity', 'max_order_notional', 'max_position_per_symbol', 'max_daily_loss', 'max_orders_per_minute', 'cooldown_seconds'];

function countFor(kind: AutotradeObserverPanel, overview: AutotradeOverview): number {
    switch (kind) {
        case 'strategy_control': return overview.strategies.length;
        case 'signal_timeline': return overview.signals.length + overview.decisions.length + overview.audit_events.length;
        case 'auto_orders': return automatedOrderObservations(overview.orders).length + overview.positions.length;
        case 'risk_limits': return overview.risk.last_verdict === null ? 0 : 1;
        case 'reconciliation': return overview.reconciliation.length;
        case 'run_performance': return overview.performance.run_count;
    }
}

export function panelSummary(
    kind: AutotradeObserverPanel,
    overview: AutotradeOverview | null,
    error: string | null,
): string {
    if (error) return error;
    if (!overview) return LOADING_TEXT;

    switch (kind) {
        case 'risk_limits':
            return Object.keys(overview.risk.limits).length > 0
                ? `已載入風控限額；目前閘門 ${overview.health.trading_gate}。行情或帳務資料不足時拒絕新委託。`
                : PANEL_COPY[kind].empty;
        case 'signal_timeline':
            return overview.signals.length > 0 || overview.decisions.length > 0 || overview.audit_events.length > 0
                ? '以下為策略訊號（durable signal）、委託決策（durable intent）、風控判定與執行事件；不以示範資料取代真實事件。'
                : PANEL_COPY[kind].empty;
        case 'auto_orders':
            return automatedOrderObservations(overview.orders).length > 0 || overview.positions.length > 0
                ? '以下為可對帳的自動委託與策略歸屬持倉觀察資料。'
                : PANEL_COPY[kind].empty;
        case 'reconciliation':
            return overview.reconciliation.length > 0
                ? '以下委託需要人工對帳；系統不會自動重送。'
                : PANEL_COPY[kind].empty;
        case 'run_performance':
            return overview.performance.run_count > 0
                ? '以下呈現每次策略執行的估算績效與帳戶損益快照；帳戶損益不等於個別策略績效。'
                : PANEL_COPY[kind].empty;
        case 'strategy_control':
            return overview.strategies.some(item => (item as StrategyObservation).market_driven)
                ? '策略依已儲存設定監測行情；交易閘門啟用時可能產生委託。暫停與停止不取消既有委託。'
                : PANEL_COPY[kind].empty;
    }
}

const CONTROL_LABELS: Record<StrategyControlAction, string> = {
    start: '啟動', pause: '暫停', resume: '恢復', stop: '停止',
};

export function AutotradeObserverPanels({
    kind,
    onSelectCode,
}: {
    kind: AutotradeObserverPanel;
    onSelectCode?: (code: string) => void;
}) {
    const [observer, setObserver] = useState<AutotradeObserverState>(autotradeObserverStore.snapshot());
    const [controlReason, setControlReason] = useState('');
    const [strategyPreview, setStrategyPreview] = useState<StrategyPreview | null>(null);
    const [controlMessage, setControlMessage] = useState<string | null>(null);
    const [pendingControl, setPendingControl] = useState<{ action: StrategyControlAction; preview: StrategyPreview | null } | null>(null);
    const [killSwitchMessage, setKillSwitchMessage] = useState<string | null>(null);
    const [reconciliationMessage, setReconciliationMessage] = useState<string | null>(null);
    const [positionRefreshMessage, setPositionRefreshMessage] = useState<string | null>(null);
    const [showAllOrders, setShowAllOrders] = useState(false);
    const [timelineFilters, setTimelineFilters] = useState<TimelineFilters>(EMPTY_TIMELINE_FILTERS);
    const [clock, setClock] = useState(() => Date.now());
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    useEffect(() => {
        if (kind !== 'strategy_control') return;
        const timer = setInterval(() => setClock(Date.now()), 5_000);
        return () => clearInterval(timer);
    }, [kind]);
    const { overview, error } = observer;
    // PAPER has no broker market data, so a chart could not resolve the code.
    const selectCode = overview?.health.environment === 'PAPER' ? undefined : onSelectCode;
    const copy = PANEL_COPY[kind];
    const timeline = overview ? filterTimeline(overview, timelineFilters) : null;
    const decisionIds = new Set(overview?.decisions.map((decision) => decision.intent_id) ?? []);
    const looseSignals = timeline?.signals.filter((signal) => !decisionIds.has(signal.intent_id)) ?? [];
    const systemEvents = timeline?.auditEvents.filter((event) => !event.intent_id || !decisionIds.has(event.intent_id)) ?? [];
    const chains = overview && timeline ? timeline.decisions.map((decision) => decisionChain(overview, decision)) : [];
    const timelineCount = chains.length + looseSignals.length + systemEvents.length;
    const autoOrders = overview
        ? sortOrdersForReview(showAllOrders ? overview.orders : automatedOrderObservations(overview.orders))
        : [];
    const count = overview
        ? kind === 'auto_orders' && showAllOrders ? overview.orders.length
            : kind === 'signal_timeline' ? timelineCount
                : countFor(kind, overview)
        : 0;
    const lifecycleEnabled = overview?.strategies.some((item) =>
        Boolean(item && typeof item === 'object' && (item as StrategyObservation).lifecycle_enabled === true),
    ) ?? false;
    const observerOnly = overview?.health.capability_mode === 'OBSERVER';
    const activeStrategy = overview?.strategies[0] as StrategyObservation | undefined;
    const strategyId = activeStrategy?.strategy_id ?? 'reference-fixture';
    const needsReason = activeStrategy?.market_driven === true;
    const openStrategyOrders = overview?.orders.filter((order) =>
        order.strategy_id === strategyId && ['APPROVED', 'SENDING', 'PENDING_SUBMIT', 'SUBMITTED', 'PART_FILLED'].includes(order.status),
    ).length ?? 0;
    const runPreview = (action: 'validate' | 'dry-run') => {
        void previewReferenceStrategy(action, strategyId)
            .then(setStrategyPreview)
            .catch(() => setStrategyPreview({ mode: action === 'validate' ? 'VALIDATE' : 'DRY_RUN', message: '策略預覽目前無法連線到 Gateway' }));
    };
    const runKillSwitch = () => {
        if (overview?.health.trading_gate === 'KILLED') {
            setKillSwitchMessage('Kill Switch 已啟用；此介面沒有解除鎖定功能。');
            return;
        }
        const reason = needsReason ? controlReason : globalThis.prompt('請輸入啟用 Kill Switch 的原因；此操作只會阻擋新 mutation。');
        if (!reason?.trim()) return;
        const confirmed = globalThis.confirm?.('確定啟用 Kill Switch？所有新委託都會被拒絕；既有委託與持倉不會自動取消或平倉。') ?? true;
        if (!confirmed) return;
        void applyAutotradeKillSwitch(reason.trim(), globalThis.crypto.randomUUID())
            .then((receipt) => {
                setKillSwitchMessage(receipt.applied ? `Kill Switch 已啟用：${receipt.reason}` : `Kill Switch 已存在：${receipt.reason}`);
                return autotradeObserverStore.refresh();
            })
            .catch(() => setKillSwitchMessage('Kill Switch 請求失敗；請確認 Gateway 連線與原因。'));
    };
    const applyControl = (action: StrategyControlAction) => {
        const reason = needsReason ? controlReason : globalThis.prompt(`請輸入${CONTROL_LABELS[action]}策略的原因；此操作不會解除交易閘門；閘門啟用時策略可能產生委託。`);
        if (!reason?.trim()) return;
        setPendingControl(null);
        void applyReferenceStrategyControl(action, reason.trim(), globalThis.crypto.randomUUID(), strategyId)
            .then((receipt) => {
                setControlMessage(`${CONTROL_LABELS[action]}${receipt.applied ? '完成' : '已重播'}：${STRATEGY_STATE_LABELS[receipt.state] ?? receipt.state} · 執行 ${receipt.run_id.slice(0, 8)}`);
                return autotradeObserverStore.refresh();
            })
            .catch(() => setControlMessage(`${CONTROL_LABELS[action]}策略失敗；請確認 Gateway 是否已啟用 SIM lifecycle。`));
    };
    /** Start and resume can create orders, so both show a summary and a dry run first. */
    const requestControl = (action: StrategyControlAction) => {
        setPendingControl({ action, preview: null });
        if (action === 'pause' || action === 'stop') return;
        void previewReferenceStrategy('dry-run', strategyId)
            .then((preview) => setPendingControl((current) => current?.action === action ? { action, preview } : current))
            .catch(() => setPendingControl((current) => current?.action === action
                ? { action, preview: { mode: 'DRY_RUN', message: 'Dry Run 無法取得；請確認 Gateway 連線後再啟動。' } } : current));
    };
    const runReconciliationRefresh = (intentId: string) => {
        const reason = globalThis.prompt('請輸入重新查詢此筆委託的原因；不會重送或建立新委託。');
        if (!reason?.trim()) return;
        void refreshAutotradeReconciliation(intentId, reason.trim(), globalThis.crypto.randomUUID())
            .then((receipt) => {
                setReconciliationMessage(
                    `對帳查詢${receipt.applied ? '完成' : '已重播'}：${receipt.status} · `
                    + `${receipt.resolved_intents} 筆結案／${receipt.observed_terminal_facts} 個券商最終狀態`,
                );
                return autotradeObserverStore.refresh();
            })
            .catch(() => setReconciliationMessage('對帳查詢未完成；請檢查 SIM reconciliation 設定與 durable case。'));
    };
    const runPositionRefresh = () => {
        void refreshAutotradePositionSnapshots()
            .then((receipt) => {
                setPositionRefreshMessage(
                    receipt.item_count > 0
                        ? `已保存 ${receipt.item_count} 筆 SIM 持倉快照；${timeText(receipt.observed_at)}`
                        : 'SIM 帳戶目前沒有持倉；未以示範資料取代空狀態。',
                );
                return autotradeObserverStore.refresh();
            })
            .catch(() => setPositionRefreshMessage('SIM 持倉快照未取得；保留最後一次 durable 資料。'));
    };
    const loading = !overview && !error;
    return (
        <section style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>{copy.title}</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Badge label='環境' value={overview?.health.environment ?? '—'} tone='#4aa3ff' />
                <Badge label='閘門' value={overview?.health.trading_gate ?? '—'} tone='#f0b44d' />
                <Badge label='模式' value={overview?.health.capability_mode ?? '—'} tone='#8892a6' />
                <Badge label='項目' value={overview ? String(count) : '—'} tone='#8892a6' />
            </div>
            <div role={loading ? 'status' : undefined} style={{ color: !overview && error ? '#ef4444' : '#aeb7c6', lineHeight: 1.6 }}>
                {panelSummary(kind, overview, overview ? null : error)}
            </div>
            {observer.stale && (
                <div role='status' style={{ color: '#f0b44d' }}>
                    Gateway 更新暫時失敗；保留最後一次成功的唯讀資料，未重送任何委託。
                </div>
            )}

            {kind === 'strategy_control' && (
                <>
                    {overview?.strategies.map((item, index) => {
                        const strategy = item as StrategyObservation;
                        const run = latestRun(overview.runs, strategy.strategy_id);
                        const heartbeat = heartbeatText(run, strategy.state, clock);
                        const symbols = Array.isArray(strategy.allowed_symbols) ? strategy.allowed_symbols.map(String) : [];
                        return <div key={`${strategy.strategy_id ?? 'strategy'}-${index}`} aria-label='策略狀態'
                            style={{ border: '1px solid #3a4050', borderRadius: 4, padding: 8, display: 'grid', gap: 3 }}>
                            <div>
                                <b>{strategy.display_name ?? strategy.strategy_id ?? '未命名策略'}</b>
                                {strategy.version ? ` · 版本 ${strategy.version}` : ''}
                                {' · '}<span style={{ color: strategy.state === 'RUNNING' ? '#73c991' : '#f0b44d' }}>
                                    {STRATEGY_STATE_LABELS[strategy.state ?? ''] ?? strategy.state ?? '狀態未知'}
                                </span>
                                {strategy.private_strategy ? ' · 私有策略（邏輯不在程式碼庫）' : ''}
                            </div>
                            <div style={{ color: '#aeb7c6' }}>監看商品：{symbols.length ? symbols.join('、') : '無'}</div>
                            <div style={{ color: heartbeat.stale ? '#ef4444' : '#aeb7c6' }}>
                                心跳：{heartbeat.text}{run ? ` · 執行 ${run.run_id.slice(0, 8)}` : ''}
                                {strategy.last_market_at ? ` · 最新成交 ${timeText(strategy.last_market_at)}` : ''}
                            </div>
                            {strategy.last_error && <div style={{ color: '#f0b44d' }}>目前狀態：{strategy.last_error}</div>}
                        </div>;
                    })}
                    {activeStrategy?.market_driven && !observerOnly && <>
                        <label>操作原因 <input value={controlReason} onChange={event => setControlReason(event.target.value)} placeholder="閘門與策略操作的紀錄原因" /></label>
                        <TradingRuntimeControls strategyId={strategyId} reason={controlReason} />
                    </>}
                    {strategyPreview && <PreviewText preview={strategyPreview} />}
                    {observerOnly ? <div role='status' style={{ color: '#8892a6' }}>遠端 observer mode：不提供策略、Kill Switch 或下單控制。</div> : (
                        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            <button onClick={() => runPreview('validate')} title='唯讀檢查；不啟動策略'>驗證策略</button>
                            <button onClick={() => runPreview('dry-run')} title='不建立 intent、outbox 或 broker 請求'>Dry Run</button>
                            {(['start', 'pause', 'resume', 'stop'] as const).map((action) => (
                                <button key={action}
                                    disabled={!lifecycleEnabled || (needsReason && !controlReason.trim())}
                                    onClick={() => requestControl(action)}
                                    title={lifecycleEnabled ? {
                                        start: '先顯示設定、風控限額與 Dry Run，確認後才啟動',
                                        pause: '暫停新策略訊號；既有委託不變',
                                        resume: '先顯示 Dry Run，確認後恢復行情監測',
                                        stop: '停止策略；既有委託與持倉不變',
                                    }[action] : 'Gateway 未啟用 SIM lifecycle'}
                                >{{ start: '開始', pause: '暫停', resume: '恢復', stop: '停止' }[action]}</button>
                            ))}
                            <button onClick={runKillSwitch} title='需要輸入原因；只能將 Gateway 加嚴為 KILLED'>啟用 Kill Switch</button>
                        </div>
                    )}
                    {pendingControl && overview && activeStrategy && !observerOnly && (
                        <div role='dialog' aria-label={`確認${CONTROL_LABELS[pendingControl.action]}策略`}
                            style={{ border: '1px solid #f0b44d', borderRadius: 4, padding: 8, display: 'grid', gap: 4 }}>
                            <b>確認{CONTROL_LABELS[pendingControl.action]}「{activeStrategy.display_name ?? strategyId}」</b>
                            {(pendingControl.action === 'start' || pendingControl.action === 'resume') ? <>
                                <div>監看商品：{Array.isArray(activeStrategy.allowed_symbols) ? activeStrategy.allowed_symbols.map(String).join('、') || '無' : '無'}</div>
                                <div>策略設定：{configText(activeStrategy)}</div>
                                <div>風控限額：{START_LIMIT_KEYS.filter((key) => key in overview.risk.limits)
                                    .map((key) => `${riskLabel(key)} ${riskValueText(overview.risk.limits[key])}`).join('；')}</div>
                                <div>交易閘門：{overview.health.trading_gate}{overview.health.trading_gate === 'ARMED' ? '（已啟用，條件成立時會立即下單）' : '（未啟用，不會送出委託）'}</div>
                                {pendingControl.preview ? <PreviewText preview={pendingControl.preview} /> : <div role='status'>正在執行 Dry Run…</div>}
                            </> : (
                                <div>
                                    {pendingControl.action === 'pause' ? '暫停後不再產生新訊號。' : '停止後結束本次執行，不再產生新訊號。'}
                                    {openStrategyOrders > 0
                                        ? `此策略仍有 ${openStrategyOrders} 筆未完成委託，不會自動取消；如需取消請到委託面板處理。`
                                        : '此策略目前沒有未完成委託。'}
                                    持倉不會自動平倉。
                                </div>
                            )}
                            <div style={{ display: 'flex', gap: 8 }}>
                                <button
                                    disabled={(pendingControl.action === 'start' || pendingControl.action === 'resume') && !pendingControl.preview}
                                    onClick={() => applyControl(pendingControl.action)}
                                >確認{CONTROL_LABELS[pendingControl.action]}</button>
                                <button onClick={() => setPendingControl(null)}>取消</button>
                            </div>
                        </div>
                    )}
                    {controlMessage && <div role='status' style={{ color: '#aeb7c6' }}>{controlMessage}</div>}
                    {killSwitchMessage && <div role='status' style={{ color: '#ef4444' }}>{killSwitchMessage}</div>}
                </>
            )}

            {kind === 'signal_timeline' && overview && timeline && (
                <>
                    <div role='group' aria-label='時間線篩選' style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <label>策略 <input aria-label='篩選策略' value={timelineFilters.strategy} onChange={(event) => setTimelineFilters((current) => ({ ...current, strategy: event.target.value }))} /></label>
                        <label>商品 <input aria-label='篩選商品' value={timelineFilters.symbol} onChange={(event) => setTimelineFilters((current) => ({ ...current, symbol: event.target.value }))} /></label>
                        <label>狀態 <input aria-label='篩選狀態' value={timelineFilters.status} onChange={(event) => setTimelineFilters((current) => ({ ...current, status: event.target.value }))} /></label>
                        <label>起始時間 <input aria-label='篩選起始時間' type='datetime-local' value={timelineFilters.from} onChange={(event) => setTimelineFilters((current) => ({ ...current, from: event.target.value }))} /></label>
                        <label>結束時間 <input aria-label='篩選結束時間' type='datetime-local' value={timelineFilters.to} onChange={(event) => setTimelineFilters((current) => ({ ...current, to: event.target.value }))} /></label>
                        {(timelineFilters.strategy || timelineFilters.symbol || timelineFilters.status || timelineFilters.from || timelineFilters.to) && <button onClick={() => setTimelineFilters(EMPTY_TIMELINE_FILTERS)}>清除篩選</button>}
                    </div>
                    {chains.length > 0 && (
                        <div style={{ display: 'grid', gap: 6 }} aria-label='委託決策鏈'>
                            {chains.map((chain) => <ChainView key={chain.intentId} chain={chain} />)}
                        </div>
                    )}
                    {looseSignals.length > 0 && (
                        <div style={{ display: 'grid', gap: 5 }} aria-label='未建立委託的訊號'>
                            {looseSignals.map((signal) => <SignalObservation key={signal.signal_id} signal={signal} />)}
                        </div>
                    )}
                    {systemEvents.length > 0 && (
                        <details>
                            <summary>系統事件（{systemEvents.length}）</summary>
                            <div style={{ display: 'grid', gap: 4, marginTop: 4 }}>
                                {systemEvents.map((event) => (
                                    <div key={event.event_id} style={{ borderLeft: '3px solid #4aa3ff', paddingLeft: 7 }}>
                                        {timeText(event.occurred_at)} · {event.event_type}{event.intent_id ? ` · 委託 ${event.intent_id.slice(0, 8)}` : ''}
                                        {Object.keys(event.details).length > 0 && <div style={{ color: '#aeb7c6' }}>細節：{formatPayload(event.details)}</div>}
                                    </div>
                                ))}
                            </div>
                        </details>
                    )}
                    {overview.signals.length + overview.decisions.length + overview.audit_events.length > 0 && timelineCount === 0 && (
                        <div role='status' style={{ color: '#aeb7c6' }}>沒有符合目前篩選條件的 durable 事件。</div>
                    )}
                </>
            )}

            {kind === 'auto_orders' && overview && (
                <>
                    {autoOrders.some((order) => UNCERTAIN_STATUSES.has(order.status)) && (
                        <div role='alert' style={{ background: 'rgba(239,68,68,0.12)', border: '1px solid #ef4444', color: '#ef4444', padding: 6, borderRadius: 4 }}>
                            ⚠ {autoOrders.filter((order) => UNCERTAIN_STATUSES.has(order.status)).length} 筆委託無法確定是否已到券商：
                            系統不會自動重送；請到「對帳佇列」查詢券商結果。同商品已排隊的委託會等這筆釐清後才送出，新的同商品委託會被拒絕。
                        </div>
                    )}
                    {overview.orders.length > automatedOrderObservations(overview.orders).length && (
                        <label style={{ color: '#aeb7c6' }}>
                            <input type='checkbox' checked={showAllOrders} onChange={(event) => setShowAllOrders(event.target.checked)} /> 顯示全部委託（含手動）
                        </label>
                    )}
                    {!observerOnly && overview.health.environment !== 'PAPER' && (
                        <div>
                            <button onClick={runPositionRefresh} title='只讀取已登入的 SIM sidecar 並保存 durable 快照；不會登入、啟用 CA 或送單'>同步 SIM 持倉快照</button>
                            {positionRefreshMessage && <div role='status' style={{ color: '#aeb7c6', marginTop: 4 }}>{positionRefreshMessage}</div>}
                        </div>
                    )}
                    {autoOrders.length > 0 && (
                        <div style={{ display: 'grid', gap: 6 }}>
                            {autoOrders.map((order) => <AutoOrderRow key={order.intent_id} order={order} onSelectCode={selectCode} />)}
                        </div>
                    )}
                    {overview.positions.length > 0 && (
                        <div style={{ display: 'grid', gap: 5 }} aria-label={`durable ${overview.health.environment} 持倉`}>
                            {overview.positions.map((position) => <PositionObservation key={position.snapshot_id} position={position} onSelectCode={selectCode} />)}
                        </div>
                    )}
                </>
            )}

            {kind === 'risk_limits' && overview && <RiskLimitsView overview={overview} />}

            {kind === 'reconciliation' && overview && overview.reconciliation.length > 0 && (
                <div style={{ display: 'grid', gap: 6 }}>
                    {sortOrdersForReview(overview.reconciliation).map((order) => (
                        <ReconciliationRow key={order.intent_id} order={order} observerOnly={observerOnly}
                            onRefresh={runReconciliationRefresh} onSelectCode={selectCode} />
                    ))}
                </div>
            )}
            {kind === 'reconciliation' && reconciliationMessage && <div role='status' style={{ color: '#aeb7c6' }}>{reconciliationMessage}</div>}

            {kind === 'run_performance' && overview && <RunPerformanceView overview={overview} />}

            <small style={{ color: '#6f7888' }}>{activeStrategy?.market_driven && !observerOnly
                ? '操作模式：啟用閘門後，策略訊號會經風控送單；同商品須等前一筆取得券商確認才送下一筆；UNKNOWN 不會自動重送。'
                : '唯讀觀察模式；不會送單、取消、平倉或重送 UNKNOWN。'}</small>
        </section>
    );
}

function PreviewText({ preview }: { preview: StrategyPreview }) {
    return <div role='status' style={{ color: '#aeb7c6', display: 'grid', gap: 2 }}>
        <div>{preview.message}</div>
        {preview.details?.map((line) => <div key={line}>· {line}</div>)}
    </div>;
}

const STEP_TONES: Record<ChainStepState, string> = {
    done: '#73c991', failed: '#ef4444', waiting: '#f0b44d', skipped: '#5b6372',
};
const STEP_MARKS: Record<ChainStepState, string> = {
    done: '✓', failed: '✕', waiting: '…', skipped: '–',
};

function ChainView({ chain }: { chain: DecisionChain }) {
    return <div style={{ borderLeft: `3px solid ${statusTone(chain.status)}`, paddingLeft: 7, display: 'grid', gap: 3 }}>
        <div>{chain.title} · <span style={{ color: statusTone(chain.status) }}>{orderStatusLabel(chain.status)}</span></div>
        <ol style={{ margin: 0, paddingLeft: 0, listStyle: 'none', display: 'flex', flexWrap: 'wrap', gap: '2px 10px' }}>
            {chain.steps.map((step) => (
                <li key={step.label} style={{ color: STEP_TONES[step.state] }} title={step.at ? timeText(step.at) : undefined}>
                    {STEP_MARKS[step.state]} {step.label}：{step.text}{step.at ? `（${timeText(step.at)}）` : ''}
                </li>
            ))}
        </ol>
    </div>;
}

function SignalObservation({ signal }: { signal: AutotradeSignal }) {
    return <div style={{ borderLeft: '3px solid #4aa3ff', paddingLeft: 7 }}>
        訊號 · {signal.strategy_id} · {signal.signal_type} · {signal.symbol} {sideText(signal.side)} · {signal.status} · {timeText(signal.observed_at)}
        {signal.risk_reasons.length ? <div style={{ color: '#f0b44d' }}>風控：{signal.risk_reasons.map(riskReasonLabel).join('、')}</div> : null}
    </div>;
}

function KlineButton({ symbol, onSelectCode }: { symbol: string | undefined; onSelectCode?: (code: string) => void }) {
    if (!symbol || !onSelectCode) return null;
    return <button onClick={() => onSelectCode(symbol)} title='把這檔商品帶到 K 線與報價面板；成交會標在 K 線上'>在 K 線顯示</button>;
}

function AutoOrderRow({ order, onSelectCode }: { order: AutotradeOrderObservation; onSelectCode?: (code: string) => void }) {
    const unit = order.quantity_unit ?? '';
    return <div style={{ borderLeft: `3px solid ${statusTone(order.status)}`, paddingLeft: 7, display: 'grid', gap: 2 }}>
        <div>
            <b>{order.symbol}</b> {sideText(order.side)} {order.quantity}{unit}{order.limit_price ? ` @ ${order.limit_price}` : ''}
            {' · '}<span style={{ color: statusTone(order.status) }}>{orderStatusLabel(order.status)}</span>
            {order.operation !== 'PLACE' ? ` · ${operationLabel(order.operation)}` : ''}
        </div>
        <div style={{ color: '#aeb7c6' }}>
            {timeText(order.created_at ?? order.updated_at)} · {order.strategy_id ? `策略 ${order.strategy_id}` : SOURCE_LABELS[order.source] ?? order.source}
            {order.run_id ? ` · 執行 ${order.run_id.slice(0, 8)}` : ''}
            {order.account_id ? ` · 帳號 ${order.account_id}` : ''}
        </div>
        {(order.filled_quantity ?? 0) > 0 && <div style={{ color: '#73c991' }}>
            已成交 {filledText(order)}{order.avg_fill_price ? ` · 均價 ${order.avg_fill_price}` : ''}
            {(order.fill_count ?? 0) > 1 ? `（${order.fill_count} 筆）` : ''}
        </div>}
        {order.broker_order_id && <div style={{ color: '#aeb7c6' }}>券商單號 {order.broker_order_id}</div>}
        {order.risk_reasons.length ? <div style={{ color: '#f0b44d' }}>風控：{order.risk_reasons.map(riskReasonLabel).join('、')}</div> : null}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
            <KlineButton symbol={order.symbol} onSelectCode={onSelectCode} />
        </div>
        <DurableOrderDetail intentId={order.intent_id} />
    </div>;
}

function ReconciliationRow({
    order, observerOnly, onRefresh, onSelectCode,
}: {
    order: AutotradeReconciliationObservation;
    observerOnly: boolean;
    onRefresh: (intentId: string) => void;
    onSelectCode?: (code: string) => void;
}) {
    const [note, setNote] = useState('');
    const [noteMessage, setNoteMessage] = useState<string | null>(null);
    const saveNote = () => {
        const conclusion = note.trim();
        if (conclusion.length < 3) return;
        void recordReconciliationNote(order.intent_id, conclusion)
            .then(() => {
                setNote('');
                setNoteMessage('已記錄對帳結論；委託狀態不變。');
                return autotradeObserverStore.refresh();
            })
            .catch(() => setNoteMessage('結論未記錄；請確認 Gateway 連線，或此筆已不在對帳狀態。'));
    };
    return <div style={{ borderLeft: `3px solid ${statusTone(order.status)}`, paddingLeft: 7, display: 'grid', gap: 3 }}>
        <div>
            {SOURCE_LABELS[order.source] ?? order.source} · {operationLabel(order.operation)} ·{' '}
            <span style={{ color: statusTone(order.status) }}>{orderStatusLabel(order.status)}（{order.status}）</span>
            {order.broker_order_id ? ` · 券商單號 ${order.broker_order_id}` : ''}{order.target_order_id ? ` · 目標 ${order.target_order_id}` : ''}
        </div>
        {orderObservationText(order) && <div style={{ color: '#aeb7c6' }}>{orderObservationText(order)}{order.account_id ? ` · 帳號 ${order.account_id}` : ''}</div>}
        {order.reconciliation_case && <div style={{ color: '#ef4444' }}>對帳原因：{order.reconciliation_case.reason} · 開案 {timeText(order.reconciliation_case.opened_at)}</div>}
        {order.operator_note && <div style={{ color: '#73c991' }}>人工結論：{order.operator_note.conclusion}{order.operator_note.noted_at ? `（${timeText(order.operator_note.noted_at)}）` : ''}</div>}
        {order.operation_payload && <div style={{ color: '#aeb7c6' }}>請求：{formatPayload(order.operation_payload)}</div>}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
            {!observerOnly && <button onClick={() => onRefresh(order.intent_id)}
                title='只查詢已驗證的 SIM broker trade；不會重送或建立委託'>重新查詢對帳</button>}
            <KlineButton symbol={order.symbol} onSelectCode={onSelectCode} />
        </div>
        {!observerOnly && (
            <form aria-label='記錄對帳結論' onSubmit={(event) => { event.preventDefault(); saveNote(); }}
                style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                <input aria-label='對帳結論' value={note} maxLength={500} onChange={(event) => setNote(event.target.value)}
                    placeholder='例：已在券商查到成交 1 張' style={{ minWidth: 200 }} />
                <button type='submit' disabled={note.trim().length < 3} title='只記錄結論，不改變委託狀態，也不聯絡券商'>記錄結論</button>
            </form>
        )}
        {noteMessage && <div role='status' style={{ color: '#aeb7c6' }}>{noteMessage}</div>}
        <DurableOrderDetail intentId={order.intent_id} />
    </div>;
}

function RiskLimitsView({ overview }: { overview: AutotradeOverview }) {
    const { limits, usage } = overview.risk;
    const rows = riskUsageRows(overview);
    const quoteAge = decimalText(limits.max_quote_age_seconds);
    const slowAge = decimalText(limits.slow_quote_max_age_seconds);
    const cooldown = decimalText(limits.cooldown_seconds);
    const verdict = riskVerdictText(overview.risk.last_verdict);
    return <div style={{ display: 'grid', gap: 6 }}>
        {rows.length > 0 && (
            <table style={{ borderCollapse: 'collapse', width: '100%' }} aria-label='風控使用量'>
                <thead><tr style={{ color: '#8892a6', textAlign: 'left' }}>
                    <th>項目</th><th>已使用</th><th>上限</th><th>剩餘</th>
                </tr></thead>
                <tbody>
                    {rows.map((row) => <tr key={row.label}>
                        <td>{row.label}</td><td>{row.used}</td><td>{row.limit}</td><td>{row.remaining}</td>
                    </tr>)}
                </tbody>
            </table>
        )}
        <div style={{ display: 'grid', gap: 3, color: '#aeb7c6' }}>
            {'allowed_symbols' in limits && <div>允許交易商品：{riskValueText(limits.allowed_symbols)}</div>}
            {quoteAge !== null && <div>
                交易時間：以最新成交價時效判斷，超過 {quoteAge} 秒{slowAge !== null ? `（冷門商品 ${slowAge} 秒）` : ''}沒有新成交即拒絕新委託；
                非交易時間沒有新成交，策略不會下單。
            </div>}
            {cooldown !== null && <div>冷卻時間：同一商品兩筆策略委託至少間隔 {cooldown} 秒（手動委託不受限）。</div>}
            <div>同商品逐筆送出：前一筆取得券商確認收到後，才送出同商品的下一筆；不同商品可同時送出。</div>
            <div>已確認成交 {usage.confirmed_filled_quantity}（{usage.confirmed_fill_count} 筆） · 委託紀錄 {usage.intent_count} 筆 · 待對帳 {usage.reconciliation_count} 筆</div>
            {!usage.authoritative_remaining_available && <div>每日損失與總曝險剩餘額度：尚無最新帳務快照，不能據此推測。</div>}
            <div>限額來源：{overview.risk.limits_source}（由 Gateway 啟動設定決定，介面無法修改）</div>
        </div>
        {verdict && (
            <div role='status' style={{ color: overview.risk.last_verdict?.allowed ? '#73c991' : '#f0b44d' }}>{verdict}</div>
        )}
        {Object.keys(limits).length > 0 && (
            <details>
                <summary>全部限額</summary>
                <div style={{ display: 'grid', gap: 2, color: '#aeb7c6', marginTop: 4 }}>
                    {Object.entries(limits).map(([key, value]) => <div key={key}>{riskLabel(key)}：{riskValueText(value)}</div>)}
                </div>
            </details>
        )}
    </div>;
}

function percentText(value: number | null): string {
    return value === null ? '—' : `${Math.round(value * 1000) / 10}%`;
}

function RunPerformanceView({ overview }: { overview: AutotradeOverview }) {
    const { performance } = overview;
    return <div style={{ display: 'grid', gap: 6 }}>
        <div style={{ color: '#aeb7c6' }}>
            已確認成交委託：{performance.filled_order_count} · 帳戶：已實現 {performance.realized_pnl ?? '尚無資料'} · 未實現 {performance.unrealized_pnl ?? '尚無資料'}
            {' · '}{performance.source === 'PAPER_ESTIMATE' ? '本機紙上估算（不含手續費與稅）' : performance.source === 'BROKER_ACCOUNT' ? '券商帳戶快照' : '尚無資料'}
            {performance.stale ? ' · 資料過期或尚未取得' : ''}
        </div>
        {performance.runs.length > 0 ? (
            <div style={{ overflowX: 'auto' }}>
                <table style={{ borderCollapse: 'collapse', width: '100%', whiteSpace: 'nowrap' }} aria-label='每次執行績效'>
                    <thead><tr style={{ color: '#8892a6', textAlign: 'left' }}>
                        <th>策略／執行</th><th>狀態</th><th>成交</th><th>來回</th><th>勝率</th><th>已實現</th>
                        <th>最大回撤</th><th>拒絕率</th><th>滑價</th><th>確認延遲</th><th>成交延遲</th>
                    </tr></thead>
                    <tbody>
                        {performance.runs.map((run: AutotradeRunPerformance) => <tr key={run.run_id}>
                            <td>{run.strategy_id ?? '未命名'}{run.version ? ` ${run.version}` : ''} · {run.run_id.slice(0, 8)}</td>
                            <td>{STRATEGY_STATE_LABELS[run.state ?? ''] ?? run.state ?? '—'}</td>
                            <td>{run.fill_count}</td>
                            <td>{run.closed_trade_count}</td>
                            <td>{percentText(run.win_rate)}</td>
                            <td>{run.realized_pnl ?? '—'}</td>
                            <td>{run.max_drawdown ?? '—'}</td>
                            <td>{percentText(run.reject_rate)}（{run.rejected_count}/{run.intent_count}）</td>
                            <td>{run.avg_slippage_bps === null ? '—' : `${run.avg_slippage_bps} bps`}</td>
                            <td>{run.avg_ack_latency_ms === null ? '—' : `${Math.round(run.avg_ack_latency_ms)} ms`}</td>
                            <td>{run.avg_fill_latency_ms === null ? '—' : `${Math.round(run.avg_fill_latency_ms)} ms`}</td>
                        </tr>)}
                    </tbody>
                </table>
            </div>
        ) : <div style={{ color: '#8892a6' }}>尚無策略執行紀錄。</div>}
        <small style={{ color: '#6f7888' }}>
            績效為估算：以先進先出配對買賣、成交價計算，不含手續費與稅；滑價以限價為基準（正值代表成交比限價差）。
        </small>
    </div>;
}

function PositionObservation({ position, onSelectCode }: { position: AutotradePositionObservation; onSelectCode?: (code: string) => void }) {
    return <div style={{ borderLeft: '3px solid #4aa3ff', paddingLeft: 7 }}>
        持倉 · {position.account_type === 'F' ? '期貨' : '證券'} {position.account_label} · {position.symbol} · {position.side === 'LONG' ? '多' : '空'} {position.quantity}
        <div style={{ color: '#aeb7c6' }}>均價 {position.average_price} · 市價 {position.market_price} · 未實現 {position.unrealized_pnl} · {timeText(position.observed_at)}</div>
        <KlineButton symbol={position.symbol} onSelectCode={onSelectCode} />
    </div>;
}

type LoadOrderObservation = (intentId: string) => Promise<AutotradeOrderObservation | null>;

/** Displays an already-read identity in full; clipboard use stays entirely in the browser. */
export function CopyableIdentity({ label, value }: { label: string; value: string }) {
    const [copyState, setCopyState] = useState<'idle' | 'copied' | 'unavailable'>('idle');
    const copy = () => {
        const clipboard = globalThis.navigator?.clipboard;
        if (!clipboard) {
            setCopyState('unavailable');
            return;
        }
        void clipboard.writeText(value)
            .then(() => setCopyState('copied'))
            .catch(() => setCopyState('unavailable'));
    };
    return <div style={{ overflowWrap: 'anywhere' }}>
        {label}：<code>{value}</code>{' '}
        <button onClick={copy} title={`複製完整 ${label}`}>複製</button>
        {copyState === 'copied' && <span role='status'> 已複製</span>}
        {copyState === 'unavailable' && <span role='status'> 無法存取 clipboard</span>}
    </div>;
}

/**
 * An expandable, durable detail read.  This intentionally fetches no broker
 * data and contains no mutation action: an absent or failed observation is
 * rendered as absence rather than being reconstructed from browser state.
 */
export function DurableOrderDetail({
    intentId,
    loadObservation = fetchAutotradeOrder,
}: {
    intentId: string;
    loadObservation?: LoadOrderObservation;
}) {
    const [expanded, setExpanded] = useState(false);
    const [loading, setLoading] = useState(false);
    const [detail, setDetail] = useState<AutotradeOrderObservation | null | undefined>(undefined);

    useEffect(() => {
        if (!expanded) return;
        let current = true;
        setLoading(true);
        setDetail(undefined);
        void loadObservation(intentId)
            .then((value) => { if (current) setDetail(value); })
            .catch(() => { if (current) setDetail(null); })
            .finally(() => { if (current) setLoading(false); });
        return () => { current = false; };
    }, [expanded, intentId, loadObservation]);

    return (
        <div style={{ marginTop: 5 }}>
            <button
                onClick={() => setExpanded((value) => !value)}
                aria-expanded={expanded}
                title='向 Gateway 重讀此 durable intent；不會查詢或變更 broker'
            >
                {expanded ? '收合可稽核詳情' : '查看可稽核詳情'}
            </button>
            {expanded && (
                <div role='status' style={{ color: '#aeb7c6', marginTop: 4 }}>
                    {loading && '正在讀取 durable 記錄…'}
                    {!loading && detail === undefined && '詳情尚未載入'}
                    {!loading && detail === null && 'Gateway 未提供此 durable 記錄；未以本機資料補齊。'}
                    {!loading && detail && (
                        <>
                            intent {detail.intent_id.slice(0, 8)} · {detail.status} · 更新 {detail.updated_at || '未知'}{detail.broker_order_id ? ` · broker ${detail.broker_order_id}` : ''}
                            <div style={{ display: 'grid', gap: 2, marginTop: 3 }}>
                                <CopyableIdentity label='Intent ID' value={detail.intent_id} />
                                <CopyableIdentity label='Correlation ID' value={detail.correlation_id} />
                                {detail.broker_order_id && <CopyableIdentity label='Broker order ID' value={detail.broker_order_id} />}
                                {detail.target_order_id && <CopyableIdentity label='Target broker order ID' value={detail.target_order_id} />}
                                {detail.strategy_id && <CopyableIdentity label='Strategy ID' value={detail.strategy_id} />}
                                {detail.run_id && <CopyableIdentity label='Run ID' value={detail.run_id} />}
                            </div>
                            {detail.risk_reasons.length ? <div style={{ color: '#f0b44d' }}>風控：{detail.risk_reasons.map(riskReasonLabel).join('、')}</div> : null}
                        </>
                    )}
                </div>
            )}
        </div>
    );
}

function operationLabel(operation: string | undefined): string {
    return ({ PLACE: '下單', CANCEL: '取消', UPDATE_PRICE: '改價', UPDATE_QUANTITY: '改量' } as Record<string, string>)[operation ?? 'PLACE'] ?? operation ?? '下單';
}

export function orderObservationText(order: Partial<AutotradeOrderObservation>): string | null {
    if (order.operation !== undefined && order.operation !== 'PLACE') return null;
    const quantity = order.quantity;
    if (!order.symbol || !order.side || !Number.isInteger(quantity) || quantity === undefined || quantity <= 0) return null;
    const price = order.limit_price ? ` · ${order.order_price_type ?? 'LMT'} ${order.limit_price}` : '';
    const attribution = order.strategy_id ? ` · 策略 ${order.strategy_id}` : '';
    const run = order.run_id ? ` · run ${order.run_id}` : '';
    return `商品：${order.symbol} · ${sideText(order.side)} ${quantity}${order.quantity_unit ?? ''}${price}${attribution}${run}`;
}

function formatPayload(value: Record<string, unknown>): string {
    return Object.entries(value).map(([key, item]) => `${key}=${typeof item === 'string' ? item : JSON.stringify(item)}`).join(' · ');
}

function riskLabel(key: string): string {
    return ({
        allowed_accounts: '允許帳戶', allowed_symbols: '允許商品',
        max_order_quantity: '單筆最大數量（股）', max_order_notional: '單筆最大名目金額',
        max_position_per_symbol: '單一商品最大部位（股）', max_gross_exposure: '最大總曝險',
        max_daily_loss: '每日最大損失', max_quote_age_seconds: '最大行情秒數',
        max_price_deviation: '最大價格偏離',
        max_orders_per_minute: '每分鐘最多委託數', cooldown_seconds: '委託冷卻秒數',
        slow_quote_symbols: '冷門商品（較長成交價時效）', slow_quote_max_age_seconds: '冷門商品成交價時效（秒）',
        board_lot_only_symbols: '只能整股的商品（CB）', allowed_futures_roots: '允許期貨商品',
        futures_multipliers: '期貨契約乘數', max_futures_order_contracts: '期貨單筆最多口數',
        max_futures_position: '期貨最大部位（口）', max_futures_notional: '期貨最大契約價值',
    } as Record<string, string>)[key] ?? key;
}

/** Human-readable limit value; lists and maps never render as raw objects. */
export function riskValueText(value: unknown): string {
    if (Array.isArray(value)) return value.length ? value.join('、') : '無';
    if (value && typeof value === 'object') {
        const entries = Object.entries(value as Record<string, unknown>);
        return entries.length ? entries.map(([key, item]) => `${key} ×${String(item)}`).join('、') : '無';
    }
    return String(value);
}

export function riskVerdictText(verdict: AutotradeRiskVerdict | null | undefined): string | null {
    if (!verdict) return null;
    const reasons = verdict.reasons.map(riskReasonLabel);
    const outcome = verdict.allowed ? '允許' : '拒絕';
    return `最近風控：${outcome}${reasons.length ? `（${reasons.join('、')}）` : ''} · 追蹤 ${verdict.correlation_id.slice(0, 8)}`;
}

export function automatedOrderObservations(
    orders: AutotradeOrderObservation[],
): AutotradeOrderObservation[] {
    return orders.filter((order) => order.source === 'STRATEGY' || order.source === 'SYSTEM_TRIGGER');
}

function Badge({ label, value, tone }: { label: string; value: string; tone: string }) {
    return <span style={{ border: `1px solid ${tone}`, borderRadius: 4, color: tone, padding: '4px 7px' }}>{label}：{value}</span>;
}
