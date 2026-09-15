import { useEffect, useState } from 'react';
import {
    applyAutotradeKillSwitch,
    fetchAutotradeOrder,
    previewReferenceStrategy,
    type AutotradeOverview,
    type AutotradeOrderObservation,
    type AutotradeReconciliationObservation,
    type AutotradeRiskVerdict,
    type AutotradeSignal,
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
        empty: '策略 runtime 預設停用。啟動功能需要另外授權，且不會由此面板啟動。',
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
        && matchesTimelineFilter(filters.status, decision.status)
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

function countFor(kind: AutotradeObserverPanel, overview: AutotradeOverview): number {
    switch (kind) {
        case 'strategy_control': return overview.strategies.length;
        case 'signal_timeline': return overview.signals.length + overview.decisions.length + overview.audit_events.length;
        case 'auto_orders': return automatedOrderObservations(overview.orders).length;
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
    if (!overview) return PANEL_COPY[kind].empty;

    switch (kind) {
        case 'risk_limits':
            return Object.keys(overview.risk.limits).length > 0
                ? '已載入 simulation 風控限額；ARMED 前必須有權威市場與帳務快照，缺少時會拒絕。所有新 mutation 目前仍由 DISARMED Gateway 拒絕。'
                : PANEL_COPY[kind].empty;
        case 'signal_timeline':
            return overview.signals.length > 0 || overview.decisions.length > 0 || overview.audit_events.length > 0
                ? '以下為 durable signal、durable intent、Risk Engine verdict 與 immutable 執行事件；不以示範資料取代真實事件。'
                : PANEL_COPY[kind].empty;
        case 'auto_orders':
            return automatedOrderObservations(overview.orders).length > 0
                ? '以下為可對帳的自動委託與策略歸屬持倉觀察資料。'
                : PANEL_COPY[kind].empty;
        case 'reconciliation':
            return overview.reconciliation.length > 0
                ? '以下委託需要人工對帳；系統不會自動重送。'
                : PANEL_COPY[kind].empty;
        case 'run_performance':
            return overview.performance.run_count > 0
                ? '以下只呈現 durable 策略執行績效，不以估算資料補齊。'
                : PANEL_COPY[kind].empty;
        case 'strategy_control':
            return PANEL_COPY[kind].empty;
    }
}

export function AutotradeObserverPanels({ kind }: { kind: AutotradeObserverPanel }) {
    const [observer, setObserver] = useState<AutotradeObserverState>(autotradeObserverStore.snapshot());
    const [strategyPreview, setStrategyPreview] = useState<string | null>(null);
    const [killSwitchMessage, setKillSwitchMessage] = useState<string | null>(null);
    const [showAllOrders, setShowAllOrders] = useState(false);
    const [timelineFilters, setTimelineFilters] = useState<TimelineFilters>(EMPTY_TIMELINE_FILTERS);
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    const { overview, error } = observer;
    const copy = PANEL_COPY[kind];
    const timeline = overview ? filterTimeline(overview, timelineFilters) : null;
    const count = overview
        ? kind === 'auto_orders' && showAllOrders ? overview.orders.length
            : kind === 'signal_timeline' && timeline
                ? timeline.signals.length + timeline.decisions.length + timeline.auditEvents.length
                : countFor(kind, overview)
        : 0;
    const displayedOrders = kind === 'reconciliation'
        ? overview?.reconciliation
        : kind === 'auto_orders' && overview
            ? (showAllOrders ? overview.orders : automatedOrderObservations(overview.orders))
            : undefined;
    const runPreview = (action: 'validate' | 'dry-run') => {
        void previewReferenceStrategy(action)
            .then((result) => setStrategyPreview(result.message))
            .catch(() => setStrategyPreview('策略預覽目前無法連線到 Gateway'));
    };
    const runKillSwitch = () => {
        if (overview?.health.trading_gate === 'KILLED') {
            setKillSwitchMessage('Kill Switch 已啟用；此介面沒有解除鎖定功能。');
            return;
        }
        const reason = globalThis.prompt('請輸入啟用 Kill Switch 的原因；此操作只會阻擋新 mutation。');
        if (!reason?.trim()) return;
        const idempotencyKey = globalThis.crypto.randomUUID();
        void applyAutotradeKillSwitch(reason.trim(), idempotencyKey)
            .then((receipt) => setKillSwitchMessage(
                receipt.applied
                    ? `Kill Switch 已啟用：${receipt.reason}`
                    : `Kill Switch 已存在：${receipt.reason}`,
            ))
            .catch(() => setKillSwitchMessage('Kill Switch 請求失敗；請確認 Gateway 連線與原因。'));
    };
    return (
        <section style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>{copy.title}</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Badge label='環境' value={overview?.health.environment ?? 'UNKNOWN'} tone='#4aa3ff' />
                <Badge label='閘門' value={overview?.health.trading_gate ?? 'UNKNOWN'} tone='#f0b44d' />
                <Badge label='項目' value={String(count)} tone='#8892a6' />
            </div>
            <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>{panelSummary(kind, overview, overview ? null : error)}</div>
            {observer.stale && (
                <div role='status' style={{ color: '#f0b44d' }}>
                    Gateway 更新暫時失敗；保留最後一次成功的唯讀資料，未重送任何委託。
                </div>
            )}
            {kind === 'auto_orders' && overview && overview.orders.length > automatedOrderObservations(overview.orders).length && (
                <label style={{ color: '#aeb7c6' }}>
                    <input
                        type='checkbox'
                        checked={showAllOrders}
                        onChange={(event) => setShowAllOrders(event.target.checked)}
                    /> 顯示全部委託（含手動）
                </label>
            )}
            {kind === 'signal_timeline' && overview && timeline && (
                <div role='group' aria-label='時間線篩選' style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    <label>策略 <input aria-label='篩選策略' value={timelineFilters.strategy} onChange={(event) => setTimelineFilters((current) => ({ ...current, strategy: event.target.value }))} /></label>
                    <label>商品 <input aria-label='篩選商品' value={timelineFilters.symbol} onChange={(event) => setTimelineFilters((current) => ({ ...current, symbol: event.target.value }))} /></label>
                    <label>狀態 <input aria-label='篩選狀態' value={timelineFilters.status} onChange={(event) => setTimelineFilters((current) => ({ ...current, status: event.target.value }))} /></label>
                    <label>起始時間 <input aria-label='篩選起始時間' type='datetime-local' value={timelineFilters.from} onChange={(event) => setTimelineFilters((current) => ({ ...current, from: event.target.value }))} /></label>
                    <label>結束時間 <input aria-label='篩選結束時間' type='datetime-local' value={timelineFilters.to} onChange={(event) => setTimelineFilters((current) => ({ ...current, to: event.target.value }))} /></label>
                    {(timelineFilters.strategy || timelineFilters.symbol || timelineFilters.status || timelineFilters.from || timelineFilters.to) && <button onClick={() => setTimelineFilters(EMPTY_TIMELINE_FILTERS)}>清除篩選</button>}
                </div>
            )}
            {(kind === 'auto_orders' || kind === 'reconciliation') && displayedOrders && displayedOrders.length > 0 && (
                <div style={{ display: 'grid', gap: 5 }}>
                    {displayedOrders.map((item) => {
                        const order = item as Partial<AutotradeOrderObservation>;
                        const reconciliation = kind === 'reconciliation'
                            ? item as AutotradeReconciliationObservation : null;
                        return <div key={order.intent_id} style={{ borderLeft: `3px solid ${order.status === 'UNKNOWN' ? '#ef4444' : '#8892a6'}`, paddingLeft: 7 }}>
                            {order.source} · {operationLabel(order.operation)} · {order.status} · {order.correlation_id?.slice(0, 8)}{order.broker_order_id ? ` · ${order.broker_order_id}` : ''}{order.target_order_id ? ` · 目標 ${order.target_order_id}` : ''}
                            {orderObservationText(order) && <div style={{ color: '#aeb7c6' }}>{orderObservationText(order)}</div>}
                            {typeof order.filled_quantity === 'number' && order.filled_quantity > 0 && <div style={{ color: '#73c991' }}>已確認成交：{order.filled_quantity}{typeof order.fill_count === 'number' && order.fill_count > 1 ? `（${order.fill_count} 筆）` : ''}</div>}
                            {order.operation_payload && <div style={{ color: '#aeb7c6' }}>請求：{formatPayload(order.operation_payload)}</div>}
                            {order.instrument && <div style={{ color: '#aeb7c6' }}>商品：{formatPayload(order.instrument)}</div>}
                            {order.risk_reasons?.length ? <div style={{ color: '#f0b44d' }}>風控：{order.risk_reasons.join('、')}</div> : null}
                            {reconciliation?.reconciliation_case && <div style={{ color: '#ef4444' }}>對帳：{reconciliation.reconciliation_case.reason} · 開案 {reconciliation.reconciliation_case.opened_at}</div>}
                            {order.intent_id && <DurableOrderDetail intentId={order.intent_id} />}
                        </div>;
                    })}
                </div>
            )}
            {kind === 'signal_timeline' && overview && timeline && (timeline.signals.length > 0 || timeline.decisions.length > 0 || timeline.auditEvents.length > 0) ? (
                <div style={{ display: 'grid', gap: 5 }}>
                    {timeline.signals.map((signal) => <SignalObservation key={signal.signal_id} signal={signal} />)}
                    {timeline.decisions.map((decision) => (
                        <div key={decision.intent_id} style={{ borderLeft: '3px solid #f0b44d', paddingLeft: 7 }}>
                            {decision.source} · {operationLabel(decision.operation)} · {decision.status} · {decision.correlation_id.slice(0, 8)}
                            {decision.risk_decision_missing ? <div style={{ color: '#ef4444' }}>風控判定 journal 缺失；不得據此重送</div> : null}
                            {typeof decision.allowed === 'boolean' ? <div style={{ color: decision.allowed ? '#73c991' : '#f0b44d' }}>風控判定：{decision.allowed ? '允許' : '拒絕'}{decision.decided_at ? ` · ${decision.decided_at}` : ''}</div> : null}
                            {decision.risk_reasons.length ? <div style={{ color: '#f0b44d' }}>風控：{decision.risk_reasons.join('、')}</div> : null}
                        </div>
                    ))}
                    {timeline.auditEvents.map((event) => (
                        <div key={event.event_id} style={{ borderLeft: '3px solid #4aa3ff', paddingLeft: 7 }}>
                            執行事件 · {event.event_type} · {event.intent_id ? `intent ${event.intent_id.slice(0, 8)}` : '全域'} · {event.occurred_at}
                            {Object.keys(event.details).length > 0 && <div style={{ color: '#aeb7c6' }}>細節：{formatPayload(event.details)}</div>}
                        </div>
                    ))}
                </div>
            ) : null}
            {kind === 'signal_timeline' && overview && timeline
                && overview.signals.length + overview.decisions.length + overview.audit_events.length > 0
                && timeline.signals.length + timeline.decisions.length + timeline.auditEvents.length === 0 && (
                    <div role='status' style={{ color: '#aeb7c6' }}>沒有符合目前篩選條件的 durable 事件。</div>
                )}
            {kind === 'strategy_control' && (
                <>
                    {overview?.strategies.map((item, index) => {
                        const strategy = item as { strategy_id?: string; version?: string; state?: string; enabled?: boolean };
                        return <div key={`${strategy.strategy_id ?? 'strategy'}-${index}`} style={{ color: '#aeb7c6' }}>
                            {strategy.strategy_id ?? '未命名策略'} {strategy.version ?? ''} · {strategy.state ?? 'UNKNOWN'} · {strategy.enabled ? '已啟用' : '未啟用'}
                        </div>;
                    })}
                    {strategyPreview && <div style={{ color: '#aeb7c6' }}>{strategyPreview}</div>}
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <button onClick={() => runPreview('validate')} title='唯讀檢查；不啟動策略'>驗證策略</button>
                        <button onClick={() => runPreview('dry-run')} title='不建立 intent、outbox 或 broker 請求'>Dry Run</button>
                        <button disabled title='需要獨立策略 runtime 授權'>開始</button>
                        <button disabled title='Gateway 目前固定 DISARMED'>暫停</button>
                        <button onClick={runKillSwitch} title='需要輸入原因；只能將 Gateway 加嚴為 KILLED'>啟用 Kill Switch</button>
                    </div>
                    {killSwitchMessage && <div role='status' style={{ color: '#ef4444' }}>{killSwitchMessage}</div>}
                </>
            )}
            {kind === 'risk_limits' && overview && Object.keys(overview.risk.limits).length > 0 && (
                <div style={{ display: 'grid', gap: 4, color: '#aeb7c6' }}>
                    <div>來源：{overview.risk.limits_source}</div>
                    {Object.entries(overview.risk.limits).map(([key, value]) => (
                        <div key={key}>{riskLabel(key)}：{Array.isArray(value) ? value.join('、') : String(value)}</div>
                    ))}
                </div>
            )}
            {kind === 'risk_limits' && overview && (
                <div style={{ display: 'grid', gap: 4, color: '#aeb7c6' }}>
                    <div>Durable usage：{overview.risk.usage.intent_count} intents · 已確認成交 {overview.risk.usage.confirmed_filled_quantity}（{overview.risk.usage.confirmed_fill_count} 筆） · 待對帳 {overview.risk.usage.reconciliation_count}</div>
                    {!overview.risk.usage.authoritative_remaining_available && <div>剩餘部位／日損／總曝險：尚無權威帳務快照，不能據此推測。</div>}
                </div>
            )}
            {kind === 'risk_limits' && riskVerdictText(overview?.risk.last_verdict) && (
                <div role='status' style={{ color: overview?.risk.last_verdict?.allowed ? '#73c991' : '#f0b44d' }}>
                    {riskVerdictText(overview?.risk.last_verdict)}
                </div>
            )}
            {kind === 'run_performance' && overview && (
                <div style={{ display: 'grid', gap: 5, color: '#8892a6' }}>
                    <div>已確認成交委託：{overview.performance.filled_order_count}</div>
                    <div>已實現 {overview.performance.realized_pnl}／未實現 {overview.performance.unrealized_pnl}</div>
                    {overview.runs.map((run) => (
                        <div key={run.run_id} style={{ borderLeft: '3px solid #8892a6', paddingLeft: 7 }}>
                            {run.strategy_id ?? '未命名策略'}{run.version ? ` ${run.version}` : ''} · {run.run_id}
                            <div>狀態：{run.state ?? 'UNKNOWN'} · 最近結果：{run.last_status ?? '無 intent'} · {run.intent_count} intents</div>
                            <div>Heartbeat：{run.heartbeat_at ?? '尚無持久化 heartbeat'}{run.ended_at ? ` · 結束：${run.ended_at}` : ''}</div>
                        </div>
                    ))}
                </div>
            )}
            <small style={{ color: '#6f7888' }}>唯讀觀察模式；不會送單、取消、平倉或重送 UNKNOWN。</small>
        </section>
    );
}

function SignalObservation({ signal }: { signal: AutotradeSignal }) {
    const direction = signal.side === 'BUY' ? '買' : signal.side === 'SELL' ? '賣' : signal.side;
    return <div style={{ borderLeft: '3px solid #4aa3ff', paddingLeft: 7 }}>
        訊號 · {signal.strategy_id} · {signal.signal_type} · {signal.symbol} {direction} · {signal.status} · {signal.correlation_id.slice(0, 8)}
        <div style={{ color: '#aeb7c6' }}>event {signal.event_id} · run {signal.run_id} · {signal.observed_at || '觀測時間未知'}</div>
        {signal.risk_reasons.length ? <div style={{ color: '#f0b44d' }}>風控：{signal.risk_reasons.map(riskReasonLabel).join('、')}</div> : null}
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
    const direction = order.side === 'BUY' ? '買' : order.side === 'SELL' ? '賣' : order.side;
    const price = order.limit_price ? ` · ${order.order_price_type ?? 'LMT'} ${order.limit_price}` : '';
    const attribution = order.strategy_id ? ` · 策略 ${order.strategy_id}` : '';
    const run = order.run_id ? ` · run ${order.run_id}` : '';
    return `商品：${order.symbol} · ${direction} ${quantity}${price}${attribution}${run}`;
}

function formatPayload(value: Record<string, unknown>): string {
    return Object.entries(value).map(([key, item]) => `${key}=${typeof item === 'string' ? item : JSON.stringify(item)}`).join(' · ');
}

function riskLabel(key: string): string {
    return ({
        allowed_accounts: '允許帳戶', allowed_symbols: '允許商品',
        max_order_quantity: '單筆最大數量', max_order_notional: '單筆最大名目金額',
        max_position_per_symbol: '單一商品最大部位', max_gross_exposure: '最大總曝險',
        max_daily_loss: '每日最大損失', max_quote_age_seconds: '最大行情秒數',
        max_price_deviation: '最大價格偏離',
    } as Record<string, string>)[key] ?? key;
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

function riskReasonLabel(reason: string): string {
    return ({
        trading_gate_not_armed: '交易閘門尚未啟用',
        risk_context_unavailable: '缺少權威市場與帳務風控快照',
        stale_quote: '行情已過期或不可用',
        price_deviation_exceeded: '委託價格偏離最新行情過大',
        position_limit_exceeded: '預估商品部位超過限額',
        gross_exposure_limit_exceeded: '預估總曝險超過限額',
        daily_loss_limit_exceeded: '每日損失已達限額',
    } as Record<string, string>)[reason] ?? reason;
}

function Badge({ label, value, tone }: { label: string; value: string; tone: string }) {
    return <span style={{ border: `1px solid ${tone}`, borderRadius: 4, color: tone, padding: '4px 7px' }}>{label}：{value}</span>;
}
