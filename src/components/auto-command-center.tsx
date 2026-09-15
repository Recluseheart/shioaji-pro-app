import { useEffect, useState } from 'react';
import type { AutotradeHealth } from '../autotrade/client';
import { autotradeObserverStore, type AutotradeObserverState } from '../autotrade/observer-store';

// Read-only until the Autotrade Gateway is live. This must never arm or submit.
export function AutoCommandCenter() {
    const [observer, setObserver] = useState<AutotradeObserverState>(autotradeObserverStore.snapshot());
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    const health = observer.overview?.health ?? null;
    const value = health ?? { environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0, enabled_strategy_count: 0, gateway_status: 'UNAVAILABLE' as const };
    return (
        <div style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>自動交易總覽</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Status label='環境' value={value.environment} tone='#4aa3ff' />
                <Status label='交易閘門' value={value.trading_gate} tone='#f0b44d' />
                <Status label='策略' value={`${value.enabled_strategy_count}/${value.strategy_count} 啟用`} tone='#8892a6' />
            </div>
            <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>
                {observer.error ?? `Gateway ${value.gateway_status}`}。此面板只讀取狀態；不會啟動策略、送出委託或重送 UNKNOWN。
            </div>
            {observer.stale && <div role='status' style={{ color: '#f0b44d' }}>顯示最後一次成功的 Gateway 狀態；沒有重送委託。</div>}
        </div>
    );
}

function Status({ label, value, tone }: { label: string; value: string; tone: string }) {
    return (
        <span style={{ border: `1px solid ${tone}`, borderRadius: 4, color: tone, padding: '4px 7px' }}>
            {label}：{value}
        </span>
    );
}
