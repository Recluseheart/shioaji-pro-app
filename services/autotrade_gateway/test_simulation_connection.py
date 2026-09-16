from __future__ import annotations

import json
import unittest

from services.autotrade_gateway.simulation_connection import (
    ConnectionState,
    SimulationConnectionMonitor,
)


class Response:
    def __init__(self, document: object) -> None:
        self.document = document

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.document).encode()


class SimulationConnectionMonitorTest(unittest.TestCase):
    def test_reports_market_data_ready_with_only_masked_accounts(self) -> None:
        def opener(request, *, timeout: float):
            del timeout
            if request.full_url.endswith("/info"):
                return Response({"simulation": True})
            if request.full_url.endswith("/accounts"):
                return Response([
                    {"account_type": "S", "account_id": "12345678", "broker_id": "broker", "sensitive_metadata": "private", "signed": True},
                    {"account_type": "F", "account_id": "ABCD", "username": "private", "signed": False},
                ])
            return Response({"ok": True})

        observation = SimulationConnectionMonitor(
            "http://127.0.0.1:21322", opener=opener,
        ).probe()

        self.assertEqual(observation.state, ConnectionState.MARKET_DATA_READY)
        self.assertEqual(observation.document()["accounts"], [
            {"account_type": "S", "account_label": "***78", "signed": True},
            {"account_type": "F", "account_label": "***CD", "signed": False},
        ])
        self.assertNotIn("sensitive_metadata", json.dumps(observation.document()))
        self.assertNotIn("broker", json.dumps(observation.document()))

    def test_lost_is_distinguished_from_initially_disconnected(self) -> None:
        available = True

        def opener(request, *, timeout: float):
            del timeout
            if not available:
                raise OSError("sidecar stopped")
            if request.full_url.endswith("/accounts"):
                return Response([])
            return Response({"simulation": True})

        monitor = SimulationConnectionMonitor("http://127.0.0.1:21322", opener=opener)
        self.assertEqual(monitor.probe().state, ConnectionState.AUTHENTICATED)
        available = False
        self.assertEqual(monitor.probe().state, ConnectionState.LOST)

    def test_failed_probe_is_backed_off_without_another_sidecar_request(self) -> None:
        calls = 0

        def opener(request, *, timeout: float):
            nonlocal calls
            del request, timeout
            calls += 1
            raise OSError("sidecar stopped")

        monitor = SimulationConnectionMonitor("http://127.0.0.1:21322", opener=opener)
        self.assertEqual(monitor.probe().state, ConnectionState.DISCONNECTED)
        self.assertEqual(monitor.probe().state, ConnectionState.DISCONNECTED)
        self.assertEqual(calls, 1)

    def test_sidecar_starting_status_is_reported_as_connecting(self) -> None:
        def opener(request, *, timeout: float):
            del request, timeout
            return Response({"status": "starting"})

        observation = SimulationConnectionMonitor("http://127.0.0.1:21322", opener=opener).probe()
        self.assertEqual(observation.state, ConnectionState.CONNECTING)

    def test_non_simulation_or_incomplete_read_contract_is_degraded(self) -> None:
        def opener(request, *, timeout: float):
            del timeout
            if request.full_url.endswith("/info"):
                return Response({"simulation": False})
            return Response({"ok": True})

        observation = SimulationConnectionMonitor("http://127.0.0.1:21322", opener=opener).probe()
        self.assertEqual(observation.state, ConnectionState.DEGRADED)
        self.assertEqual(observation.accounts, ())


if __name__ == "__main__":
    unittest.main()
