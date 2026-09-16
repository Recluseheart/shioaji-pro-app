import { describe, expect, it } from 'vitest';
import { gatewayApiCompatibility } from './auto-command-center';

describe('Auto Command Center Gateway version boundary', () => {
    it('requires the connection-observation API before describing a Gateway as current', () => {
        expect(gatewayApiCompatibility(0)).toBe('UPGRADE_REQUIRED');
        expect(gatewayApiCompatibility(1)).toBe('UPGRADE_REQUIRED');
        expect(gatewayApiCompatibility(2)).toBe('CURRENT');
    });
});
