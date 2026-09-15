import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../lib/api', () => ({ apiGet: api.get, apiPost: api.post }));

import { normalizeAutotradeOverview } from '../autotrade/client';
import {
    automatedOrderObservations,
    DurableOrderDetail,
    filterTimeline,
    orderObservationText,
    panelSummary,
    riskVerdictText,
} from './autotrade-observer-panels';

beforeEach(() => vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true));
afterEach(() => vi.unstubAllGlobals());

describe('Autotrade observer panel summaries', () => {
    it('filters signals, decisions, and immutable events by strategy, product, status, and time', () => {
        const overview = normalizeAutotradeOverview({
            signals: [{
                signal_id: 'signal-match', strategy_id: 'alpha', run_id: 'run-1', event_id: 'event-1',
                signal_type: 'ENTRY', symbol: '2330', side: 'BUY', intent_id: 'intent-1',
                correlation_id: 'correlation-1', status: 'RISK_REJECTED', risk_reasons: [],
                observed_at: '2026-09-15T00:00:00+00:00',
            }, {
                signal_id: 'signal-other', strategy_id: 'beta', run_id: 'run-2', event_id: 'event-2',
                signal_type: 'ENTRY', symbol: '2317', side: 'BUY', intent_id: 'intent-2',
                correlation_id: 'correlation-2', status: 'SUBMITTED', risk_reasons: [],
                observed_at: '2026-09-15T00:01:00+00:00',
            }],
            decisions: [{
                intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'STRATEGY', status: 'RISK_REJECTED',
                strategy_id: 'alpha', symbol: '2330', decided_at: '2026-09-15T00:00:10+00:00', risk_reasons: [],
            }],
            audit_events: [{
                event_id: 'audit-match', intent_id: 'intent-1', event_type: 'INTENT_EVALUATED',
                details: { strategy_id: 'alpha', symbol: '2330', status: 'RISK_REJECTED' },
                occurred_at: '2026-09-15T00:00:20+00:00',
            }],
        });

        expect(filterTimeline(overview, {
            strategy: 'alpha', symbol: '2330', status: 'RISK_REJECTED',
            from: '2026-09-15T00:00:00+00:00', to: '2026-09-15T00:00:20+00:00',
        })).toEqual({
            signals: [overview.signals[0]],
            decisions: [overview.decisions[0]],
            auditEvents: [overview.audit_events[0]],
        });
    });

    it('does not describe populated simulation limits as unavailable', () => {
        const overview = normalizeAutotradeOverview({
            risk: { limits: { max_order_quantity: 10000 } },
        });

        expect(panelSummary('risk_limits', overview, null)).toContain('已載入 simulation');
        expect(panelSummary('risk_limits', overview, null)).toContain('缺少時會拒絕');
        expect(panelSummary('risk_limits', overview, null)).not.toContain('尚未載入');
    });

    it('describes durable decisions without treating them as empty data', () => {
        const overview = normalizeAutotradeOverview({
            decisions: [{
                intent_id: 'decision-1', correlation_id: 'correlation-1', source: 'MANUAL_UI',
                status: 'RISK_REJECTED', risk_reasons: ['trading_gate_not_armed'],
            }],
        });

        expect(panelSummary('signal_timeline', overview, null)).toContain('durable intent');
    });

    it('counts a durable signal as timeline evidence before its linked decision', () => {
        const overview = normalizeAutotradeOverview({
            signals: [{
                signal_id: 'signal-1', strategy_id: 'reference-fixture', run_id: 'run-1',
                event_id: 'event-1', signal_type: 'REFERENCE_SIGNAL', symbol: '2330', side: 'BUY',
                intent_id: 'intent-1', correlation_id: 'correlation-1', status: 'RISK_REJECTED',
                risk_reasons: ['trading_gate_not_armed'], observed_at: '2026-09-15T00:00:00+00:00',
            }],
        });
        expect(panelSummary('signal_timeline', overview, null)).toContain('durable signal');
    });

    it('keeps a connection error ahead of any observation summary', () => {
        expect(panelSummary('risk_limits', null, 'Gateway 尚未連線')).toBe('Gateway 尚未連線');
    });

    it('presents the latest rejected verdict with a readable reason and short correlation', () => {
        expect(riskVerdictText({
            intent_id: 'intent-1', correlation_id: 'correlation-123456', status: 'RISK_REJECTED',
            allowed: false, reasons: ['trading_gate_not_armed'], source: 'MANUAL_UI', updated_at: '',
        })).toBe('最近風控：拒絕（交易閘門尚未啟用） · 追蹤 correlat');
    });

    it('defaults Auto Orders to strategy and system sources, excluding manual intents', () => {
        const overview = normalizeAutotradeOverview({
            orders: [
                { intent_id: 'manual', correlation_id: 'c1', source: 'MANUAL_UI', status: 'RISK_REJECTED' },
                { intent_id: 'strategy', correlation_id: 'c2', source: 'STRATEGY', status: 'RISK_REJECTED' },
                { intent_id: 'system', correlation_id: 'c3', source: 'SYSTEM_TRIGGER', status: 'RISK_REJECTED' },
            ],
        });
        expect(automatedOrderObservations(overview.orders).map((order) => order.intent_id)).toEqual(['strategy', 'system']);
        expect(panelSummary('auto_orders', overview, null)).toContain('自動委託');
    });

    it('renders durable product and strategy attribution without pretending a target mutation has a price', () => {
        expect(orderObservationText({
            operation: 'PLACE', symbol: '2330', side: 'BUY', quantity: 2,
            limit_price: '1000', order_price_type: 'LMT', strategy_id: 'reference', run_id: 'run-1',
        })).toBe('商品：2330 · 買 2 · LMT 1000 · 策略 reference · run run-1');
        expect(orderObservationText({ operation: 'UPDATE_PRICE', symbol: '2330', side: 'BUY', quantity: 2 })).toBeNull();
        expect(orderObservationText({ operation: 'PLACE', symbol: '2330', side: 'BUY' })).toBeNull();
    });
});

describe('DurableOrderDetail', () => {
    it('only reads the durable order endpoint after an explicit expansion', async () => {
        const loadObservation = vi.fn().mockResolvedValue({
            intent_id: 'intent-1', correlation_id: 'correlation-1', source: 'MANUAL_UI',
            status: 'RISK_REJECTED', symbol: '2330', side: 'BUY', quantity: 1,
            limit_price: '1000', order_price_type: 'LMT', strategy_id: null, run_id: null,
            broker_order_id: null, operation: 'PLACE', target_order_id: null,
            operation_payload: null, instrument: null, risk_reasons: ['trading_gate_not_armed'],
            updated_at: '2026-09-15T00:00:00+00:00',
        });
        let renderer!: ReturnType<typeof create>;
        await act(async () => { renderer = create(createElement(DurableOrderDetail, { intentId: 'intent-1', loadObservation })); });
        expect(loadObservation).not.toHaveBeenCalled();

        await act(async () => {
            renderer.root.findByType('button').props.onClick();
            await Promise.resolve();
        });
        expect(loadObservation).toHaveBeenCalledWith('intent-1');
        expect(JSON.stringify(renderer.toJSON())).toContain('RISK_REJECTED');
        expect(JSON.stringify(renderer.toJSON())).toContain('交易閘門尚未啟用');
        await act(async () => renderer.unmount());
    });

    it('does not fabricate a detail after a failed durable read', async () => {
        const loadObservation = vi.fn().mockRejectedValue(new Error('offline'));
        let renderer!: ReturnType<typeof create>;
        await act(async () => { renderer = create(createElement(DurableOrderDetail, { intentId: 'intent-2', loadObservation })); });
        await act(async () => {
            renderer.root.findByType('button').props.onClick();
            await Promise.resolve();
        });
        expect(JSON.stringify(renderer.toJSON())).toContain('Gateway 未提供此 durable 記錄');
        expect(JSON.stringify(renderer.toJSON())).not.toContain('intent intent-2');
        await act(async () => renderer.unmount());
    });
});
