from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.autotrade_gateway.order_core import Gate, Intent, OrderCoordinator, RiskContext, SQLiteIntentStore, Source, Status
from services.autotrade_gateway.server import create_server


def armed_coordinator(store: SQLiteIntentStore) -> OrderCoordinator:
    return OrderCoordinator(
        store,
        Gate.ARMED,
        risk_context_provider=lambda: RiskContext(
            quote_price=Decimal("1000"),
            quote_at=datetime.now(timezone.utc),
        ),
    )


class ReadServiceHandler(BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).received.append((self.path, dict(self.headers.items()), b""))
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
        ReadServiceHandler.received.clear()
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
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        getattr(self.server, "order_store").close()
        self.upstream.shutdown()
        self.upstream_thread.join()
        self.upstream.server_close()

    def test_health_is_sim_disarmed_and_cache_safe(self) -> None:
        with urlopen(f"{self.base}/api/v1/autotrade/health") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(json.load(response)["trading_gate"], "DISARMED")

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
            "last_event_id": None, "last_intent_id": None, "last_status": None,
        }])
        self.assertEqual(overview["risk"]["limits_source"], "gateway_static_simulation_v1")
        self.assertEqual(overview["risk"]["limits"]["max_order_notional"], "5000000")
        self.assertEqual(overview["risk"]["limits"]["max_gross_exposure"], "10000000")
        self.assertEqual(overview["risk"]["limits"]["max_quote_age_seconds"], 5)
        self.assertEqual(overview["orders"], [])
        self.assertEqual(overview["reconciliation"], [])

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

    def test_runs_endpoint_projects_only_durable_strategy_intents(self) -> None:
        store = getattr(self.server, "order_store")
        strategy_intent = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "fixture", "run-durable")
        manual_intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        coordinator = OrderCoordinator(store, Gate.DISARMED)
        coordinator.accept(strategy_intent)
        coordinator.accept(manual_intent)
        with urlopen(f"{self.base}/api/v1/autotrade/runs") as response:
            document = json.load(response)
        self.assertEqual(document["items"], [
            {"run_id": "run-durable", "strategy_id": "fixture", "last_intent_id": strategy_intent.intent_id,
             "last_status": "RISK_REJECTED", "updated_at": document["items"][0]["updated_at"], "intent_count": 1},
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
        self.assertEqual(store.outbox_state(intent.intent_id), "UNKNOWN")

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
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/reserve_stock", method="POST"))
        self.assertEqual(raised.exception.code, 403)

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
        original_has_intent = store.has_intent
        first_reads = threading.Barrier(2)
        calls = 0

        def synchronize_initial_reads(intent_id: str) -> bool:
            nonlocal calls
            calls += 1
            if calls <= 2:
                first_reads.wait(timeout=5)
            return original_has_intent(intent_id)

        def post() -> tuple[int, dict[str, object]]:
            try:
                urlopen(Request(
                    f"{self.base}/api/v1/order/place_order", data=body, method="POST",
                    headers={"Idempotency-Key": "concurrent-manual-retry"},
                ))
            except HTTPError as error:
                return error.code, json.load(error)
            self.fail("DISARMED mutation unexpectedly returned success")

        with patch.object(store, "has_intent", side_effect=synchronize_initial_reads):
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
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base}/api/v1/order/place_order")
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
        )
        for request, message in cases:
            with self.subTest(path=request.full_url), patch(
                "services.autotrade_gateway.server.urlopen", side_effect=TimeoutError,
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

    def test_all_web_post_read_contracts_are_forwarded_without_browser_credentials(self) -> None:
        # This is deliberately a UI-facing inventory rather than a copy of
        # READ_QUERY_POST_PATHS. A newly-added frontend contract must be
        # represented here with the corresponding compatibility coverage.
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
