import { useEffect, useState } from 'react';
import {
    previewReferenceStrategy,
    type AutotradeOverview,
    type AutotradeOrderObservation,
    type AutotradeRiskVerdict,
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

function countFor(kind: AutotradeObserverPanel, overview: AutotradeOverview): number {
    switch (kind) {
        case 'strategy_control': return overview.strategies.length;
        case 'signal_timeline': return overview.decisions.length;
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
            return overview.decisions.length > 0
                ? '以下為 durable intent 與 Risk Engine verdict；不以示範資料取代真實事件。'
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
    const [showAllOrders, setShowAllOrders] = useState(false);
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    const { overview, error } = observer;
    const copy = PANEL_COPY[kind];
    const count = overview
        ? kind === 'auto_orders' && showAllOrders ? overview.orders.length : countFor(kind, overview)
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
            {(kind === 'auto_orders' || kind === 'reconciliation') && displayedOrders && displayedOrders.length > 0 && (
                <div style={{ display: 'grid', gap: 5 }}>
                    {displayedOrders.map((item) => {
                        const order = item as { intent_id?: string; correlation_id?: string; source?: string; status?: string; broker_order_id?: string | null; operation?: string; target_order_id?: string | null; operation_payload?: Record<string, unknown> | null; instrument?: Record<string, unknown> | null; risk_reasons?: string[] };
                        return <div key={order.intent_id} style={{ borderLeft: `3px solid ${order.status === 'UNKNOWN' ? '#ef4444' : '#8892a6'}`, paddingLeft: 7 }}>
                            {order.source} · {operationLabel(order.operation)} · {order.status} · {order.correlation_id?.slice(0, 8)}{order.broker_order_id ? ` · ${order.broker_order_id}` : ''}{order.target_order_id ? ` · 目標 ${order.target_order_id}` : ''}
                            {order.operation_payload && <div style={{ color: '#aeb7c6' }}>請求：{formatPayload(order.operation_payload)}</div>}
                            {order.instrument && <div style={{ color: '#aeb7c6' }}>商品：{formatPayload(order.instrument)}</div>}
                            {order.risk_reasons?.length ? <div style={{ color: '#f0b44d' }}>風控：{order.risk_reasons.join('、')}</div> : null}
                        </div>;
                    })}
                </div>
            )}
            {kind === 'signal_timeline' && overview?.decisions.length ? (
                <div style={{ display: 'grid', gap: 5 }}>
                    {overview.decisions.map((decision) => (
                        <div key={decision.intent_id} style={{ borderLeft: '3px solid #f0b44d', paddingLeft: 7 }}>
                            {decision.source} · {operationLabel(decision.operation)} · {decision.status} · {decision.correlation_id.slice(0, 8)}
                            {decision.risk_reasons.length ? <div style={{ color: '#f0b44d' }}>風控：{decision.risk_reasons.join('、')}</div> : null}
                        </div>
                    ))}
                </div>
            ) : null}
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
                    </div>
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
            {kind === 'risk_limits' && riskVerdictText(overview?.risk.last_verdict) && (
                <div role='status' style={{ color: overview?.risk.last_verdict?.allowed ? '#73c991' : '#f0b44d' }}>
                    {riskVerdictText(overview?.risk.last_verdict)}
                </div>
            )}
            {kind === 'run_performance' && overview && (
                <div style={{ display: 'grid', gap: 5, color: '#8892a6' }}>
                    <div>已實現 {overview.performance.realized_pnl}／未實現 {overview.performance.unrealized_pnl}</div>
                    {overview.runs.map((run) => (
                        <div key={run.run_id} style={{ borderLeft: '3px solid #8892a6', paddingLeft: 7 }}>
                            {run.strategy_id ?? '未命名策略'} · {run.run_id} · {run.last_status ?? 'UNKNOWN'} · {run.intent_count} intents
                        </div>
                    ))}
                </div>
            )}
            <small style={{ color: '#6f7888' }}>唯讀觀察模式；不會送單、取消、平倉或重送 UNKNOWN。</small>
        </section>
    );
}

function operationLabel(operation: string | undefined): string {
    return ({ PLACE: '下單', CANCEL: '取消', UPDATE_PRICE: '改價', UPDATE_QUANTITY: '改量' } as Record<string, string>)[operation ?? 'PLACE'] ?? operation ?? '下單';
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
