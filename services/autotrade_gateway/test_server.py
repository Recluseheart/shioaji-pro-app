from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.autotrade_gateway.server import create_server


class GatewayServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

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


if __name__ == "__main__":
    unittest.main()
