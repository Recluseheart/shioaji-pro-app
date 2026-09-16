import { describe, expect, it } from 'vitest';
import { connectionFreshness } from './auto-command-center';

describe('Auto Command Center connection freshness', () => {
    const now = Date.parse('2026-09-16T01:00:00+00:00');

    it('marks an old cached sidecar posture stale instead of presenting it as live', () => {
        expect(connectionFreshness('2026-09-16T00:59:50+00:00', now)).toBe('CURRENT');
        expect(connectionFreshness('2026-09-16T00:59:40+00:00', now)).toBe('STALE');
    });

    it('fails closed when the Gateway did not provide a valid observation time', () => {
        expect(connectionFreshness(null, now)).toBe('UNKNOWN');
        expect(connectionFreshness('not-a-timestamp', now)).toBe('UNKNOWN');
    });
});
