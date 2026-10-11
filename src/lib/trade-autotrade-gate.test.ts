import { describe, expect, it } from 'vitest';
import { assertGatewayInitiator } from './autotrade-order-initiator';

describe('Autotrade browser initiator boundary', () => {
    it('permits only a manual UI source to enter the current Gateway order path', () => {
        expect(() => assertGatewayInitiator('manual')).not.toThrow();
    });

    it.each(['auto', 'agent'] as const)('fails closed for %s without source impersonation', (source) => {
        expect(() => assertGatewayInitiator(source)).toThrow('尚未移植至 Gateway');
    });
});
