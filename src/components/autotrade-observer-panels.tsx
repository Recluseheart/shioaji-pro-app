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
        empty: '尚無已註冊策略。啟動功能會在 durable strategy runtime 移植後才開放。',
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
    return (
        <section style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>{copy.title}</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Badge label='環境' value={overview?.health.environment ?? 'UNKNOWN'} tone='#4aa3ff' />
                <Badge label='閘門' value={overview?.health.trading_gate ?? 'UNKNOWN'} tone='#f0b44d' />
                <Badge label='項目' value={String(count)} tone='#8892a6' />
            </div>
            <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>{error ?? copy.empty}</div>
            {kind === 'strategy_control' && (
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <button disabled title='Gateway 目前固定 DISARMED'>驗證策略</button>
                    <button disabled title='Gateway 目前固定 DISARMED'>Dry Run</button>
                    <button disabled title='Gateway 目前固定 DISARMED'>開始</button>
                    <button disabled title='Gateway 目前固定 DISARMED'>暫停</button>
                </div>
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

function Badge({ label, value, tone }: { label: string; value: string; tone: string }) {
    return <span style={{ border: `1px solid ${tone}`, borderRadius: 4, color: tone, padding: '4px 7px' }}>{label}：{value}</span>;
}
