import { useEffect, useState } from 'react';
import {
    applyAutotradeKillSwitch,
    fetchAutotradeConnection,
    type AutotradeConnection,
    type AutotradeHealth,
    type AutotradeOverview,
} from '../autotrade/client';
import { autotradeObserverStore, type AutotradeObserverState } from '../autotrade/observer-store';
import { canLoadPreset, loadWorkspacePreset } from '../autotrade/workspace-commands';

const CONNECTION_STALE_AFTER_MS = 15_000;
const CONNECTION_OBSERVATION_API_VERSION = 2;

export type ConnectionFreshness = 'CURRENT' | 'STALE' | 'UNKNOWN';
export type GatewayApiCompatibility = 'CURRENT' | 'UPGRADE_REQUIRED';

export const RUNTIME_MODE_LABELS: Record<string, string> = {
    PAPER: '本機紙上交易', SIM: '模擬盤', OBSERVE: '只讀觀察',
};

export const WORKSPACE_PRESETS = ['完整交易終端', '自動交易控制', '自動交易觀察'] as const;

/** Version 2 introduced the independently polled connection-observation route. */
export function gatewayApiCompatibility(version: number): GatewayApiCompatibility {
    return version >= CONNECTION_OBSERVATION_API_VERSION ? 'CURRENT' : 'UPGRADE_REQUIRED';
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

/** Remaining arm time as m:ss, or null when the gate has no expiry. */
export function armRemaining(armExpiresAt: number | null, now: number = Date.now()): string | null {
    if (!armExpiresAt) return null;
    const seconds = Math.max(0, Math.floor(armExpiresAt - now / 1000));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

/** Everything an operator should look at, in plain language. */
export function commandCenterIssues(overview: AutotradeOverview | null, error: string | null): string[] {
    const issues: string[] = [];
    if (error) issues.push(error);
    if (!overview) return issues;
    if (overview.health.trading_gate === 'KILLED') issues.push('Kill Switch 已啟用：所有新委託都會被拒絕');
    if (overview.runtime.last_error) issues.push(`執行服務：${overview.runtime.last_error}`);
    for (const item of overview.strategies) {
        const strategy = item as { display_name?: string; strategy_id?: string; last_error?: string | null };
        if (strategy.last_error) issues.push(`策略 ${strategy.display_name ?? strategy.strategy_id ?? ''}：${strategy.last_error}`);
    }
    if (overview.reconciliation.length) {
        issues.push(`${overview.reconciliation.length} 筆委託無法確定是否已到券商，需要人工對帳`);
    }
    return issues;
}

export function AutoCommandCenter() {
    const [observer, setObserver] = useState<AutotradeObserverState>(autotradeObserverStore.snapshot());
    const [liveConnection, setLiveConnection] = useState<AutotradeConnection | null>(null);
    const [clock, setClock] = useState(() => Date.now());
    const [killReason, setKillReason] = useState('');
    const [killMessage, setKillMessage] = useState<string | null>(null);
    const apiVersion = observer.overview?.health.api_version ?? 0;
    const armed = observer.overview?.health.trading_gate === 'ARMED';
    useEffect(() => {
        return autotradeObserverStore.subscribe(setObserver);
    }, []);
    useEffect(() => {
        if (gatewayApiCompatibility(apiVersion) === 'UPGRADE_REQUIRED') {
            setLiveConnection(null);
            return;
        }
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
    }, [apiVersion]);
    useEffect(() => {
        if (!armed) return;
        const timer = setInterval(() => setClock(Date.now()), 1_000);
        return () => clearInterval(timer);
    }, [armed]);
    const overview = observer.overview;
    const value: AutotradeHealth = {
        ...(overview?.health ?? {
        api_version: 0, environment: 'SIM', trading_gate: 'DISARMED', strategy_count: 0,
        enabled_strategy_count: 0, gateway_status: 'UNAVAILABLE', capability_mode: 'OBSERVER',
        broker_connection: { state: 'DISCONNECTED', accounts: [], last_updated_at: null },
        }),
        ...(liveConnection ? { broker_connection: liveConnection } : {}),
    };
    const freshness = connectionFreshness(value.broker_connection.last_updated_at, clock);
    const compatibility = gatewayApiCompatibility(value.api_version);
    const observerOnly = value.capability_mode === 'OBSERVER';
    const remaining = armed ? armRemaining(overview?.runtime.arm_expires_at ?? null, clock) : null;
    const issues = commandCenterIssues(overview, overview ? null : observer.error);
    const today = overview?.today;
    const performance = overview?.performance;
    const usage = overview?.risk.usage;
    const runKillSwitch = () => {
        if (!killReason.trim()) return;
        const confirmed = globalThis.confirm?.(
            '確定啟用 Kill Switch？\n啟用後所有新委託（手動與策略）都會被拒絕，排隊中的委託不會送出。\n'
            + '既有委託與持倉不會被自動取消或平倉。\n此狀態無法在介面上解除，需由操作人員在服務端處理。',
        );
        if (!confirmed) return;
        void applyAutotradeKillSwitch(killReason.trim(), globalThis.crypto.randomUUID())
            .then((receipt) => {
                setKillMessage(receipt.applied ? `Kill Switch 已啟用：${receipt.reason}` : `Kill Switch 早已啟用：${receipt.reason}`);
                return autotradeObserverStore.refresh();
            })
            .catch(() => setKillMessage('Kill Switch 請求失敗；請確認 Gateway 連線。'));
    };
    return (
        <div style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>自動交易總覽</strong>
            {!overview && !observer.error && <div role='status' style={{ color: '#aeb7c6' }}>載入中…</div>}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Status label='環境' value={value.environment} tone='#4aa3ff' />
                <Status label='模式' value={RUNTIME_MODE_LABELS[overview?.runtime.mode ?? 'OBSERVE'] ?? overview?.runtime.mode ?? '—'} tone='#4aa3ff' />
                <Status label='交易閘門' value={remaining ? `${value.trading_gate}（剩 ${remaining}）` : value.trading_gate}
                    tone={value.trading_gate === 'ARMED' ? '#73c991' : value.trading_gate === 'KILLED' ? '#ef4444' : '#f0b44d'} />
                <Status label='帳戶連線' value={value.broker_connection.state} tone='#8892a6' />
                <Status label='資料新鮮度' value={freshness} tone={freshness === 'CURRENT' ? '#73c991' : '#f0b44d'} />
                <Status label='策略' value={`${value.enabled_strategy_count}/${value.strategy_count} 啟用`} tone='#8892a6' />
                <Status label='Gateway API' value={`v${value.api_version}`} tone={compatibility === 'CURRENT' ? '#73c991' : '#f0b44d'} />
            </div>
            {value.broker_connection.accounts.length > 0 && (
                <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>
                    {value.environment === 'PAPER' ? '本機 PAPER 帳戶：' : '已發現 simulation 帳戶：'}{value.broker_connection.accounts.map(account =>
                        `${account.account_type} ${account.account_label}${account.signed ? '（已簽署）' : '（未簽署）'}`,
                    ).join('、')}
                </div>
            )}
            {today && (
                <div aria-label='今日自動交易'>
                    今日（{today.date ?? '—'}）：自動委託 {today.auto_order_count} 筆（成交 {today.auto_filled_count}、風控拒絕 {today.auto_rejected_count}）
                    · 手動 {today.manual_order_count} 筆 · 未完成 {today.open_order_count} 筆
                </div>
            )}
            {performance && (
                <div aria-label='損益與曝險'>
                    損益：已實現 {performance.realized_pnl ?? '尚無資料'} · 未實現 {performance.unrealized_pnl ?? '尚無資料'}
                    {performance.source === 'PAPER_ESTIMATE' ? '（本機估算）' : performance.source === 'BROKER_ACCOUNT' ? '（券商帳戶）' : ''}
                    {performance.stale ? '（資料過期）' : ''}
                    <br />
                    曝險：股票帳戶 {performance.gross_exposure ?? '尚無資料'} · 未成交委託預留 {usage?.pending_exposure ?? '0'}
                </div>
            )}
            <div aria-label='異常' style={{ display: 'grid', gap: 3 }}>
                {issues.length === 0
                    ? <span style={{ color: '#73c991' }}>目前沒有需要處理的異常。</span>
                    : issues.map((issue) => <span key={issue} role='alert' style={{ color: '#ef4444' }}>⚠ {issue}</span>)}
            </div>
            {canLoadPreset() && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }} aria-label='一鍵切換版面'>
                    {WORKSPACE_PRESETS.map((name) => (
                        <button key={name} onClick={() => loadWorkspacePreset(name)} title='只改變畫面版面；不會改變策略或交易閘門'>
                            切換到「{name}」
                        </button>
                    ))}
                </div>
            )}
            {!observerOnly && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }} aria-label='Kill Switch'>
                    {value.trading_gate === 'KILLED' ? (
                        <span style={{ color: '#ef4444' }}>Kill Switch 已啟用；介面無法解除，需由操作人員在服務端處理。</span>
                    ) : (
                        <>
                            <input aria-label='Kill Switch 原因' value={killReason} onChange={(event) => setKillReason(event.target.value)} placeholder='啟用 Kill Switch 的原因' />
                            <button disabled={!killReason.trim()} onClick={runKillSwitch} style={{ color: '#ef4444' }}
                                title='立即拒絕所有新委託；不會自動取消既有委託或平倉'>啟用 Kill Switch</button>
                        </>
                    )}
                </div>
            )}
            {killMessage && <div role='status' style={{ color: '#ef4444' }}>{killMessage}</div>}
            <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>
                {overview ? `Gateway ${value.gateway_status}` : observer.error ?? 'Gateway 尚未回應'}。此總覽不會啟動策略、送出委託或重送 UNKNOWN；切換版面不會改變策略或交易閘門；Kill Switch 只阻擋新委託，不會自動取消既有委託。
            </div>
            {observer.stale && <div role='status' style={{ color: '#f0b44d' }}>顯示最後一次成功的 Gateway 狀態；沒有重送委託。</div>}
            {compatibility === 'UPGRADE_REQUIRED' && overview && <div role='status' style={{ color: '#f0b44d' }}>
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
