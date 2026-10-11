export type OrderInitiator = 'manual' | 'auto' | 'agent';

/**
 * The browser cannot safely represent a strategy, trigger, or Agent as a
 * MANUAL_UI order. Reject those sources until they run behind the Gateway
 * with a durable runtime identity.
 */
export function assertGatewayInitiator(initiator: OrderInitiator): void {
    if (initiator !== 'manual') {
        throw Object.assign(
            new Error(
                '自動策略、觸價單與 Agent 下單尚未移植至 Gateway 的 durable intent 鏈路',
            ),
            { mutationNotStarted: true as const },
        );
    }
}
