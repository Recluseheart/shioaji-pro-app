import { useEffect, useState } from 'react';
import { fetchAutotradeConnection, type AutotradeConnection, type AutotradeHealth } from '../autotrade/client';
import { autotradeObserverStore, type AutotradeObserverState } from '../autotrade/observer-store';

// Read-only until the Autotrade Gateway is live. This must never arm or submit.
export function AutoCommandCenter() {
    const [observer, setObserver] = useState<AutotradeObserverState>(autotradeObserverStore.snapshot());
    const [liveConnection, setLiveConnection] = useState<AutotradeConnection | null>(null);
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    useEffect(() => {
        let active = true;
        const refresh = () => {
            void fetchAutotradeConnection().then(value => {
                if (active) setLiveConnection(value);
            }).catch(() => undefined);
        };
        refresh();
        const timer = setInterval(refresh, 5_000);
        return () => { active = false; clearInterval(timer); };
    }, []);
    const value: AutotradeHealth = {
        ...(observer.overview?.health ?? {
        environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0,
        enabled_strategy_count: 0, gateway_status: 'UNAVAILABLE', capability_mode: 'OBSERVER',
        broker_connection: { state: 'DISCONNECTED', accounts: [], last_updated_at: null },
        }),
        ...(liveConnection ? { broker_connection: liveConnection } : {}),
    };
    return (
        <div style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>自動交易總覽</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Status label='環境' value={value.environment} tone='#4aa3ff' />
                <Status label='交易閘門' value={value.trading_gate} tone='#f0b44d' />
                <Status label='帳戶連線' value={value.broker_connection.state} tone='#8892a6' />
                <Status label='策略' value={`${value.enabled_strategy_count}/${value.strategy_count} 啟用`} tone='#8892a6' />
            </div>
            {value.broker_connection.accounts.length > 0 && (
                <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>
                    已發現 simulation 帳戶：{value.broker_connection.accounts.map(account =>
                        `${account.account_type} ${account.account_label}${account.signed ? '（已簽署）' : '（未簽署）'}`,
                    ).join('、')}
                </div>
            )}
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
