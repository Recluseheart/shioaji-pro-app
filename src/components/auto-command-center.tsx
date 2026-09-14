// Read-only until the Autotrade Gateway is live. This must never arm or submit.
export function AutoCommandCenter() {
    return (
        <div style={{ padding: 12, display: 'grid', gap: 10, fontSize: 13 }}>
            <strong>自動交易總覽</strong>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Status label='環境' value='SIM' tone='#4aa3ff' />
                <Status label='交易閘門' value='DISARMED' tone='#f0b44d' />
                <Status label='策略' value='尚未啟用' tone='#8892a6' />
            </div>
            <div style={{ color: '#aeb7c6', lineHeight: 1.6 }}>
                Gateway 尚未連線。此面板只顯示安全狀態；不會啟動策略、送出委託或重送 UNKNOWN。
            </div>
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
