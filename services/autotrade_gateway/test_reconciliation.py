from __future__ import annotations

import unittest

from services.autotrade_gateway.order_core import Status
from services.autotrade_gateway.reconciliation import SimulationTradeProvider, terminal_callback_from_trade
from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount


class ReconciliationParserTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
