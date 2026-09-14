from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.autotrade_gateway.server import create_server


class ReadServiceHandler(BaseHTTPRequestHandler):
    received: list[tuple[str, dict[str, str], bytes]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).received.append((self.path, dict(self.headers.items()), b""))
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
        self.assertEqual(overview["strategies"], [])
        self.assertEqual(overview["orders"], [])
        self.assertEqual(overview["reconciliation"], [])

    def test_every_mutation_is_denied_before_broker_access(self) -> None:
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(f"{self.base}/api/v1/order/place_order", method="POST"))
        self.assertEqual(raised.exception.code, 403)

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


if __name__ == "__main__":
    unittest.main()
