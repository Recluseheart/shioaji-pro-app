from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.autotrade_gateway.order_core import Gate, Intent, OrderCoordinator, SQLiteIntentStore, Source, Status
from services.autotrade_gateway.server import create_server


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

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        type(self).received.append((self.path, dict(self.headers.items()), body))
        payload = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


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

    def test_reconciliation_endpoint_exposes_unknown_without_a_resend(self) -> None:
        store = getattr(self.server, "order_store")
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(store, Gate.ARMED).accept(intent)
        store.recover()
        with urlopen(f"{self.base}/api/v1/autotrade/reconciliation") as response:
            document = json.load(response)
        self.assertEqual(document["items"][0]["intent_id"], intent.intent_id)
        self.assertEqual(document["items"][0]["status"], "UNKNOWN")
        self.assertEqual(store.outbox_state(intent.intent_id), "UNKNOWN")

    def test_gateway_startup_recovers_persisted_pending_delivery_to_unknown(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "restart-recovery.db"
            previous_store = SQLiteIntentStore(database_path)
            intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
            OrderCoordinator(previous_store, Gate.ARMED).accept(intent)
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
        store = getattr(self.server, "order_store")
        self.assertEqual(store.outbox_state(receipt["intent_id"]), None)
        self.assertEqual(store.observation_orders()[0]["source"], "MANUAL_UI")
        with urlopen(f"{self.base}/api/v1/autotrade/overview") as response:
            overview = json.load(response)
        self.assertIn("trading_gate_not_armed", overview["orders"][0]["risk_reasons"])
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
