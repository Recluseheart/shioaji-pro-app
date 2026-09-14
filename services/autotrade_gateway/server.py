"""Fail-closed Gateway seed; it owns no broker session or mutation path."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .order_core import Gate, Intent, OrderCoordinator, SQLiteIntentStore, Source


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

    def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _proxy_read(self) -> None:
        """Forward allowed read traffic without forwarding browser credentials."""
        upstream = getattr(self.server, "upstream_origin")
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in {"accept", "if-none-match", "if-modified-since"}
        }
        request = Request(f"{upstream}{self.path}", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=5) as response:
                self._bytes(
                    response.status,
                    response.read(),
                    response.headers.get_content_type(),
                )
        except HTTPError as error:
            self._bytes(error.code, error.read(), error.headers.get_content_type())
        except URLError:
            self._json(HTTPStatus.BAD_GATEWAY, {"message": "Shioaji read service unavailable"})

    def _intent(self) -> Intent:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 8_192:
            raise ValueError("invalid request body size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("intent body must be an object")
        return Intent(
            source=Source(value["source"]),
            account_id=str(value["account_id"]),
            symbol=str(value["symbol"]),
            side=str(value["side"]),
            quantity=int(value["quantity"]),
            limit_price=str(value["limit_price"]),
            strategy_id=str(value["strategy_id"]) if value.get("strategy_id") else None,
            run_id=str(value["run_id"]) if value.get("run_id") else None,
        )

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path == "/api/v1/autotrade/health":
            self._json(HTTPStatus.OK, health_document())
            return
        if path == "/api/v1/autotrade/overview":
            self._json(HTTPStatus.OK, overview_document())
            return
        if path.startswith("/api/"):
            self._proxy_read()
            return
        self._json(HTTPStatus.NOT_FOUND, {"message": "route not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.partition("?")[0] == "/api/v1/autotrade/intents":
            try:
                intent = self._intent()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid autotrade intent"})
                return
            coordinator = getattr(self.server, "order_coordinator")
            status, verdict = coordinator.accept(intent)
            self._json(
                HTTPStatus.ACCEPTED if verdict.allowed else HTTPStatus.CONFLICT,
                {
                    "intent_id": intent.intent_id,
                    "correlation_id": intent.correlation_id,
                    "status": status.value,
                    "risk_reasons": list(verdict.reasons),
                },
            )
            return
        self._json(HTTPStatus.FORBIDDEN, {"message": "Autotrade mutation is disabled"})

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def create_server(
    host: str = "127.0.0.1",
    port: int = 18082,
    upstream_origin: Optional[str] = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    server.upstream_origin = (upstream_origin or os.getenv("SHIOAJI_HTTP_ORIGIN", "http://127.0.0.1:21322")).rstrip("/")  # type: ignore[attr-defined]
    store = SQLiteIntentStore(os.getenv("AUTOTRADE_GATEWAY_DB", ":memory:"))
    server.order_store = store  # type: ignore[attr-defined]
    server.order_coordinator = OrderCoordinator(store, gate=Gate.DISARMED)  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    running_server = create_server()
    try:
        running_server.serve_forever()
    finally:
        getattr(running_server, "order_store").close()
