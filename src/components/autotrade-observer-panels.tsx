import { useEffect, useState } from 'react';
import {
    fetchAutotradeOverview,
    type AutotradeOverview,
} from '../autotrade/client';

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
        case 'signal_timeline': return overview.signals.length;
        case 'auto_orders': return overview.orders.length;
        case 'risk_limits': return overview.risk.last_verdict === null ? 0 : 1;
        case 'reconciliation': return overview.reconciliation.length;
        case 'run_performance': return overview.performance.run_count;
    }
}

export function AutotradeObserverPanels({ kind }: { kind: AutotradeObserverPanel }) {
    const [overview, setOverview] = useState<AutotradeOverview | null>(null);
    const [error, setError] = useState<string | null>(null);
    useEffect(() => {
        void fetchAutotradeOverview().then(setOverview).catch(() => setError('Gateway 尚未連線'));
    }, []);
    const copy = PANEL_COPY[kind];
    const count = overview ? countFor(kind, overview) : 0;
    const displayedOrders = kind === 'reconciliation' ? overview?.reconciliation : overview?.orders;
    return (
        <section style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>{copy.title}</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Badge label='環境' value={overview?.health.environment ?? 'UNKNOWN'} tone='#4aa3ff' />
                <Badge label='閘門' value={overview?.health.trading_gate ?? 'UNKNOWN'} tone='#f0b44d' />
                <Badge label='項目' value={String(count)} tone='#8892a6' />
            </div>
            <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>{error ?? copy.empty}</div>
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
            {kind === 'strategy_control' && (
                <>
                    {overview?.strategies.map((item, index) => {
                        const strategy = item as { strategy_id?: string; version?: string; state?: string; enabled?: boolean };
                        return <div key={`${strategy.strategy_id ?? 'strategy'}-${index}`} style={{ color: '#aeb7c6' }}>
                            {strategy.strategy_id ?? '未命名策略'} {strategy.version ?? ''} · {strategy.state ?? 'UNKNOWN'} · {strategy.enabled ? '已啟用' : '未啟用'}
                        </div>;
                    })}
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <button disabled title='Gateway 目前固定 DISARMED'>驗證策略</button>
                        <button disabled title='需要獨立策略 runtime 授權'>Dry Run</button>
                        <button disabled title='需要獨立策略 runtime 授權'>開始</button>
                        <button disabled title='Gateway 目前固定 DISARMED'>暫停</button>
                    </div>
                </>
            )}
            {kind === 'run_performance' && overview && (
                <div style={{ color: '#8892a6' }}>
                    已實現 {overview.performance.realized_pnl}／未實現 {overview.performance.unrealized_pnl}
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

function Badge({ label, value, tone }: { label: string; value: string; tone: string }) {
    return <span style={{ border: `1px solid ${tone}`, borderRadius: 4, color: tone, padding: '4px 7px' }}>{label}：{value}</span>;
}
