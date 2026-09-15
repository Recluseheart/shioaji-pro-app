from __future__ import annotations

import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
