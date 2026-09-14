"""Fail-closed Gateway seed; it owns no broker session or mutation path."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def health_document() -> dict[str, Any]:
    """Stable first read contract consumed by the Web Autotrade panels."""
    return {
        "environment": "SIM",
        "trading_gate": "DISARMED",
        "strategy_count": 0,
        "enabled_strategy_count": 0,
        "gateway_status": "READY",
    }


def overview_document() -> dict[str, Any]:
    """Read model for the Autotrade panels.

    This seed deliberately reports only durable facts it owns.  It must not
    manufacture strategy activity, orders, or broker callbacks merely to make
    a dashboard appear busy.
    """
    return {
        "health": health_document(),
        "strategies": [],
        "signals": [],
        "orders": [],
        "risk": {
            "mode": "ENFORCING",
            "limits_source": "not_configured",
            "last_verdict": None,
        },
        "reconciliation": [],
        "performance": {
            "run_count": 0,
            "filled_order_count": 0,
            "realized_pnl": "0",
            "unrealized_pnl": "0",
        },
    }


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "AutotradeGateway/0.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Do not emit request bodies, query parameters or credentials.
        return

    def _json(self, status: HTTPStatus, document: dict[str, Any]) -> None:
        payload = json.dumps(document, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/v1/autotrade/health":
            self._json(HTTPStatus.OK, health_document())
            return
        if self.path == "/api/v1/autotrade/overview":
            self._json(HTTPStatus.OK, overview_document())
            return
        self._json(HTTPStatus.NOT_FOUND, {"message": "route not found"})

    def do_POST(self) -> None:  # noqa: N802
        self._json(HTTPStatus.FORBIDDEN, {"message": "Autotrade mutation is disabled"})

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def create_server(host: str = "127.0.0.1", port: int = 18082) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), GatewayHandler)


if __name__ == "__main__":
    create_server().serve_forever()
