from __future__ import annotations

from services.autotrade_gateway.order_core import Verdict

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import select
import socket
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.autotrade_gateway.order_core import Fill, Gate, Intent, OrderCoordinator, RiskContext, RiskPosition, SQLiteIntentStore, Source, Status, StrategyRun
from services.autotrade_gateway.server import GATEWAY_API_VERSION, GatewayHandler, create_server, health_document
from services.autotrade_gateway.strategy_runtime import StrategyRuntime


def armed_coordinator(store: SQLiteIntentStore) -> OrderCoordinator:
    return OrderCoordinator(
        store,
        Gate.ARMED,
        risk_context_provider=lambda _intent: RiskContext(
            quote_price=Decimal("1000"),
            quote_at=datetime.now(timezone.utc),
        ),
    )


class ReadServiceHandler(BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []
    market_hold_started = threading.Event()
    market_hold_client_closed = threading.Event()
    market_hold_release = threading.Event()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).received.append((self.path, dict(self.headers.items()), b""))
        if self.path.startswith("/api/v1/stream/data?hold=1"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": upstream-ready\n\n")
            self.wfile.flush()
            type(self).market_hold_started.set()
            while not type(self).market_hold_release.is_set():
                readable, _, _ = select.select([self.connection], [], [], 0.05)
                if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                    type(self).market_hold_client_closed.set()
                    return
            return
        if self.path.startswith("/api/v1/stream/data"):
            payload = b"id: cursor-1\nevent: tick\ndata: {\"code\":\"2330\"}\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _receive_mutation(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        type(self).received.append((self.path, dict(self.headers.items()), body))
        payload = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = _receive_mutation
    do_PUT = _receive_mutation
    do_DELETE = _receive_mutation


class GatewayServerTest(unittest.TestCase):
    def setUp(self) -> None:
        # Unit fixtures must remain process-local even when a developer has a
        # real, schema-scoped PostgreSQL Gateway configuration in `.env`.
        # Integration coverage opts in through test_postgres_intent_store with
        # a UUID schema; sharing the configured durable journal here makes
        # tests order-dependent and can write fixture facts into it.
        self._store_environment = patch.dict(os.environ, {
            "AUTOTRADE_GATEWAY_DATABASE_URL": "",
            "AUTOTRADE_GATEWAY_DB": "",
        }, clear=False)
        self._store_environment.start()
        ReadServiceHandler.received.clear()
        ReadServiceHandler.market_hold_started.clear()
        ReadServiceHandler.market_hold_client_closed.clear()
        ReadServiceHandler.market_hold_release.clear()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), ReadServiceHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever)
        self.upstream_thread.start()
        self.server = create_server(
            port=0,
            upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}",
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        ReadServiceHandler.market_hold_release.set()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        getattr(self.server, "order_store").close()
        self.upstream.shutdown()
        self.upstream_thread.join()
        self.upstream.server_close()
        self._store_environment.stop()

    def test_health_is_sim_disarmed_and_cache_safe(self) -> None:
        with urlopen(f"{self.base}/api/v1/autotrade/health") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            document = json.load(response)
        self.assertEqual(document["trading_gate"], "DISARMED")
        self.assertEqual(document["api_version"], GATEWAY_API_VERSION)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_connection_posture_is_a_dedicated_credential_free_read(self) -> None:
        with urlopen(f"{self.base}/api/v1/autotrade/connection") as response:
            document = json.load(response)
        # Generic fixture responses do not establish a SIM session. The
        # Gateway makes only credential-free GET probes and reports DEGRADED;
        # it does not attempt a login, CA activation, or broker mutation.
        self.assertEqual(document["state"], "DEGRADED")
        self.assertEqual(
            [item[0] for item in ReadServiceHandler.received],
            ["/api/v1/health", "/api/v1/info"],
        )

    def test_position_projection_masks_account_identity_and_never_reads_sidecar(self) -> None:
        from services.autotrade_gateway.order_core import PositionSnapshot

        getattr(self.server, "order_store").record_position_snapshot(PositionSnapshot(
            snapshot_id="server-position-1", account_type="S", account_id="SIM-ACCOUNT-1234",
            symbol="2330", side="LONG", quantity="2", average_price="1000",
            market_price="1001", unrealized_pnl="2", observed_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
        ))
        with urlopen(f"{self.base}/api/v1/autotrade/positions") as response:
            document = json.load(response)

        self.assertEqual(document["items"], [{
            "snapshot_id": "server-position-1", "account_type": "S", "account_label": "SIM-ACCOUNT-1234",
            "symbol": "2330", "side": "LONG", "quantity": "2", "average_price": "1000",
            "market_price": "1001", "unrealized_pnl": "2",
            "observed_at": "2026-09-16T00:00:00+00:00",
        }])
        self.assertEqual(ReadServiceHandler.received, [])

    def test_operator_position_refresh_reads_only_sim_facts_and_persists_atomically(self) -> None:
        from services.autotrade_gateway.order_core import PositionSnapshot

        class SnapshotProvider:
            def position_snapshots(self):
                return (PositionSnapshot(
                    snapshot_id="refresh-position-1", account_type="F", account_id="SIM-FUTURE-5678",
                    symbol="TXFZ6", side="SHORT", quantity="1", average_price="22000",
                    market_price="22010", unrealized_pnl="-10",
                    observed_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                ),)

        self.server.position_snapshot_provider = SnapshotProvider()
        request = Request(f"{self.base}/api/v1/autotrade/positions/refresh", data=b"", method="POST")
        with urlopen(request) as response:
            receipt = json.load(response)
        with urlopen(f"{self.base}/api/v1/autotrade/positions") as response:
            document = json.load(response)

        self.assertEqual(receipt, {"item_count": 1, "observed_at": "2026-09-16T00:00:00+00:00"})
        self.assertEqual(document["items"][0]["account_label"], "SIM-FUTURE-5678")
        self.assertEqual(getattr(self.server, "order_coordinator").gate, Gate.DISARMED)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_client_disconnect_while_writing_a_read_response_is_silent(self) -> None:
        handler = GatewayHandler.__new__(GatewayHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError

        handler._bytes(200, b'{}', 'application/json')

        handler.send_response.assert_called_once_with(200)
        handler.wfile.write.assert_called_once_with(b'{}')

    def test_explicit_simulation_execution_wires_only_the_worker_adapter(self) -> None:
        """Opt-in may construct the loopback adapter, but must not arm or send."""
        with patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_EXECUTION": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False):
            server = create_server(
                port=0,
                upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}",
            )
        try:
            self.assertIsNotNone(getattr(server, "execution_worker"))
            self.assertEqual(getattr(server, "order_coordinator").gate, Gate.DISARMED)
            self.assertEqual(ReadServiceHandler.received, [])
        finally:
            server.server_close()
            getattr(server, "order_store").close()

    def test_explicit_simulation_risk_context_uses_discovered_account(self) -> None:
        class SnapshotProvider:
            def __init__(self, _origin: str) -> None: pass
            def simulation_stock_accounts(self):
                from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount
                return (SimulationStockAccount("B", "paper-account-1"),)
            def stock_risk_context(self, account, symbol: str, *, exchange: str = "TSE"):
                if (account.account_id, symbol, exchange) != ("paper-account-1", "2330", "TSE"):
                    raise AssertionError("unexpected simulation snapshot lookup")
                return RiskContext(
                    quote_price=Decimal("1000"), quote_at=datetime.now(timezone.utc),
                    positions={"2330": RiskPosition(0, Decimal("1000"))},
                )
        with patch("services.autotrade_gateway.server.SimulationSnapshotProvider", SnapshotProvider), patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_RISK_CONTEXT": "1", "AUTOTRADE_ENV": "SIM",
        }, clear=False):
            server = create_server(port=0, upstream_origin="http://127.0.0.1:21322")
        try:
            coordinator = getattr(server, "order_coordinator")
            coordinator.gate = Gate.ARMED
            status, verdict = coordinator.accept(Intent(Source.MANUAL_UI, "paper-account-1", "2330", "BUY", 1, "1000"))
            self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
            fixture = getattr(server, "strategy_runtime")._fixture_intent("test", datetime.now(timezone.utc))
            self.assertEqual(fixture.account_id, "paper-account-1")
            self.assertEqual(fixture.broker_request["body"]["stock_order"]["account"]["broker_id"], "B")
            self.assertEqual(getattr(server, "execution_worker"), None)
        finally:
            server.server_close()
            getattr(server, "order_store").close()

    def test_simulation_reconciliation_requires_verified_sim_account_and_stays_read_only(self) -> None:
        class SnapshotProvider:
            def __init__(self, _origin: str) -> None: pass

            def simulation_stock_accounts(self):
                from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount

                return (SimulationStockAccount("B", "paper-account-1"),)

            def stock_risk_context(self, _account, _symbol: str, *, exchange: str = "TSE"):
                return RiskContext(Decimal("1000"), datetime.now(timezone.utc))

        class TradeProvider:
            constructed_for: list[str] = []

            def __init__(self, origin: str) -> None:
                self.constructed_for.append(origin)

            def terminal_facts(self, _account):
                raise AssertionError("server construction must not query broker trades")

        with patch("services.autotrade_gateway.server.SimulationSnapshotProvider", SnapshotProvider), patch(
            "services.autotrade_gateway.server.SimulationTradeProvider", TradeProvider
        ), patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_RISK_CONTEXT": "1",
            "AUTOTRADE_ENABLE_SIMULATION_RECONCILIATION": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False):
            server = create_server(port=0, upstream_origin="http://127.0.0.1:21322")
        try:
            self.assertIsNotNone(getattr(server, "reconciliation_refresher"))
            self.assertEqual(getattr(server, "order_coordinator").gate, Gate.DISARMED)
            self.assertEqual(TradeProvider.constructed_for, ["http://127.0.0.1:21322"])
            self.assertEqual(ReadServiceHandler.received, [])
        finally:
            server.server_close()
            getattr(server, "order_store").close()

    def test_simulation_reconciliation_refuses_live_or_unverified_configuration(self) -> None:
        with patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_RECONCILIATION": "1",
            "AUTOTRADE_ENV": "LIVE",
        }, clear=False), self.assertRaisesRegex(RuntimeError, "SIM only"):
            create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")
        with patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_RECONCILIATION": "1",
            "AUTOTRADE_ENV": "SIM",
            "AUTOTRADE_ENABLE_SIMULATION_RISK_CONTEXT": "",
        }, clear=False), self.assertRaisesRegex(RuntimeError, "verified risk context"):
            create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")

    def test_observer_mode_rejects_every_control_and_order_mutation_before_parsing(self) -> None:
        with patch.dict(os.environ, {"AUTOTRADE_GATEWAY_MODE": "OBSERVER"}, clear=False):
            server = create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(f"{base}/api/v1/autotrade/health") as response:
                self.assertEqual(json.load(response)["capability_mode"], "OBSERVER")
            with urlopen(f"{base}/api/v1/autotrade/overview") as response:
                self.assertEqual(json.load(response)["health"]["capability_mode"], "OBSERVER")
            with urlopen(Request(
                f"{base}/api/v1/order/order_deal_records",
                data=b'{"account_type":"S","broker_id":"fixture","account_id":"fixture"}',
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer browser-secret",
                    "Cookie": "session=browser-secret",
                },
                method="POST",
            )) as response:
                self.assertEqual(response.status, 200)
            path, headers, _body = ReadServiceHandler.received[-1]
            self.assertEqual(path, "/api/v1/order/order_deal_records")
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            with urlopen(Request(
                f"{base}/api/v1/stream/data/order_event?account_type=S",
                headers={
                    "Accept": "text/event-stream",
                    "Authorization": "Bearer browser-secret",
                    "Cookie": "session=browser-secret",
                },
            )) as response:
                self.assertEqual(response.headers.get_content_type(), "text/event-stream")
                self.assertIn(b"event: tick", response.read())
            path, headers, _body = ReadServiceHandler.received[-1]
            self.assertEqual(path, "/api/v1/stream/data/order_event?account_type=S")
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            for path in (
                "/api/v1/autotrade/intents",
                "/api/v1/autotrade/kill-switch",
                "/api/v1/autotrade/reconciliation/not-a-real-intent/refresh",
                "/api/v1/order/place_order",
                "/api/v1/watchlist",
            ):
                with self.subTest(path=path), self.assertRaises(HTTPError) as denied:
                    urlopen(Request(f"{base}{path}", data=b"not-json", method="POST"))
                self.assertEqual(denied.exception.code, 403)
            self.assertEqual(getattr(server, "order_store").observation_orders(), [])
            self.assertEqual(len(ReadServiceHandler.received), 2)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            getattr(server, "order_store").close()

    def test_observer_mode_refuses_execution_or_strategy_control_configuration(self) -> None:
        with patch.dict(os.environ, {
            "AUTOTRADE_GATEWAY_MODE": "OBSERVER",
            "AUTOTRADE_ENABLE_SIMULATION_EXECUTION": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False), self.assertRaisesRegex(RuntimeError, "observer mode"):
            create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")

    def test_targeted_simulation_reconciliation_refresh_is_idempotent_and_never_dispatches(self) -> None:
        class SnapshotProvider:
            def __init__(self, _origin: str) -> None: pass
            def simulation_stock_accounts(self):
                from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount
                return (SimulationStockAccount("B", "paper-account-1"),)
            def stock_risk_context(self, _account, _symbol: str, *, exchange: str = "TSE"):
                return RiskContext(Decimal("1000"), datetime.now(timezone.utc))

        class TradeProvider:
            calls = 0
            def __init__(self, _origin: str) -> None: pass
            def terminal_facts(self, _account):
                type(self).calls += 1
                return (("broker-target", Status.FILLED), ("broker-other", Status.CANCELLED))

        with patch("services.autotrade_gateway.server.SimulationSnapshotProvider", SnapshotProvider), patch(
            "services.autotrade_gateway.server.SimulationTradeProvider", TradeProvider,
        ), patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_RISK_CONTEXT": "1",
            "AUTOTRADE_ENABLE_SIMULATION_RECONCILIATION": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False):
            server = create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            store = getattr(server, "order_store")
            target = Intent(Source.MANUAL_UI, "paper-account-1", "2330", "BUY", 1, "1000")
            other = Intent(Source.MANUAL_UI, "paper-account-1", "2317", "BUY", 1, "1000")
            coordinator = getattr(server, "order_coordinator")
            coordinator.gate = Gate.ARMED
            coordinator.accept(target)
            store.persist(other, Verdict(True))
            store.mark_pending_submit(target.intent_id, "broker-target")
            store.mark_pending_submit(other.intent_id, "broker-other")
            self.assertIn(store.claim_outbox(), {target.intent_id, other.intent_id})
            self.assertIn(store.claim_outbox(), {target.intent_id, other.intent_id})
            store.mark_delivery_unknown(target.intent_id, "timeout")
            store.mark_delivery_unknown(other.intent_id, "timeout")
            base = f"http://127.0.0.1:{server.server_port}"
            def request(reason: str = "operator verification"):
                return Request(
                    f"{base}/api/v1/autotrade/reconciliation/{target.intent_id}/refresh",
                    data=json.dumps({"reason": reason}).encode(), method="POST",
                    headers={"Content-Type": "application/json", "Idempotency-Key": "refresh-target-1"},
                )
            with urlopen(request()) as response:
                first = json.load(response)
            with urlopen(request()) as response:
                replay = json.load(response)
            self.assertTrue(first["applied"])
            self.assertFalse(replay["applied"])
            self.assertEqual((first["resolved_intents"], first["status"]), (1, "FILLED"))
            self.assertEqual(TradeProvider.calls, 1)
            self.assertEqual(store.status(target.intent_id), Status.FILLED)
            self.assertEqual(store.status(other.intent_id), Status.UNKNOWN)
            self.assertEqual(store.outbox_state(other.intent_id), "UNKNOWN")
            self.assertEqual(getattr(server, "execution_worker"), None)
            with self.assertRaises(HTTPError) as collision:
                urlopen(request("different reason"))
            self.assertEqual(collision.exception.code, 409)
            self.assertEqual(TradeProvider.calls, 1)
            self.assertEqual(ReadServiceHandler.received, [])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            getattr(server, "order_store").close()

    def test_reconciliation_refresh_timeout_stays_pending_without_a_second_broker_read(self) -> None:
        class SnapshotProvider:
            def __init__(self, _origin: str) -> None: pass
            def simulation_stock_accounts(self):
                from services.autotrade_gateway.simulation_snapshot import SimulationStockAccount
                return (SimulationStockAccount("B", "paper-account-1"),)
            def stock_risk_context(self, _account, _symbol: str, *, exchange: str = "TSE"):
                return RiskContext(Decimal("1000"), datetime.now(timezone.utc))

        class TimeoutTradeProvider:
            calls = 0
            def __init__(self, _origin: str) -> None: pass
            def terminal_facts(self, _account):
                type(self).calls += 1
                raise TimeoutError("sidecar response uncertain")

        with patch("services.autotrade_gateway.server.SimulationSnapshotProvider", SnapshotProvider), patch(
            "services.autotrade_gateway.server.SimulationTradeProvider", TimeoutTradeProvider,
        ), patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_RISK_CONTEXT": "1",
            "AUTOTRADE_ENABLE_SIMULATION_RECONCILIATION": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False):
            server = create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            store = getattr(server, "order_store")
            intent = Intent(Source.MANUAL_UI, "paper-account-1", "2330", "BUY", 1, "1000")
            coordinator = getattr(server, "order_coordinator")
            coordinator.gate = Gate.ARMED
            coordinator.accept(intent)
            self.assertEqual(store.claim_outbox(), intent.intent_id)
            store.mark_pending_submit(intent.intent_id, "broker-timeout")
            store.mark_delivery_unknown(intent.intent_id, "dispatch_uncertain:TimeoutError")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/autotrade/reconciliation/{intent.intent_id}/refresh",
                data=b'{"reason":"operator timeout drill"}', method="POST",
                headers={"Content-Type": "application/json", "Idempotency-Key": "refresh-timeout-1"},
            )
            for _ in range(2):
                with self.assertRaises(HTTPError) as unavailable:
                    urlopen(request)
                self.assertEqual(unavailable.exception.code, 503)
            self.assertEqual(TimeoutTradeProvider.calls, 1)
            self.assertEqual(store.status(intent.intent_id), Status.RECONCILING)
            # A bound broker identity remains non-claimable DISPATCHED; the
            # UNKNOWN intent still cannot be re-enqueued or dispatched.
            self.assertEqual(store.outbox_state(intent.intent_id), "DISPATCHED")
            self.assertEqual(store.reconciliation_refresh_receipt("refresh-timeout-1")["state"], "PENDING")
            self.assertEqual(getattr(server, "execution_worker"), None)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            getattr(server, "order_store").close()

    def test_execution_adapter_refuses_non_simulation_environment(self) -> None:
        with patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_SIMULATION_EXECUTION": "1",
            "AUTOTRADE_ENV": "LIVE",
        }, clear=False), self.assertRaisesRegex(RuntimeError, "SIM only"):
            create_server(
                port=0,
                upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}",
            )

    def test_broker_callback_refuses_non_simulation_environment(self) -> None:
        with patch.dict(os.environ, {
            "AUTOTRADE_BROKER_CALLBACK_SECRET": "test-only-callback-secret",
            "AUTOTRADE_ENV": "LIVE",
        }, clear=False), self.assertRaisesRegex(RuntimeError, "SIM only"):
            create_server(
                port=0,
                upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}",
            )

    def test_reference_strategy_signals_require_verified_risk_context(self) -> None:
        with patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_REFERENCE_STRATEGY_SIGNALS": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False), self.assertRaisesRegex(RuntimeError, "risk context"):
            create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")

    def test_explicit_strategy_lifecycle_control_is_idempotent_and_broker_free(self) -> None:
        with patch.dict(os.environ, {
            "AUTOTRADE_ENABLE_STRATEGY_LIFECYCLE": "1",
            "AUTOTRADE_ENV": "SIM",
        }, clear=False):
            server = create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            request = Request(
                f"{base}/api/v1/autotrade/strategies/reference-fixture/start",
                data=b'{"reason":"operator test"}',
                headers={"Content-Type": "application/json", "Idempotency-Key": "strategy-start-1"}, method="POST",
            )
            with urlopen(request) as response:
                first = json.load(response)
            with urlopen(request) as response:
                replay = json.load(response)
            self.assertEqual(first["state"], "RUNNING")
            self.assertTrue(first["applied"])
            self.assertFalse(replay["applied"])
            self.assertEqual(first["run_id"], replay["run_id"])
            self.assertEqual(getattr(server, "order_coordinator").gate, Gate.DISARMED)
            self.assertEqual(getattr(server, "order_store").observation_orders(), [])
            self.assertEqual(ReadServiceHandler.received, [])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            getattr(server, "order_store").close()

    def test_kill_switch_requires_a_reason_and_only_tightens_the_gate(self) -> None:
        with self.assertRaises(HTTPError) as missing_key:
            urlopen(Request(
                f"{self.base}/api/v1/autotrade/kill-switch", data=b'{"reason":"operator test"}', method="POST",
            ))
        self.assertEqual(missing_key.exception.code, 400)

        request = Request(
            f"{self.base}/api/v1/autotrade/kill-switch", data=b'{"reason":"operator test"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "kill-test-1"}, method="POST",
        )
        with urlopen(request) as response:
            document = json.load(response)
        self.assertEqual(document, {"trading_gate": "KILLED", "reason": "operator test", "applied": True})
        with urlopen(f"{self.base}/api/v1/autotrade/health") as response:
            self.assertEqual(json.load(response)["trading_gate"], "KILLED")
        self.assertEqual(getattr(self.server, "order_store").observation_audit_events()[0]["event_type"], "KILL_SWITCH_APPLIED")
        self.assertEqual(ReadServiceHandler.received, [])

    def test_health_counts_only_runtime_enabled_strategies(self) -> None:
        class Runtime:
            def observations(self) -> list[dict[str, object]]:
                return [{"enabled": True}, {"enabled": False}, {"enabled": "true"}]

        self.assertEqual(health_document(Runtime()), {
            "api_version": GATEWAY_API_VERSION, "environment": "SIM", "runtime_mode": "OBSERVE", "trading_gate": "DISARMED", "strategy_count": 3,
            "enabled_strategy_count": 1, "gateway_status": "READY", "capability_mode": "OPERATOR",
        })

    def test_read_proxy_fixed_paths_cannot_match_longer_unlisted_routes(self) -> None:
        for path in (
            "/api/v1/auth/accounts-extra",
            "/api/v1/auth/ca_expiretime-extra",
            "/api/v1/auth/usage-extra",
            "/api/v1/order/trades-extra",
            "/api/v1/order/combotrades-extra",
            "/api/v1/order/stock_reserve_summary-extra",
            "/api/v1/order/stock_reserve_detail-extra",
            "/api/v1/order/earmarking_detail-extra",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{self.base}{path}")
                self.assertEqual(raised.exception.code, 404)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_read_proxy_forwards_each_fixed_contract_without_browser_credentials(self) -> None:
        paths = (
            "/api/v1/auth/accounts",
            "/api/v1/auth/ca_expiretime?person_id=masked",
            "/api/v1/auth/usage",
            "/api/v1/order/trades",
            "/api/v1/order/combotrades",
            "/api/v1/order/stock_reserve_summary",
            "/api/v1/order/stock_reserve_detail",
            "/api/v1/order/earmarking_detail",
        )
        for path in paths:
            with self.subTest(path=path):
                request = Request(
                    f"{self.base}{path}",
                    headers={
                        "Authorization": "Bearer browser-secret",
                        "Cookie": "session=browser-secret",
                    },
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                received_path, headers, _body = ReadServiceHandler.received[-1]
                self.assertEqual(received_path, path)
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Cookie", headers)

    def test_web_get_read_contract_inventory_stays_gateway_reachable(self) -> None:
        """Cover each upstream client GET family with a concrete safe shape.

        Dynamic contract identifiers are individually validated rather than
        giving `/data/` or `/monitor/` a broad prefix. These representatives
        come directly from `src/lib/shioaji.ts` and its consumers, so a future
        allowlist tightening cannot quietly send the Web back to a sidecar.
        """
        paths = (
            "/api/v1/health",
            "/api/v1/info",
            "/api/v1/auth/accounts",
            "/api/v1/auth/ca_expiretime?person_id=masked",
            "/api/v1/data/contracts?security_type=STK&region=TW",
            "/api/v1/data/contracts/2330?security_type=STK&region=TW",
            "/api/v1/data/contracts/2330/info?security_type=STK&region=TW",
            "/api/v1/data/contracts/futures?region=TW",
            "/api/v1/data/contracts/futures/roots?region=TW",
            "/api/v1/data/contracts/options?root=TXO&region=TW",
            "/api/v1/data/contracts/options/roots?region=TW",
            "/api/v1/data/contracts/warrants?underlying_code=2330&region=TW",
            "/api/v1/data/contracts/warrants/underlyings?region=TW&include_name=true",
            "/api/v1/data/contracts/tick-bands/TXF?security_type=FUT&region=TW",
            "/api/v1/data/contracts/combo/futures?root=TXF&region=TW",
            "/api/v1/data/regulatory_punish",
            "/api/v1/monitor/metrics",
            "/api/v1/monitor/subscriptions",
            "/api/v1/watchlist",
        )
        for path in paths:
            with self.subTest(path=path):
                request = Request(
                    f"{self.base}{path}",
                    headers={"Authorization": "Bearer browser-secret", "Cookie": "session=browser-secret"},
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                received_path, headers, _body = ReadServiceHandler.received[-1]
                self.assertEqual(received_path, path)
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Cookie", headers)

    def test_futures_combo_contract_resolver_is_narrowly_allowlisted_without_credentials(self) -> None:
        body = json.dumps({"legs": [
            {"security_type": "FUT", "region": "TW", "exchange": "TAIFEX", "code": "TXFZ6", "target_code": None},
            {"security_type": "FUT", "region": "TW", "exchange": "TAIFEX", "code": "TXFY6", "target_code": None},
        ]}).encode()
        request = Request(
            f"{self.base}/api/v1/data/contracts/combo", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer browser-secret",
                "Cookie": "session=browser-secret",
            },
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        received_path, headers, received_body = ReadServiceHandler.received[-1]
        self.assertEqual(received_path, "/api/v1/data/contracts/combo")
        self.assertEqual(received_body, body)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_futures_combo_contract_resolver_rejects_non_futures_or_extra_input(self) -> None:
        for body in (
            {"legs": []},
            {"legs": [{"security_type": "OPT", "region": "TW", "exchange": "TAIFEX", "code": "TXO", "target_code": None}] * 2},
            {"legs": [{"security_type": "FUT", "region": "TW", "exchange": "TAIFEX", "code": "TXFZ6", "target_code": None, "side": "BUY"}] * 2},
        ):
            with self.subTest(body=body), self.assertRaises(HTTPError) as raised:
                urlopen(Request(
                    f"{self.base}/api/v1/data/contracts/combo", data=json.dumps(body).encode(), method="POST",
                    headers={"Content-Type": "application/json"},
                ))
            self.assertEqual(raised.exception.code, 400)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_overview_never_invents_broker_activity(self) -> None:
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            overview = json.load(response)
        self.assertEqual(overview["health"]["environment"], "SIM")
        self.assertEqual(overview["strategies"], [{
            "strategy_id": "reference-fixture", "version": "v1",
            "config_checksum": "reference-fixture-v1", "state": "DISABLED",
            "enabled": False, "allowed_symbols": ["2330"],
            "lifecycle_enabled": False,
            "last_event_id": None, "last_intent_id": None, "last_status": None,
        }])
        self.assertEqual(overview["risk"]["limits_source"], "gateway_static_simulation_v1")
        self.assertEqual(overview["risk"]["limits"]["max_order_notional"], "5000000")
        self.assertEqual(overview["risk"]["limits"]["max_gross_exposure"], "10000000")
        self.assertEqual(overview["risk"]["limits"]["max_quote_age_seconds"], 5)
        self.assertEqual(overview["orders"], [])
        self.assertEqual(overview["reconciliation"], [])

    def test_strategy_and_signal_read_routes_project_only_durable_or_disabled_facts(self) -> None:
        with urlopen(f"{self.base}/api/v1/autotrade/strategies") as response:
            strategies = json.load(response)
        self.assertEqual(strategies["items"][0]["strategy_id"], "reference-fixture")
        self.assertFalse(strategies["items"][0]["enabled"])
        with urlopen(f"{self.base}/api/v1/autotrade/strategies/reference-fixture") as response:
            strategy = json.load(response)
        self.assertEqual(strategy["strategy_id"], "reference-fixture")
        self.assertEqual(strategy["state"], "DISABLED")
        with urlopen(f"{self.base}/api/v1/autotrade/signals") as response:
            self.assertEqual(json.load(response), {"items": []})
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base}/api/v1/autotrade/strategies/not-a-strategy")
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_strategy_lifecycle_mutations_are_explicitly_denied_without_sidecar_access(self) -> None:
        for action in ("start", "pause", "resume", "stop"):
            with self.subTest(action=action), self.assertRaises(HTTPError) as raised:
                urlopen(Request(
                    f"{self.base}/api/v1/autotrade/strategies/reference-fixture/{action}",
                    data=b"{}", method="POST",
                ))
            self.assertEqual(raised.exception.code, 403)
            self.assertEqual(json.load(raised.exception)["message"], "strategy runtime is disabled")
        self.assertEqual(getattr(self.server, "order_store").observation_orders(), [])
        self.assertEqual(ReadServiceHandler.received, [])

    def test_signals_route_projects_a_durable_fixture_signal_without_a_sidecar_call(self) -> None:
        store = getattr(self.server, "order_store")
        runtime = StrategyRuntime(OrderCoordinator(store, Gate.DISARMED), allow_reference_fixture=True)
        self.assertIsNotNone(runtime.process_fixture_event({
            "reference_signal": True,
            "event_id": "durable-signal-read",
            "observed_at": "2026-09-15T00:00:00+00:00",
        }))
        self.server.strategy_runtime = runtime  # type: ignore[attr-defined]

        with urlopen(f"{self.base}/api/v1/autotrade/signals") as response:
            signals = json.load(response)["items"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["event_id"], "durable-signal-read")
        self.assertEqual(signals[0]["status"], "RISK_REJECTED")
        self.assertEqual(signals[0]["risk_reasons"], ["trading_gate_not_armed"])
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            self.assertEqual(json.load(response)["signals"], signals)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_intents_endpoint_returns_durable_read_only_observations(self) -> None:
        body = json.dumps({
            "source": "MANUAL_UI", "account_id": "SIM-001", "symbol": "2330",
            "side": "BUY", "quantity": 1, "limit_price": "1000",
        }).encode()
        with self.assertRaises(HTTPError):
            urlopen(Request(f"{self.base}/api/v1/autotrade/intents", data=body, method="POST"))
        with urlopen(f"{self.base}/api/v1/autotrade/intents") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["source"], "MANUAL_UI")
        self.assertEqual(document["items"][0]["status"], "RISK_REJECTED")
        self.assertIn("trading_gate_not_armed", document["items"][0]["risk_reasons"])

    def test_individual_intent_and_order_reads_share_the_durable_projection(self) -> None:
        body = json.dumps({
            "source": "MANUAL_UI", "account_id": "SIM-001", "symbol": "2330",
            "side": "BUY", "quantity": 1, "limit_price": "1000",
        }).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/autotrade/intents", data=body, method="POST"))
        receipt = json.load(raised.exception)
        intent_id = receipt["intent_id"]
        for resource in ("intents", "orders"):
            with self.subTest(resource=resource), urlopen(
                f"{self.base}/api/v1/autotrade/{resource}/{intent_id}",
            ) as response:
                document = json.load(response)
            self.assertEqual(document["intent_id"], intent_id)
            self.assertEqual(document["correlation_id"], receipt["correlation_id"])
            self.assertEqual(document["status"], "RISK_REJECTED")
        with self.assertRaises(HTTPError) as missing:
            urlopen(f"{self.base}/api/v1/autotrade/orders/not-an-intent")
        self.assertEqual(missing.exception.code, 404)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_orders_endpoint_projects_durable_broker_identity_without_a_sidecar_call(self) -> None:
        store = getattr(self.server, "order_store")
        intent = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "fixture", "run-1")
        armed_coordinator(store).accept(intent)

        class MockDispatcher:
            def dispatch(self, _intent_id: str) -> str:
                return "MOCK-ORDER-READ-1"

        from services.autotrade_gateway.worker import ExecutionWorker
        self.assertTrue(ExecutionWorker(store, MockDispatcher()).dispatch_once())
        with urlopen(f"{self.base}/api/v1/autotrade/orders") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["intent_id"], intent.intent_id)
        self.assertEqual(document["items"][0]["broker_order_id"], "MOCK-ORDER-READ-1")
        self.assertEqual(document["items"][0]["source"], "STRATEGY")
        self.assertEqual(document["items"][0]["symbol"], "2330")
        self.assertEqual(document["items"][0]["side"], "BUY")
        self.assertEqual(document["items"][0]["quantity"], 1)
        self.assertEqual(document["items"][0]["limit_price"], "1000")
        self.assertEqual(ReadServiceHandler.received, [])

    def test_internal_terminal_callback_requires_secret_and_only_advances_bound_order(self) -> None:
        store = getattr(self.server, "order_store")
        intent = Intent(
            Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000",
            strategy_id="callback-fixture", run_id="callback-run",
        )
        status, verdict = armed_coordinator(store).accept(intent)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))

        class MockDispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "MOCK-CALLBACK-1"

        from services.autotrade_gateway.worker import ExecutionWorker
        self.assertTrue(ExecutionWorker(store, MockDispatcher()).dispatch_once())
        self.server.broker_callback_secret = "test-callback-secret"  # type: ignore[attr-defined]
        body = json.dumps({
            "intent_id": intent.intent_id,
            "broker_order_id": "MOCK-CALLBACK-1",
            "status": "FILLED",
        }).encode()
        with self.assertRaises(HTTPError) as denied:
            urlopen(Request(
                f"{self.base}/api/v1/autotrade/internal/broker-terminal", data=body,
                headers={"Content-Type": "application/json", "X-Autotrade-Callback-Secret": "wrong"},
                method="POST",
            ))
        self.assertEqual(denied.exception.code, 403)
        with urlopen(Request(
            f"{self.base}/api/v1/autotrade/internal/broker-terminal", data=body,
            headers={"Content-Type": "application/json", "X-Autotrade-Callback-Secret": "test-callback-secret"},
            method="POST",
        )) as response:
            self.assertEqual(json.load(response), {"intent_id": intent.intent_id, "status": "FILLED"})
        self.assertEqual(store.status(intent.intent_id), Status.FILLED)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_fills_endpoint_and_performance_use_only_durable_broker_facts(self) -> None:
        store = getattr(self.server, "order_store")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(store).accept(intent)

        class AcceptedDispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "MOCK-FILL-READ-1"

        from services.autotrade_gateway.worker import ExecutionWorker
        self.assertTrue(ExecutionWorker(store, AcceptedDispatcher()).dispatch_once())
        store.record_fill(Fill(
            fill_id="mock-fill-read-1", intent_id=intent.intent_id,
            broker_order_id="MOCK-FILL-READ-1", quantity=1, price="1001",
            filled_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        ))
        with urlopen(f"{self.base}/api/v1/autotrade/fills") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["fill_id"], "mock-fill-read-1")
        self.assertEqual(document["items"][0]["broker_order_id"], "MOCK-FILL-READ-1")
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            overview = json.load(response)
        self.assertEqual(overview["performance"]["filled_order_count"], 1)
        self.assertEqual(overview["orders"][0]["filled_quantity"], 1)
        self.assertEqual(overview["orders"][0]["fill_count"], 1)
        self.assertEqual(overview["risk"]["usage"], {
            "intent_count": 1, "confirmed_fill_count": 1,
            "confirmed_filled_quantity": 1, "reconciliation_count": 0,
            "authoritative_remaining_available": False,
        })
        self.assertEqual(ReadServiceHandler.received, [])

    def test_runs_endpoint_projects_only_durable_strategy_intents(self) -> None:
        store = getattr(self.server, "order_store")
        strategy_intent = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "fixture", "run-durable")
        manual_intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        coordinator = OrderCoordinator(store, Gate.DISARMED)
        coordinator.accept(strategy_intent)
        coordinator.accept(manual_intent)
        recorded_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
        store.record_strategy_run(StrategyRun(
            run_id="run-durable", strategy_id="fixture", version="test-v1",
            config_checksum="test-checksum", state="FIXTURE_COMPLETED",
            last_intent_id=strategy_intent.intent_id, last_status=Status.RISK_REJECTED,
            intent_count=1, started_at=recorded_at, heartbeat_at=recorded_at,
            ended_at=recorded_at, updated_at=recorded_at,
        ))
        with urlopen(f"{self.base}/api/v1/autotrade/runs") as response:
            document = json.load(response)
        self.assertEqual(document["items"], [
            {"run_id": "run-durable", "strategy_id": "fixture", "version": "test-v1",
             "config_checksum": "test-checksum", "state": "FIXTURE_COMPLETED",
             "last_intent_id": strategy_intent.intent_id, "last_status": "RISK_REJECTED",
             "intent_count": 1, "started_at": recorded_at.isoformat(),
             "heartbeat_at": recorded_at.isoformat(), "ended_at": recorded_at.isoformat(),
             "updated_at": recorded_at.isoformat()},
        ])
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            overview = json.load(response)
        self.assertEqual(overview["performance"]["run_count"], 1)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_decision_timeline_projects_durable_risk_verdict_without_a_sidecar_call(self) -> None:
        body = json.dumps({
            "source": "MANUAL_UI", "account_id": "SIM-001", "symbol": "2330",
            "side": "BUY", "quantity": 1, "limit_price": "1000",
        }).encode()
        with self.assertRaises(HTTPError):
            urlopen(Request(f"{self.base}/api/v1/autotrade/intents", data=body, method="POST"))
        with urlopen(f"{self.base}/api/v1/autotrade/decisions") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["source"], "MANUAL_UI")
        self.assertEqual(document["items"][0]["status"], "RISK_REJECTED")
        self.assertIn("trading_gate_not_armed", document["items"][0]["risk_reasons"])
        self.assertFalse(document["items"][0]["allowed"])
        self.assertIsInstance(document["items"][0]["decided_at"], str)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_audit_events_endpoint_projects_durable_lifecycle_facts_without_a_sidecar_call(self) -> None:
        body = json.dumps({
            "source": "MANUAL_UI", "account_id": "SIM-001", "symbol": "2330",
            "side": "BUY", "quantity": 1, "limit_price": "1000",
        }).encode()
        with self.assertRaises(HTTPError):
            urlopen(Request(f"{self.base}/api/v1/autotrade/intents", data=body, method="POST"))
        with urlopen(f"{self.base}/api/v1/autotrade/audit-events") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["event_type"], "INTENT_EVALUATED")
        self.assertEqual(document["items"][0]["details"]["status"], "RISK_REJECTED")
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            overview = json.load(response)
        self.assertEqual(overview["audit_events"][0]["event_type"], "INTENT_EVALUATED")
        self.assertEqual(ReadServiceHandler.received, [])

    def test_reconciliation_endpoint_exposes_unknown_without_a_resend(self) -> None:
        store = getattr(self.server, "order_store")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(store).accept(intent)
        store.recover()
        with urlopen(f"{self.base}/api/v1/autotrade/reconciliation") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["intent_id"], intent.intent_id)
        self.assertEqual(document["items"][0]["status"], "UNKNOWN")
        self.assertEqual(document["items"][0]["reconciliation_case"]["reason"], "restart_recovery")
        self.assertEqual(store.outbox_state(intent.intent_id), "UNKNOWN")
        store.transition(intent.intent_id, Status.RECONCILING)
        with urlopen(f"{self.base}/api/v1/autotrade/reconciliation") as response:
            reconciling = json.load(response)
        self.assertEqual(reconciling["items"][0]["status"], "RECONCILING")
        self.assertEqual(reconciling["items"][0]["reconciliation_case"]["state"], "RECONCILING")

    def test_autotrade_events_replay_durable_observations_without_sidecar_access(self) -> None:
        store = getattr(self.server, "order_store")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(store, Gate.DISARMED).accept(intent)
        request = Request(
            f"{self.base}/api/v1/autotrade/events",
            headers={"Accept": "text/event-stream"},
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            self.assertEqual(response.readline().decode(), "retry: 3000\n")
            self.assertEqual(response.readline().decode(), "\n")
            event_id = response.readline().decode().removeprefix("id: ").strip()
            self.assertEqual(event_id, f"{intent.intent_id}:{store.observation_orders()[0]['updated_at']}")
            self.assertEqual(response.readline().decode(), "event: autotrade-order\n")
            payload = response.readline().decode().removeprefix("data: ")
            self.assertEqual(json.loads(payload)["intent_id"], intent.intent_id)
            self.assertEqual(response.readline().decode(), "\n")
        self.assertEqual(ReadServiceHandler.received, [])

    def test_autotrade_events_resume_after_the_last_seen_durable_observation(self) -> None:
        store = getattr(self.server, "order_store")
        first = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        second = Intent(Source.MANUAL_UI, "SIM-001", "2317", "BUY", 1, "1000")
        coordinator = OrderCoordinator(store, Gate.DISARMED)
        coordinator.accept(first)
        coordinator.accept(second)
        first_observation = next(
            item for item in store.observation_orders() if item["intent_id"] == first.intent_id
        )
        request = Request(
            f"{self.base}/api/v1/autotrade/events",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": f"{first.intent_id}:{first_observation['updated_at']}",
            },
        )
        with urlopen(request, timeout=2) as response:
            response.readline()
            response.readline()
            response.readline()
            response.readline()
            payload = response.readline().decode().removeprefix("data: ")
            self.assertEqual(json.loads(payload)["intent_id"], second.intent_id)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_autotrade_events_notify_connected_observers_after_a_durable_receipt(self) -> None:
        request = Request(
            f"{self.base}/api/v1/autotrade/events",
            headers={"Accept": "text/event-stream"},
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.readline().decode(), "retry: 3000\n")
            self.assertEqual(response.readline().decode(), "\n")
            body = json.dumps({
                "source": "MANUAL_UI", "account_id": "SIM-001", "symbol": "2330",
                "side": "BUY", "quantity": 1, "limit_price": "1000",
            }).encode()
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(
                    f"{self.base}/api/v1/autotrade/intents", data=body, method="POST",
                    headers={"Idempotency-Key": "sse-observer-notification"},
                ))
            self.assertEqual(raised.exception.code, 409)
            receipt = json.load(raised.exception)
            response.readline()
            self.assertEqual(response.readline().decode(), "event: autotrade-order\n")
            payload = response.readline().decode().removeprefix("data: ")
            self.assertEqual(json.loads(payload)["intent_id"], receipt["intent_id"])
        self.assertEqual(ReadServiceHandler.received, [])

    def test_autotrade_event_disconnect_releases_its_listener(self) -> None:
        request = Request(
            f"{self.base}/api/v1/autotrade/events",
            headers={"Accept": "text/event-stream"},
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.readline().decode(), "retry: 3000\n")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not getattr(self.server, "autotrade_event_hub")._listeners:
                break
            time.sleep(0.05)
        self.assertEqual(getattr(self.server, "autotrade_event_hub")._listeners, set())
        self.assertEqual(ReadServiceHandler.received, [])

    def test_gateway_startup_recovers_persisted_pending_delivery_to_unknown(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "restart-recovery.db"
            previous_store = SQLiteIntentStore(database_path)
            intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
            armed_coordinator(previous_store).accept(intent)
            self.assertEqual(previous_store.outbox_state(intent.intent_id), "PENDING")
            previous_store.close()

            with patch.dict(os.environ, {
                "AUTOTRADE_GATEWAY_DB": str(database_path),
                "AUTOTRADE_GATEWAY_DATABASE_URL": "",
            }):
                restarted = create_server(port=0, upstream_origin=f"http://127.0.0.1:{self.upstream.server_port}")
            try:
                recovered_store = getattr(restarted, "order_store")
                self.assertEqual(recovered_store.status(intent.intent_id), Status.UNKNOWN)
                self.assertEqual(recovered_store.outbox_state(intent.intent_id), "UNKNOWN")
            finally:
                restarted.server_close()
                getattr(restarted, "order_store").close()

    def test_every_mutation_is_denied_before_broker_access(self) -> None:
        for path in ("/api/v1/order/reserve_stock", "/api/v1/paper/update_status"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                urlopen(Request(f"{self.base}{path}", method="POST"))
            self.assertEqual(raised.exception.code, 403)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_cancel_order_is_audited_but_cannot_dispatch_without_target_account_lookup(self) -> None:
        body = json.dumps({"trade_id": "BROKER-ORDER-1"}).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/cancel_order", data=body, method="POST"))
        self.assertEqual(raised.exception.code, 409)
        receipt = json.load(raised.exception)
        self.assertEqual(receipt["status"], "RISK_REJECTED")
        self.assertIn("trading_gate_not_armed", receipt["risk_reasons"])
        self.assertIn("target_order_account_unverified", receipt["risk_reasons"])
        self.assertIsNone(getattr(self.server, "order_store").outbox_state(receipt["intent_id"]))
        self.assertEqual(ReadServiceHandler.received, [])

    def test_bound_nonterminal_order_allows_only_a_durable_follow_up_cancel_intent(self) -> None:
        store = getattr(self.server, "order_store")
        original = Intent(
            Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000",
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {"contract": {"code": "2330"}},
            },
        )
        armed_coordinator(store).accept(original)
        store.mark_pending_submit(original.intent_id, "BOUND-SERVER-CANCEL-1")
        getattr(self.server, "order_coordinator").gate = Gate.ARMED

        request = Request(
            f"{self.base}/api/v1/order/cancel_order",
            data=b'{"trade_id":"BOUND-SERVER-CANCEL-1"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "bound-cancel-1"},
            method="POST",
        )
        with urlopen(request) as response:
            receipt = json.load(response)

        self.assertEqual(receipt["status"], "APPROVED")
        follow_up = store.load_intent(receipt["intent_id"])
        self.assertEqual(follow_up.account_id, "SIM-001")
        self.assertTrue(follow_up.target_order_verified)
        self.assertEqual(follow_up.target_order_id, "BOUND-SERVER-CANCEL-1")
        self.assertEqual(store.outbox_state(follow_up.intent_id), "PENDING")
        self.assertEqual(ReadServiceHandler.received, [])

    def test_combo_cancel_cannot_target_a_bound_single_order(self) -> None:
        store = getattr(self.server, "order_store")
        original = Intent(
            Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000",
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {"contract": {"code": "2330"}},
            },
        )
        armed_coordinator(store).accept(original)
        store.mark_pending_submit(original.intent_id, "BOUND-SINGLE-ORDER-1")
        getattr(self.server, "order_coordinator").gate = Gate.ARMED

        request = Request(
            f"{self.base}/api/v1/order/cancel_comboorder",
            data=b'{"trade_id":"BOUND-SINGLE-ORDER-1"}',
            headers={"Content-Type": "application/json", "Idempotency-Key": "mismatched-combo-cancel-1"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as rejected:
            urlopen(request)
        self.assertEqual(rejected.exception.code, 409)
        receipt = json.load(rejected.exception)
        self.assertIn("target_order_account_unverified", receipt["risk_reasons"])
        self.assertEqual(ReadServiceHandler.received, [])

    def test_cancel_combo_order_uses_the_same_durable_cancel_ingress(self) -> None:
        body = json.dumps({"trade_id": "COMBO-BROKER-ORDER-1"}).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/cancel_comboorder", data=body, method="POST"))
        self.assertEqual(raised.exception.code, 409)
        receipt = json.load(raised.exception)
        self.assertEqual(receipt["status"], "RISK_REJECTED")
        self.assertIn("target_order_account_unverified", receipt["risk_reasons"])
        observation = getattr(self.server, "order_store").observation_orders()[0]
        self.assertEqual(observation["operation"], "CANCEL")
        self.assertEqual(observation["target_order_id"], "COMBO-BROKER-ORDER-1")
        intent = getattr(self.server, "order_store").load_intent(receipt["intent_id"])
        self.assertEqual(
            intent.broker_request,
            {
                "path": "/api/v1/order/cancel_comboorder",
                "body": {"trade_id": "COMBO-BROKER-ORDER-1"},
            },
        )
        self.assertIsNone(getattr(self.server, "order_store").outbox_state(receipt["intent_id"]))
        self.assertEqual(ReadServiceHandler.received, [])

    def test_update_order_mutations_are_audited_but_fail_closed(self) -> None:
        for path, body, operation in (
            ("update_price", {"trade_id": "BROKER-ORDER-1", "price": 1000}, "UPDATE_PRICE"),
            ("update_qty", {"trade_id": "BROKER-ORDER-1", "quantity": 2}, "UPDATE_QUANTITY"),
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                urlopen(Request(
                    f"{self.base}/api/v1/order/{path}",
                    data=json.dumps(body).encode(), method="POST",
                ))
            self.assertEqual(raised.exception.code, 409)
            receipt = json.load(raised.exception)
            self.assertIn("target_order_account_unverified", receipt["risk_reasons"])
            observation = getattr(self.server, "order_store").observation_orders()[0]
            self.assertEqual(observation["operation"], operation)
            expected_change = {"price": body["price"]} if "price" in body else {"quantity": body["quantity"]}
            self.assertEqual(observation["operation_payload"], expected_change)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_target_mutation_rejects_unsupported_browser_fields(self) -> None:
        body = json.dumps({"trade_id": "BROKER-ORDER-1", "price": 1000, "account": "override"}).encode()
        with self.assertRaises(HTTPError) as rejected:
            urlopen(Request(
                f"{self.base}/api/v1/order/update_price", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            ))
        self.assertEqual(rejected.exception.code, 400)
        self.assertEqual(getattr(self.server, "order_store").observation_orders(), [])
        self.assertEqual(ReadServiceHandler.received, [])

    def test_manual_place_order_is_durably_rejected_while_disarmed(self) -> None:
        body = json.dumps({
            "contract": {"code": "2330"},
            "stock_order": {
                "action": "Buy", "price": 1000, "quantity": 1,
                "account": {"account_id": "SIM-001"},
            },
        }).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/place_order", data=body, method="POST"))
        self.assertEqual(raised.exception.code, 409)
        receipt = json.load(raised.exception)
        self.assertEqual(receipt["status"], "RISK_REJECTED")
        self.assertIn("trading_gate_not_armed", receipt["risk_reasons"])
        self.assertIn("交易閘門尚未啟用", receipt["message"])
        store = getattr(self.server, "order_store")
        self.assertEqual(store.outbox_state(receipt["intent_id"]), None)
        self.assertEqual(store.observation_orders()[0]["source"], "MANUAL_UI")
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            overview = json.load(response)
        self.assertIn("trading_gate_not_armed", overview["orders"][0]["risk_reasons"])
        self.assertEqual(overview["risk"]["last_verdict"], {
            "intent_id": receipt["intent_id"],
            "correlation_id": receipt["correlation_id"],
            "status": "RISK_REJECTED",
            "allowed": False,
            "reasons": ["trading_gate_not_armed"],
            "source": "MANUAL_UI",
            "updated_at": overview["orders"][0]["updated_at"],
        })
        self.assertEqual(ReadServiceHandler.received, [])

    def test_manual_idempotency_key_reuses_the_same_rejected_receipt(self) -> None:
        body = json.dumps({
            "contract": {"code": "2330"},
            "stock_order": {
                "action": "Buy", "price": 1000, "quantity": 1,
                "account": {"account_id": "SIM-001"},
            },
        }).encode()
        receipts = []
        for _ in range(2):
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(
                    f"{self.base}/api/v1/order/place_order", data=body, method="POST",
                    headers={"Idempotency-Key": "manual-retry-1"},
                ))
            self.assertEqual(raised.exception.code, 409)
            receipts.append(json.load(raised.exception))
        self.assertEqual(receipts[0]["intent_id"], receipts[1]["intent_id"])
        self.assertEqual(len(getattr(self.server, "order_store").observation_orders()), 1)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_idempotency_key_cannot_be_reused_for_a_different_order(self) -> None:
        first_body = {
            "contract": {"code": "2330"},
            "stock_order": {
                "action": "Buy", "price": 1000, "quantity": 1,
                "account": {"account_id": "SIM-001"},
            },
        }
        changed_body = {
            **first_body,
            "stock_order": {**first_body["stock_order"], "quantity": 2},
        }
        headers = {"Idempotency-Key": "manual-retry-conflict"}
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(
                f"{self.base}/api/v1/order/place_order", data=json.dumps(first_body).encode(), method="POST",
                headers=headers,
            ))
        first_receipt = json.load(raised.exception)
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(
                f"{self.base}/api/v1/order/place_order", data=json.dumps(changed_body).encode(), method="POST",
                headers=headers,
            ))
        self.assertEqual(raised.exception.code, 409)
        collision = json.load(raised.exception)
        self.assertEqual(collision["intent_id"], first_receipt["intent_id"])
        self.assertEqual(collision["message"], "idempotency key is already used by a different request")
        self.assertEqual(len(getattr(self.server, "order_store").observation_orders()), 1)

    def test_all_manual_mutation_ingresses_reuse_their_idempotent_receipts(self) -> None:
        requests = (
            ("cancel_order", {"trade_id": "BROKER-ORDER-1"}),
            ("update_price", {"trade_id": "BROKER-ORDER-1", "price": 1000}),
            ("update_qty", {"trade_id": "BROKER-ORDER-1", "quantity": 2}),
            ("place_comboorder", {
                "combo_contract": {"legs": [{"code": "TXFZ6"}, {"code": "TXFY6"}]},
                "order": {
                    "action": "Buy", "price": 0, "price_type": "MKT", "quantity": 1,
                    "account": {"account_id": "SIM-001"},
                },
            }),
        )
        for index, (path, body) in enumerate(requests):
            with self.subTest(path=path):
                headers = {"Idempotency-Key": f"manual-mutation-retry-{index}"}
                receipts = []
                for _ in range(2):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(Request(
                            f"{self.base}/api/v1/order/{path}", data=json.dumps(body).encode(), method="POST",
                            headers=headers,
                        ))
                    self.assertEqual(raised.exception.code, 409)
                    receipts.append(json.load(raised.exception))
                self.assertEqual(receipts[0]["intent_id"], receipts[1]["intent_id"])
        self.assertEqual(len(getattr(self.server, "order_store").observation_orders()), len(requests))
        self.assertEqual(ReadServiceHandler.received, [])

    def test_concurrent_same_key_replays_the_durable_receipt_without_a_500(self) -> None:
        body = json.dumps({
            "contract": {"code": "2330"},
            "stock_order": {
                "action": "Buy", "price": 1000, "quantity": 1,
                "account": {"account_id": "SIM-001"},
            },
        }).encode()
        store = getattr(self.server, "order_store")
        first_reads = threading.Barrier(2)

        def post() -> tuple[int, dict[str, object]]:
            first_reads.wait(timeout=5)
            try:
                urlopen(Request(
                    f"{self.base}/api/v1/order/place_order", data=body, method="POST",
                    headers={"Idempotency-Key": "concurrent-manual-retry"},
                ))
            except HTTPError as error:
                return error.code, json.load(error)
            self.fail("DISARMED mutation unexpectedly returned success")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _unused: post(), range(2)))
        self.assertEqual([status for status, _receipt in results], [409, 409])
        self.assertEqual(results[0][1]["intent_id"], results[1][1]["intent_id"])
        self.assertEqual(len(store.observation_orders()), 1)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_market_order_shape_is_audited_without_a_fake_limit_price_error(self) -> None:
        body = json.dumps({
            "contract": {"code": "2330"},
            "stock_order": {
                "action": "Buy", "price": 0, "price_type": "MKT", "quantity": 1,
                "account": {"account_id": "SIM-001"},
            },
        }).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/place_order", data=body, method="POST"))
        receipt = json.load(raised.exception)
        self.assertEqual(receipt["status"], "RISK_REJECTED")
        self.assertIn("trading_gate_not_armed", receipt["risk_reasons"])
        self.assertNotIn("invalid_limit_price", receipt["risk_reasons"])

    def test_combo_order_is_audited_without_contacting_the_sidecar(self) -> None:
        body = json.dumps({
            "combo_contract": {"legs": [{"code": "TXFZ6"}, {"code": "TXFY6"}]},
            "order": {
                "action": "Buy", "price": 0, "price_type": "MKT", "quantity": 1,
                "account": {"account_id": "SIM-001"},
            },
        }).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/place_comboorder", data=body, method="POST"))
        self.assertEqual(raised.exception.code, 409)
        receipt = json.load(raised.exception)
        self.assertIn("trading_gate_not_armed", receipt["risk_reasons"])
        observation = getattr(self.server, "order_store").observation_orders()[0]
        self.assertEqual(observation["instrument"], {
            "kind": "COMBO", "legs": [{"code": "TXFZ6"}, {"code": "TXFY6"}],
        })
        self.assertEqual(ReadServiceHandler.received, [])

    def test_intent_ingress_records_a_disarmed_risk_rejection_without_outbox(self) -> None:
        body = json.dumps({
            "source": "MANUAL_UI", "account_id": "SIM-001", "symbol": "2330",
            "side": "BUY", "quantity": 1, "limit_price": "1000",
        }).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/autotrade/intents", data=body, method="POST"))
        self.assertEqual(raised.exception.code, 409)
        receipt = json.load(raised.exception)
        self.assertEqual(receipt["status"], "RISK_REJECTED")
        self.assertIn("trading_gate_not_armed", receipt["risk_reasons"])
        store = getattr(self.server, "order_store")
        self.assertEqual(store.outbox_state(receipt["intent_id"]), None)

    def test_browser_cannot_impersonate_a_strategy_at_the_common_intent_ingress(self) -> None:
        body = json.dumps({
            "source": "STRATEGY", "account_id": "SIM-001", "symbol": "2330",
            "side": "BUY", "quantity": 1, "limit_price": "1000",
            "strategy_id": "reference-fixture", "run_id": "spoofed-run",
        }).encode()
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/autotrade/intents", data=body, method="POST"))
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(getattr(self.server, "order_store").observation_orders(), [])

    def test_strategy_validate_and_dry_run_never_contact_sidecar_or_persist_intent(self) -> None:
        for action in ("validate", "dry-run"):
            with self.subTest(action=action):
                request = Request(
                    f"{self.base}/api/v1/autotrade/strategies/reference-fixture/{action}",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(request) as response:
                    document = json.load(response)
                self.assertEqual(document["mode"], "VALIDATE" if action == "validate" else "DRY_RUN")
        self.assertEqual(getattr(self.server, "order_store").observation_orders(), [])
        self.assertEqual(ReadServiceHandler.received, [])

    def test_read_proxy_preserves_path_but_never_forwards_browser_credentials(self) -> None:
        request = Request(
            f"{self.base}/api/v1/data/contracts?code=2330",
            headers={"Accept": "application/json", "Authorization": "Bearer browser-secret", "Cookie": "session=browser-secret"},
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"path": "/api/v1/data/contracts?code=2330"})
        path, headers, body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/data/contracts?code=2330")
        self.assertEqual(body, b"")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_unlisted_read_route_is_not_forwarded_to_the_sidecar(self) -> None:
        for path in (
            "/api/v1/order/place_order",
            "/api/v1/data/not_an_upstream_web_read",
            "/api/v1/monitor/not_an_upstream_web_read",
            "/api/v1/data/contracts/combo",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{self.base}{path}")
                self.assertEqual(raised.exception.code, 404)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_upstream_timeouts_return_controlled_credential_free_gateway_errors(self) -> None:
        cases = (
            (Request(f"{self.base}/api/v1/data/contracts?code=2330"), "Shioaji read service unavailable"),
            (Request(
                f"{self.base}/api/v1/data/snapshots", data=b'{"contracts":["2330"]}', method="POST",
                headers={"Content-Type": "application/json"},
            ), "Shioaji read service unavailable"),
            (Request(
                f"{self.base}/api/v1/watchlist", data=b'{"name":"fixture"}', method="POST",
                headers={"Content-Type": "application/json"},
            ), "Shioaji watchlist service unavailable"),
            (Request(f"{self.base}/api/v1/stream/data?region=TW"), "Shioaji market stream unavailable"),
            (Request(
                f"{self.base}/api/v1/stream/data/order_event?account_type=S",
                headers={"Authorization": "Bearer browser-secret"},
            ), "Shioaji trade event stream unavailable"),
        )
        for upstream_error in (TimeoutError, RemoteDisconnected):
            for request, message in cases:
                with self.subTest(path=request.full_url, upstream_error=upstream_error.__name__), patch(
                    "services.autotrade_gateway.server.urlopen", side_effect=upstream_error,
                ), self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(raised.exception.code, 502)
                self.assertEqual(json.load(raised.exception)["message"], message)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_allowlisted_read_query_post_preserves_json_but_not_credentials(self) -> None:
        body = b'{"contracts":["2330"]}'
        request = Request(
            f"{self.base}/api/v1/data/snapshots",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer browser-secret"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        path, headers, upstream_body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/data/snapshots")
        self.assertEqual(upstream_body, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", headers)

    def test_all_compatibility_post_read_contracts_are_forwarded_without_browser_credentials(self) -> None:
        # This is deliberately a compatibility inventory rather than a copy
        # of READ_QUERY_POST_PATHS. A newly-added UI or reconciliation read
        # contract must be represented here with matching boundary coverage.
        paths = (
            "/api/v1/auth/usage",
            "/api/v1/data/credit_enquire",
            "/api/v1/data/index_components",
            "/api/v1/data/kbars",
            "/api/v1/data/scanner",
            "/api/v1/data/short_stock_sources",
            "/api/v1/data/snapshots",
            "/api/v1/data/ticks",
            "/api/v1/order/combotrades",
            "/api/v1/order/earmarking_detail",
            "/api/v1/order/order_deal_records",
            "/api/v1/order/stock_reserve_detail",
            "/api/v1/order/stock_reserve_summary",
            "/api/v1/order/trades",
            "/api/v1/portfolio/account_balance",
            "/api/v1/portfolio/margin",
            "/api/v1/portfolio/position_unit",
            "/api/v1/portfolio/profit_loss",
            "/api/v1/portfolio/profitloss_sum",
            "/api/v1/portfolio/settlements",
            "/api/v1/portfolio/trading_limits",
        )
        body = b'{"fixture":true}'
        for path in paths:
            with self.subTest(path=path):
                request = Request(
                    f"{self.base}{path}", data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer browser-secret",
                        "Cookie": "session=browser-secret",
                    },
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                received_path, headers, received_body = ReadServiceHandler.received[-1]
                self.assertEqual(received_path, path)
                self.assertEqual(received_body, body)
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Cookie", headers)

    def test_trade_callback_subscription_is_allowlisted_without_broker_mutation(self) -> None:
        body = b'{"broker_id":"9A95","account_id":"SIM-001","account_type":"S"}'
        request = Request(
            f"{self.base}/api/v1/auth/subscribe_trade",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer browser-secret",
                "Cookie": "session=browser-secret",
            },
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        path, headers, upstream_body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/auth/subscribe_trade")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        self.assertEqual(upstream_body, body)
        self.assertEqual(getattr(self.server, "order_store").observation_orders(), [])

    def test_watchlist_crud_is_allowlisted_without_order_access(self) -> None:
        cases = (
            ("POST", "/api/v1/watchlist", b'{"name":"my-list","contracts":[]}'),
            ("PUT", "/api/v1/watchlist/list-1", b'{"contracts":[]}'),
            ("POST", "/api/v1/watchlist/list-1/contracts", b'{"contracts":[]}'),
            ("DELETE", "/api/v1/watchlist/list-1/contracts", b'{"contracts":[]}'),
            ("DELETE", "/api/v1/watchlist/list-1", None),
        )
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                request = Request(
                    f"{self.base}{path}",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer browser-secret",
                        "Cookie": "session=browser-secret",
                    },
                    method=method,
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                received_path, headers, received_body = ReadServiceHandler.received[-1]
                self.assertEqual(received_path, path)
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Cookie", headers)
                self.assertEqual(received_body, body or b"")
        self.assertEqual(getattr(self.server, "order_store").observation_orders(), [])

    def test_malformed_watchlist_route_is_not_forwarded(self) -> None:
        request = Request(
            f"{self.base}/api/v1/watchlist/not%2Fa-valid-id/contracts",
            data=b'{"contracts":[]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(ReadServiceHandler.received, [])

    def test_market_sse_is_relayed_without_browser_credentials(self) -> None:
        request = Request(
            f"{self.base}/api/v1/stream/data?region=TW",
            headers={"Accept": "text/event-stream", "Last-Event-ID": "cursor-0", "Authorization": "Bearer browser-secret"},
        )
        with urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            self.assertIn(b"id: cursor-1", response.read())
        path, headers, body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/stream/data?region=TW")
        self.assertEqual(body, b"")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(headers["Last-Event-Id"], "cursor-0")
        self.assertNotIn("Authorization", headers)

    def test_trade_event_sse_is_relayed_without_browser_credentials(self) -> None:
        request = Request(
            f"{self.base}/api/v1/stream/data/order_event?account_type=S",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": "trade-cursor-0",
                "Authorization": "Bearer browser-secret",
                "Cookie": "session=browser-secret",
            },
        )
        with urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            self.assertIn(b"event: tick", response.read())
        path, headers, body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/stream/data/order_event?account_type=S")
        self.assertEqual(body, b"")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(headers["Last-Event-Id"], "trade-cursor-0")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_market_sse_disconnect_closes_quiet_upstream_stream(self) -> None:
        request = Request(
            f"{self.base}/api/v1/stream/data?hold=1",
            headers={"Accept": "text/event-stream"},
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.readline(), b": upstream-ready\n")
        self.assertTrue(ReadServiceHandler.market_hold_started.wait(1))
        self.assertTrue(
            ReadServiceHandler.market_hold_client_closed.wait(2),
            "downstream disconnect must close the quiet upstream SSE connection",
        )

    def test_market_subscription_is_allowlisted_but_order_mutations_remain_denied(self) -> None:
        body = b'{"contracts":[{"code":"2330"}],"quote_type":"tick"}'
        request = Request(
            f"{self.base}/api/v1/stream/subscribe",
            data=body,
            headers={"Content-Type": "application/json", "Cookie": "browser-secret"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        path, headers, upstream_body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/stream/subscribe")
        self.assertEqual(upstream_body, body)
        self.assertNotIn("Cookie", headers)

    def test_allowlisted_market_capability_subscription_is_forwarded(self) -> None:
        body = b'{"index":{"code":"IX0001"}}'
        request = Request(
            f"{self.base}/api/v1/stream/subscribe/calculated_index",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        path, _, upstream_body = ReadServiceHandler.received[-1]
        self.assertEqual(path, "/api/v1/stream/subscribe/calculated_index")
        self.assertEqual(upstream_body, body)

    def test_unlisted_market_capability_is_not_forwarded(self) -> None:
        body = b'{"index":{"code":"IX0001"}}'
        request = Request(
            f"{self.base}/api/v1/stream/subscribe/not-a-capability",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(ReadServiceHandler.received, [])


if __name__ == "__main__":
    unittest.main()
