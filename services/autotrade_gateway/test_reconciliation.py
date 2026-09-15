from __future__ import annotations

import unittest

from services.autotrade_gateway.order_core import Status
from services.autotrade_gateway.reconciliation import terminal_callback_from_trade


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


if __name__ == "__main__":
    unittest.main()
