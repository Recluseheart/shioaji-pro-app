import { useEffect, useState } from 'react';
import { apiGet, apiPost } from '../lib/api';
import { autotradeObserverStore } from '../autotrade/observer-store';

type Config = { symbol: string; side: string; quantity: number; threshold: string; condition: string; max_signals: number };
type Runtime = { mode: string; execution_available: boolean; running: boolean; last_tick: string | null; last_error: string | null; trading_gate: string };
// A private strategy describes its own settings; its logic never reaches the browser.
export type ConfigField = { name: string; label: string; type: 'text' | 'decimal' | 'integer'; min?: number | string; max?: number | string; step?: string };
type StrategyDocument = { config: Record<string, unknown>; config_schema?: ConfigField[]; display_name?: string; private_strategy?: boolean };

export function fieldValue(field: ConfigField, raw: string): string | number {
    return field.type === 'integer' ? Number(raw) : raw;
}

export function TradingRuntimeControls({ strategyId, reason }: { strategyId: string; reason: string }) {
    const [config, setConfig] = useState<Config>({ symbol: '2330', side: 'BUY', quantity: 1, threshold: '1000', condition: 'AT_OR_BELOW', max_signals: 1 });
    const [pluginConfig, setPluginConfig] = useState<Record<string, unknown> | null>(null);
    const [schema, setSchema] = useState<ConfigField[] | null>(null);
    const [displayName, setDisplayName] = useState<string | null>(null);
    const [runtime, setRuntime] = useState<Runtime | null>(null);
    const [message, setMessage] = useState('');
    const [busy, setBusy] = useState(false);
    const [paperSymbol, setPaperSymbol] = useState('99991');
    const [paperPrice, setPaperPrice] = useState('100');
    useEffect(() => {
        let active = true;
        void apiGet<StrategyDocument>(`/api/v1/autotrade/strategies/${strategyId}`)
            .then(value => {
                if (!active) return;
                if (Array.isArray(value.config_schema)) {
                    setSchema(value.config_schema);
                    setPluginConfig(value.config);
                    setDisplayName(value.display_name ?? null);
                } else {
                    setSchema(null);
                    setConfig(value.config as Config);
                }
            }).catch(() => undefined);
        const refresh = () => { void apiGet<Runtime>('/api/v1/autotrade/runtime')
            .then(value => { if (active) setRuntime(value); }).catch(() => { if (active) setRuntime(null); }); };
        refresh();
        const timer = setInterval(refresh, 2000);
        return () => { active = false; clearInterval(timer); };
    }, [strategyId]);
    const perform = async (work: () => Promise<unknown>, success: string) => {
        setBusy(true); setMessage('處理中…');
        try { await work(); setMessage(success); await autotradeObserverStore.refresh(); }
        catch (error) { setMessage(error instanceof Error ? error.message : '操作失敗'); }
        finally { setBusy(false); }
    };
    const execution = (action: 'arm' | 'disarm') => {
        if (!reason?.trim()) return;
        void perform(() => apiPost(`/api/v1/autotrade/execution/${action}`, { reason: reason.trim() },
            { headers: { 'Idempotency-Key': crypto.randomUUID() } }), action === 'arm' ? '交易閘門已啟用五分鐘' : '交易閘門已關閉');
    };
    const saveConfig = (body: unknown) => void perform(
        () => apiPost(`/api/v1/autotrade/strategies/${strategyId}/configure`, body),
        '策略設定已儲存；請先 Dry Run，再開始策略',
    );
    return <section style={{ border: '1px solid #667085', padding: 10, display: 'grid', gap: 8 }}>
        <strong>{runtime?.mode === 'PAPER' ? '本機紙上交易 · 合成行情 · 不連券商' : 'Shioaji SIM 策略執行'}</strong>
        <div>執行服務：{runtime?.running ? '運行中' : '未就緒'} · {runtime?.trading_gate ?? 'UNKNOWN'}</div>
        {runtime?.last_error && <div role='alert'>執行服務暫停：{runtime.last_error}</div>}
        {schema && pluginConfig ? (
            <form aria-label='私有策略設定' onSubmit={event => { event.preventDefault(); saveConfig(pluginConfig); }} style={{ display: 'grid', gap: 6 }}>
                <div>{displayName ?? strategyId} · 私有策略（策略邏輯不在程式碼庫）</div>
                {schema.map(field => (
                    <label key={field.name}>{field.label} <input
                        type={field.type === 'text' ? 'text' : 'number'}
                        min={field.min} max={field.max}
                        // HTML step validation is anchored at min (0.01 + 0.05k would reject 99);
                        // decimals are validated by the strategy on the server instead.
                        step={field.type === 'integer' ? 1 : 'any'}
                        required
                        value={String(pluginConfig[field.name] ?? '')}
                        onChange={e => setPluginConfig({ ...pluginConfig, [field.name]: fieldValue(field, e.target.value) })}
                    /></label>
                ))}
                <button disabled={busy} type='submit'>儲存策略設定</button>
            </form>
        ) : (
            <form onSubmit={event => { event.preventDefault(); saveConfig(config); }} style={{ display: 'grid', gap: 6 }}>
                <label>策略商品 <select value={config.symbol} onChange={e => setConfig({ ...config, symbol: e.target.value })}>
                    {['2330', '2317', '0050'].map(symbol => <option key={symbol}>{symbol}</option>)}
                </select></label>
                <label>方向 <select value={config.side} onChange={e => setConfig({ ...config, side: e.target.value })}>
                    <option value='BUY'>買進</option><option value='SELL'>賣出</option>
                </select></label>
                <label>數量（股，盤中零股） <input type='number' min={1} max={999} required value={config.quantity} onChange={e => setConfig({ ...config, quantity: Number(e.target.value) })} /></label>
                <label>價格門檻（元） <input type='number' min='0.01' step='0.01' required value={config.threshold} onChange={e => setConfig({ ...config, threshold: e.target.value })} /></label>
                <label>觸發條件 <select value={config.condition} onChange={e => setConfig({ ...config, condition: e.target.value })}>
                    <option value='AT_OR_BELOW'>價格小於或等於門檻</option><option value='AT_OR_ABOVE'>價格大於或等於門檻</option>
                </select></label>
                <label>每次執行最多訊號數 <input type='number' min={1} max={100} required value={config.max_signals} onChange={e => setConfig({ ...config, max_signals: Number(e.target.value) })} /></label>
                <button disabled={busy} type='submit'>儲存策略設定</button>
            </form>
        )}
        <div>{schema ? '每筆新成交最多觸發一次限價單，並經過共同風控；暫停／停止不取消既有委託。' : '觸發後以當時行情送出限價單；每個新行情最多一筆。暫停／停止不取消既有委託。'}</div>
        <div>交易閘門每次啟用五分鐘；請先填寫操作原因。</div>
        <div style={{ display: 'flex', gap: 8 }}>
            <button disabled={busy || !reason.trim() || !runtime?.execution_available || !runtime.running || runtime.trading_gate === 'KILLED' || runtime.trading_gate === 'ARMED'} onClick={() => execution('arm')}>啟用交易閘門</button>
            <button disabled={busy || !reason.trim() || runtime?.trading_gate !== 'ARMED'} onClick={() => execution('disarm')}>關閉交易閘門</button>
        </div>
        {runtime?.mode === 'PAPER' && (
            <form aria-label='PAPER 合成價格' onSubmit={event => {
                event.preventDefault();
                void perform(() => apiPost('/api/v1/autotrade/paper/prices', { symbol: paperSymbol, price: paperPrice }),
                    `PAPER 合成價格已設為 ${paperSymbol} ${paperPrice}`);
            }} style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                <span>測試用合成價格（僅 PAPER）</span>
                {/* Any code the PAPER Gateway knows, including CBs given at launch; the Gateway validates it. */}
                <input aria-label='合成價格商品' list='paper-price-symbols' required size={8} value={paperSymbol}
                    onChange={e => setPaperSymbol(e.target.value.trim().toUpperCase())} />
                <datalist id='paper-price-symbols'>
                    {['99991', '2330', '2317', '0050', 'TXFR1'].map(symbol => <option key={symbol} value={symbol} />)}
                </datalist>
                <input aria-label='合成價格' type='number' min='0.01' step='any' required value={paperPrice} onChange={e => setPaperPrice(e.target.value)} />
                <button disabled={busy} type='submit'>設定價格</button>
            </form>
        )}
        {!runtime?.execution_available && <div>此服務只觀察行情。SIM 下單尚未由啟動設定允許。</div>}
        <div role='status'>{message}</div>
    </section>;
}
