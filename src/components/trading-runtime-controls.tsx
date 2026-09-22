import { useEffect, useState } from 'react';
import { apiGet, apiPost } from '../lib/api';
import { autotradeObserverStore } from '../autotrade/observer-store';

type Config = { symbol: string; side: string; quantity: number; threshold: string; condition: string; max_signals: number };
type Runtime = { mode: string; execution_available: boolean; running: boolean; last_tick: string | null; last_error: string | null; trading_gate: string };

export function TradingRuntimeControls({ strategyId, reason }: { strategyId: string; reason: string }) {
    const [config, setConfig] = useState<Config>({ symbol: '2330', side: 'BUY', quantity: 1, threshold: '1000', condition: 'AT_OR_BELOW', max_signals: 1 });
    const [runtime, setRuntime] = useState<Runtime | null>(null);
    const [message, setMessage] = useState('');
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        let active = true;
        void apiGet<{ config: Config }>(`/api/v1/autotrade/strategies/${strategyId}`)
            .then(value => { if (active) setConfig(value.config); }).catch(() => undefined);
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
    return <section style={{ border: '1px solid #667085', padding: 10, display: 'grid', gap: 8 }}>
        <strong>{runtime?.mode === 'PAPER' ? '本機紙上交易 · 合成行情 · 不連券商' : 'Shioaji SIM 策略執行'}</strong>
        <div>執行服務：{runtime?.running ? '運行中' : '未就緒'} · {runtime?.trading_gate ?? 'UNKNOWN'}</div>
        {runtime?.last_error && <div role='alert'>執行服務暫停：{runtime.last_error}</div>}
        <form onSubmit={event => { event.preventDefault(); void perform(() => apiPost(`/api/v1/autotrade/strategies/${strategyId}/configure`, config), '策略設定已儲存；請先 Dry Run，再開始策略'); }} style={{ display: 'grid', gap: 6 }}>
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
        <div>觸發後以當時行情送出限價單；每個新行情最多一筆。暫停／停止不取消既有委託。</div>
        <div>交易閘門每次啟用五分鐘；請先填寫操作原因。</div>
        <div style={{ display: 'flex', gap: 8 }}>
            <button disabled={busy || !reason.trim() || !runtime?.execution_available || !runtime.running || runtime.trading_gate === 'KILLED' || runtime.trading_gate === 'ARMED'} onClick={() => execution('arm')}>啟用交易閘門</button>
            <button disabled={busy || !reason.trim() || runtime?.trading_gate !== 'ARMED'} onClick={() => execution('disarm')}>關閉交易閘門</button>
        </div>
        {!runtime?.execution_available && <div>此服務只觀察行情。SIM 下單尚未由啟動設定允許。</div>}
        <div role='status'>{message}</div>
    </section>;
}
