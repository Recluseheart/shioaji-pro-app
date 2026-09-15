from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from services.autotrade_gateway.broker_http import BrokerProtocolError, ShioajiHttpDispatcher, durable_broker_request
from services.autotrade_gateway.order_core import Gate, Intent, OrderCoordinator, RiskContext, SQLiteIntentStore, Source, Status
from services.autotrade_gateway.worker import ExecutionWorker


class BrokerMockHandler(BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []
    response_payload = b'{"order":{"id":"mock-order-1"}}'

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received.append((self.path, dict(self.headers.items()), body))
        payload = type(self).response_payload
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def armed_coordinator(store: SQLiteIntentStore) -> OrderCoordinator:
    return OrderCoordinator(
        store,
        Gate.ARMED,
        risk_context_provider=lambda: RiskContext(
            quote_price=Decimal("1000"), quote_at=datetime.now(timezone.utc),
        ),
    )


class BrokerHttpDispatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        BrokerMockHandler.received.clear()
        BrokerMockHandler.response_payload = b'{"order":{"id":"mock-order-1"}}'
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BrokerMockHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.store = SQLiteIntentStore(":memory:")
        self.dispatcher = ShioajiHttpDispatcher(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self) -> None:
        self.store.close()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_worker_dispatches_the_durable_manual_request_to_loopback_mock(self) -> None:
        request_body = {
            "contract": {"code": "2330"},
            "stock_order": {"action": "Buy", "price": 1000, "quantity": 1, "account": {"account_id": "SIM-001"}},
        }
        intent = Intent(
            Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000",
            broker_request={"path": "/api/v1/order/place_order", "body": request_body},
        )
        armed_coordinator(self.store).accept(intent)

        self.assertTrue(ExecutionWorker(self.store, self.dispatcher).dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.PENDING_SUBMIT)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "DISPATCHED")
        path, headers, body = BrokerMockHandler.received[-1]
        self.assertEqual(path, "/api/v1/order/place_order")
        self.assertEqual(json.loads(body), request_body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_missing_identity_or_unmappable_request_never_produces_a_pending_submit(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)

        self.assertTrue(ExecutionWorker(self.store, self.dispatcher).dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        self.assertEqual(BrokerMockHandler.received, [])
        with self.assertRaises(BrokerProtocolError):
            durable_broker_request(intent)

    def test_missing_broker_identity_after_delivery_becomes_unknown_without_retry(self) -> None:
        BrokerMockHandler.response_payload = b'{"order":{}}'
        intent = Intent(
            Source.MANUAL_UI,
            "SIM-001",
            "2330",
            "BUY",
            1,
            "1000",
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {"contract": {"code": "2330"}, "stock_order": {"quantity": 1}},
            },
        )
        armed_coordinator(self.store).accept(intent)

        self.assertTrue(ExecutionWorker(self.store, self.dispatcher).dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        self.assertEqual(len(BrokerMockHandler.received), 1)

    def test_dispatcher_rejects_non_loopback_or_credential_origins(self) -> None:
        for origin in ("https://broker.example", "http://user:pass@127.0.0.1:21322", "http://127.0.0.1:21322/api"):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    ShioajiHttpDispatcher(origin)
