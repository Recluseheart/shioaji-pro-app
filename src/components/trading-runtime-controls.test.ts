import { describe, expect, it } from 'vitest';
import { fieldValue } from './trading-runtime-controls';

describe('private strategy settings form', () => {
    it('keeps prices as exact decimal text and counts as integers', () => {
        expect(fieldValue({ name: 'price', label: '價格', type: 'decimal' }, '101.25')).toBe('101.25');
        expect(fieldValue({ name: 'symbols', label: 'CB', type: 'text' }, '99991,23301')).toBe('99991,23301');
        expect(fieldValue({ name: 'max_lots', label: '上限', type: 'integer' }, '3')).toBe(3);
    });
});
