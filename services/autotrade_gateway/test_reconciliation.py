from __future__ import annotations

import unittest

from services.autotrade_gateway.order_core import Gate, Intent, OrderCoordinator, RiskContext, SQLiteIntentStore, Source, Status
from services.autotrade_gateway.reconciliation import (
    ReconciliationRefresher,
    SimulationTradeProvider,
    apply_reconciled_terminals,
    terminal_callback_from_trade,
)
from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount


class ReconciliationParserTest(unittest.TestCase):
    def test_apply_resolves_only_matching_unknown(self) -> None:
        from datetime import datetime, timezone
        from decimal import Decimal
        store = SQLiteIntentStore(":memory:")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(store, Gate.ARMED, risk_context_provider=lambda _i: RiskContext(Decimal("1000"), datetime.now(timezone.utc))).accept(intent)
        store.mark_pending_submit(intent.intent_id, "broker-1")
        store.mark_delivery_unknown(intent.intent_id, "timeout")
        self.assertEqual(apply_reconciled_terminals(store, (("broker-1", Status.FILLED), ("broker-only", Status.FILLED))), 1)
        self.assertEqual(store.status(intent.intent_id), Status.FILLED)
        store.close()

    def test_apply_leaves_unknown_when_duplicate_terminal_facts_conflict(self) -> None:
        """A contradictory broker read cannot pick an arbitrary terminal state."""
        from datetime import datetime, timezone
        from decimal import Decimal

        store = SQLiteIntentStore(":memory:")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(
            store,
            Gate.ARMED,
            risk_context_provider=lambda _i: RiskContext(
                Decimal("1000"), datetime.now(timezone.utc)
            ),
        ).accept(intent)
        store.mark_pending_submit(intent.intent_id, "broker-conflict-1")
        store.mark_delivery_unknown(intent.intent_id, "timeout")

        self.assertEqual(
            apply_reconciled_terminals(
                store,
                (
                    ("broker-conflict-1", Status.FILLED),
                    ("broker-conflict-1", Status.CANCELLED),
                ),
            ),
            0,
        )
        self.assertEqual(store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(store.observation_reconciliation_cases()[0]["state"], "OPEN")
        store.close()
    def test_maps_only_matching_authoritative_terminal_trade_statuses(self) -> None:
        for source_status, expected in (
            ("Filled", Status.FILLED),
            ("Cancelled", Status.CANCELLED),
            ("Failed", Status.BROKER_REJECTED),
        ):
            with self.subTest(source_status=source_status):
                self.assertEqual(terminal_callback_from_trade({
                    "order": {"id": "broker-1"},
                    "status": {"id": "broker-1", "status": source_status},
                }), ("broker-1", expected))

    def test_rejects_partial_mismatched_and_malformed_trade_facts(self) -> None:
        for trade in (
            {"order": {"id": "broker-1"}, "status": {"id": "broker-1", "status": "PartFilled"}},
            {"order": {"id": "broker-1"}, "status": {"id": "other", "status": "Filled"}},
            {"order": {"id": "broker-1"}, "status": {"status": "Filled"}},
            {"order": {"id": "broker-1"}, "status": {"id": "broker-1", "status": "Inactive"}},
        ):
            with self.subTest(trade=trade):
                self.assertIsNone(terminal_callback_from_trade(trade))

    def test_provider_reads_only_account_scoped_trades_and_filters_nonterminals(self) -> None:
        received = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self):
                return b'[{"order":{"id":"broker-1"},"status":{"id":"broker-1","status":"Filled"}},{"order":{"id":"broker-2"},"status":{"id":"broker-2","status":"PartFilled"}}]'

        def opener(request, *, timeout):
            received.append((request.full_url, request.data, timeout))
            return Response()

        provider = SimulationTradeProvider("http://127.0.0.1:21322", opener=opener)
        facts = provider.terminal_facts(SimulationStockAccount("broker", "account"))

        self.assertEqual(facts, (("broker-1", Status.FILLED),))
        self.assertEqual(received[0][0], "http://127.0.0.1:21322/api/v1/order/trades")
        self.assertIn(b'"account_type":"S"', received[0][1])

    def test_refresher_reads_one_verified_account_and_never_dispatches(self) -> None:
        from datetime import datetime, timezone
        from decimal import Decimal

        store = SQLiteIntentStore(":memory:")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(
            store,
            Gate.ARMED,
            risk_context_provider=lambda _i: RiskContext(
                Decimal("1000"), datetime.now(timezone.utc)
            ),
        ).accept(intent)
        store.mark_pending_submit(intent.intent_id, "broker-refresh-1")
        store.mark_delivery_unknown(intent.intent_id, "timeout")

        class Provider:
            received: list[SimulationStockAccount] = []

            def terminal_facts(self, account: SimulationStockAccount):
                self.received.append(account)
                return (("broker-refresh-1", Status.CANCELLED),)

        account = SimulationStockAccount("broker", "verified-paper-account")
        provider = Provider()
        result = ReconciliationRefresher(store, provider, account).refresh()

        self.assertEqual(provider.received, [account])
        self.assertEqual(result.observed_terminal_facts, 1)
        self.assertEqual(result.resolved_intents, 1)
        self.assertEqual(store.status(intent.intent_id), Status.CANCELLED)
        self.assertEqual(store.outbox_state(intent.intent_id), "TERMINAL")
        store.close()


if __name__ == "__main__":
    unittest.main()
