import type { AutotradeOrderObservation } from './client';

export type AutotradeOrderOverlay = {
    intentId: string;
    price: number;
    side: 'BUY' | 'SELL';
    quantity: number;
    status: string;
};

/**
 * Derive safe, read-only chart annotations from durable observations.
 *
 * An annotation is deliberately limited to a priced PLACE intent owned by a
 * strategy/system trigger.  Manual intents, target-order operations and
 * market-priced intents cannot be represented as a truthful price line.
 */
export function autotradeOrderOverlays(
    orders: AutotradeOrderObservation[],
    symbol: string,
): AutotradeOrderOverlay[] {
    return orders.flatMap((order): AutotradeOrderOverlay[] => {
        const price = Number(order.limit_price);
        if (
            order.symbol !== symbol
            || order.operation !== 'PLACE'
            || (order.source !== 'STRATEGY' && order.source !== 'SYSTEM_TRIGGER')
            || (order.side !== 'BUY' && order.side !== 'SELL')
            || !Number.isFinite(price)
            || price <= 0
            || !Number.isInteger(order.quantity)
            || order.quantity <= 0
        ) return [];
        return [{
            intentId: order.intent_id,
            price,
            side: order.side,
            quantity: order.quantity,
            status: order.status,
        }];
    });
}
