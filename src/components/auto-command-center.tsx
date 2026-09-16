import { useEffect, useState } from 'react';
import { fetchAutotradeConnection, type AutotradeConnection, type AutotradeHealth } from '../autotrade/client';
import { autotradeObserverStore, type AutotradeObserverState } from '../autotrade/observer-store';

const CONNECTION_STALE_AFTER_MS = 15_000;

export type ConnectionFreshness = 'CURRENT' | 'STALE' | 'UNKNOWN';
export type GatewayApiCompatibility = 'CURRENT' | 'UPGRADE_REQUIRED';

/** Version 2 introduced the independently polled connection-observation route. */
export function gatewayApiCompatibility(version: number): GatewayApiCompatibility {
    return version >= 2 ? 'CURRENT' : 'UPGRADE_REQUIRED';
}

/** A cached healthy sidecar posture is never equivalent to a current probe. */
export function connectionFreshness(
    lastUpdatedAt: string | null,
    now: number = Date.now(),
): ConnectionFreshness {
    if (!lastUpdatedAt) return 'UNKNOWN';
    const observed = Date.parse(lastUpdatedAt);
    if (!Number.isFinite(observed) || observed > now + 1_000) return 'UNKNOWN';
    return now - observed > CONNECTION_STALE_AFTER_MS ? 'STALE' : 'CURRENT';
}

// Read-only until the Autotrade Gateway is live. This must never arm or submit.
export function AutoCommandCenter() {
    const [observer, setObserver] = useState<AutotradeObserverState>(autotradeObserverStore.snapshot());
    const [liveConnection, setLiveConnection] = useState<AutotradeConnection | null>(null);
    const [clock, setClock] = useState(() => Date.now());
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    useEffect(() => {
        let active = true;
        const refresh = () => {
            setClock(Date.now());
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
        api_version: 0, environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0,
        enabled_strategy_count: 0, gateway_status: 'UNAVAILABLE', capability_mode: 'OBSERVER',
        broker_connection: { state: 'DISCONNECTED', accounts: [], last_updated_at: null },
        }),
        ...(liveConnection ? { broker_connection: liveConnection } : {}),
    };
    const freshness = connectionFreshness(value.broker_connection.last_updated_at, clock);
    const compatibility = gatewayApiCompatibility(value.api_version);
    return (
        <div style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>自動交易總覽</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Status label='環境' value={value.environment} tone='#4aa3ff' />
                <Status label='交易閘門' value={value.trading_gate} tone='#f0b44d' />
                <Status label='帳戶連線' value={value.broker_connection.state} tone='#8892a6' />
                <Status label='Gateway API' value={`v${value.api_version}`} tone={compatibility === 'CURRENT' ? '#73c991' : '#f0b44d'} />
                <Status label='資料新鮮度' value={freshness} tone={freshness === 'CURRENT' ? '#73c991' : '#f0b44d'} />
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
            {compatibility === 'UPGRADE_REQUIRED' && <div role='status' style={{ color: '#f0b44d' }}>
                此 Gateway 未提供目前的帳戶連線觀測 API；請由操作人員安全更新或重啟 Gateway 後，再判定 sidecar 狀態。
            </div>}
            {freshness !== 'CURRENT' && <div role='status' style={{ color: '#f0b44d' }}>
                {freshness === 'STALE'
                    ? '帳戶連線資料已過期；保留最後一次觀測，不可據此判定市場或帳務可用。'
                    : '尚未取得可驗證的帳戶連線時間；不可據此判定市場或帳務可用。'}
            </div>}
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
