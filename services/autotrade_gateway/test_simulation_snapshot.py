from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from services.autotrade_gateway.simulation_snapshot import SimulationSnapshotProvider


class Response:
    def __init__(self, value: object) -> None: self.value = value
    def __enter__(self): return self
    def __exit__(self, *_args: object) -> None: return None
    def read(self) -> bytes: return json.dumps(self.value).encode()


class SimulationSnapshotTest(unittest.TestCase):
    def test_only_a_verified_simulation_stock_account_is_discovered(self) -> None:
        def opener(request, *, timeout: float):
            del timeout
            return Response(
                {"simulation": True} if request.full_url.endswith("/info") else [
                    {"account_type": "S", "broker_id": "B", "account_id": "paper-1", "person_id": "hidden"},
                    {"account_type": "F", "broker_id": "B", "account_id": "future-1"},
                ],
            )
        provider = SimulationSnapshotProvider("http://127.0.0.1:21322", opener=opener)
        self.assertEqual(provider.simulation_stock_accounts()[0].account_id, "paper-1")

    def test_context_fails_closed_when_quote_or_position_payload_is_invalid(self) -> None:
        def opener(request, *, timeout: float):
            del timeout
            if request.full_url.endswith("/info"):
                return Response({"simulation": True})
            if request.full_url.endswith("/accounts"):
                return Response([])
            if request.full_url.endswith("/position_unit"):
                return Response([{"code": "2330", "quantity": "one"}])
            return Response([{"code": "2330", "close": 1000, "datetime": "2026-09-15T09:00:00+08:00"}])
        provider = SimulationSnapshotProvider("http://127.0.0.1:21322", opener=opener)
        from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount
        self.assertIsNone(provider.stock_risk_context(SimulationStockAccount("B", "paper-1"), "2330"))

    def test_position_snapshots_read_stock_and_futures_without_exposing_identity(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def opener(request, *, timeout: float):
            del timeout
            body = json.loads(request.data) if request.data else {}
            calls.append((request.full_url, body))
            if request.full_url.endswith("/info"):
                return Response({"simulation": True})
            if request.full_url.endswith("/accounts"):
                return Response([
                    {"account_type": "S", "broker_id": "B", "account_id": "paper-stock-1234"},
                    {"account_type": "F", "broker_id": "B", "account_id": "paper-future-5678"},
                ])
            if body["account_type"] == "S":
                self.assertEqual(body["unit"], "Share")
                return Response([{
                    "code": "2330", "direction": "Buy", "quantity": 2,
                    "price": 1000, "last_price": 1001.5, "pnl": 3,
                }])
            self.assertEqual(body["unit"], "Common")
            return Response([{
                "code": "TXFZ6", "direction": "Sell", "quantity": 1,
                "price": 22000, "last_price": 22010, "pnl": -10,
            }])

        provider = SimulationSnapshotProvider("http://127.0.0.1:21322", opener=opener)
        observed_at = datetime(2026, 9, 16, 1, 2, 3, tzinfo=timezone.utc)
        snapshots = provider.position_snapshots(observed_at=observed_at)

        self.assertIsNotNone(snapshots)
        self.assertEqual([(item.account_type, item.symbol, item.side, item.quantity) for item in snapshots or ()], [
            ("S", "2330", "LONG", "2"), ("F", "TXFZ6", "SHORT", "1"),
        ])
        self.assertEqual([item.observed_at for item in snapshots or ()], [observed_at, observed_at])
        self.assertNotIn("paper-stock-1234", repr([item.snapshot_id for item in snapshots or ()]))
        self.assertEqual([body["account_type"] for _, body in calls if body], ["S", "F"])

    def test_position_snapshots_fail_closed_on_a_malformed_broker_fact(self) -> None:
        def opener(request, *, timeout: float):
            del timeout
            if request.full_url.endswith("/info"):
                return Response({"simulation": True})
            if request.full_url.endswith("/accounts"):
                return Response([{"account_type": "S", "broker_id": "B", "account_id": "paper-1"}])
            return Response([{
                "code": "2330", "direction": "Buy", "quantity": 1,
                "price": 1000, "last_price": "not-a-decimal", "pnl": 0,
            }])

        provider = SimulationSnapshotProvider("http://127.0.0.1:21322", opener=opener)
        self.assertIsNone(provider.position_snapshots())


if __name__ == "__main__":
    unittest.main()
