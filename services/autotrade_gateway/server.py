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


# These GET routes have no order-mutation semantics in the Shioaji HTTP
# contract.  POST-shaped read APIs and SSE need dedicated adapters: forwarding
# them generically would blur the only-mutation-owner boundary.
READ_PROXY_EXACT_PATHS = frozenset({
    "/api/v1/health",
    "/api/v1/info",
    "/api/v1/watchlist",
})
READ_PROXY_PREFIXES = (
    "/api/v1/auth/accounts",
    "/api/v1/auth/ca_expiretime",
    "/api/v1/auth/usage",
    "/api/v1/data/",
    "/api/v1/monitor/",
    "/api/v1/order/trades",
    "/api/v1/order/combotrades",
    "/api/v1/order/stock_reserve_summary",
    "/api/v1/order/stock_reserve_detail",
    "/api/v1/order/earmarking_detail",
)
READ_QUERY_POST_PATHS = frozenset({
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
})
MARKET_SSE_PATH = "/api/v1/stream/data"
MARKET_STREAM_POST_PATHS = frozenset({
    "/api/v1/stream/subscribe",
    "/api/v1/stream/unsubscribe",
})
MARKET_STREAM_CAPABILITIES = frozenset({
    "calculated_index",
    "index_components",
    "index_contribution",
    "industry_contribution",
    "scanner",
})


def is_allowed_read_path(path: str) -> bool:
    """Return whether a path belongs to the deliberately narrow GET proxy."""
    return path in READ_PROXY_EXACT_PATHS or path.startswith(READ_PROXY_PREFIXES)


def is_allowed_market_stream_post(path: str) -> bool:
    """Allow only named market subscriptions, never an arbitrary stream POST."""
    if path in MARKET_STREAM_POST_PATHS:
        return True
    for action in ("subscribe", "unsubscribe"):
        prefix = f"/api/v1/stream/{action}/"
        if path.startswith(prefix):
            return path.removeprefix(prefix) in MARKET_STREAM_CAPABILITIES
    return False


def health_document() -> dict[str, Any]:
    """Stable first read contract consumed by the Web Autotrade panels."""
    return {
        "environment": "SIM",
        "trading_gate": "DISARMED",
        "strategy_count": 0,
        "enabled_strategy_count": 0,
        "gateway_status": "READY",
    }


def overview_document(store: Any = None) -> dict[str, Any]:
    """Read model for the Autotrade panels.

    This seed deliberately reports only durable facts it owns.  It must not
    manufacture strategy activity, orders, or broker callbacks merely to make
    a dashboard appear busy.
    """
    orders = store.observation_orders() if store is not None else []
    return {
        "health": health_document(),
        "strategies": [],
        "signals": [],
        "orders": orders,
        "risk": {
            "mode": "ENFORCING",
            "limits_source": "not_configured",
            "last_verdict": None,
        },
        "reconciliation": [item for item in orders if item["status"] in {"UNKNOWN", "RECONCILING", "MANUAL_REVIEW"}],
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

    def _proxy_read_query(self, body: bytes) -> None:
        """Forward an explicitly read-only JSON query without browser credentials."""
        upstream = getattr(self.server, "upstream_origin")
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in {"accept", "content-type"}
        }
        request = Request(f"{upstream}{self.path}", data=body, headers=headers, method="POST")
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

    def _relay_market_sse(self) -> None:
        """Relay the single upstream market stream without credentials or buffering."""
        upstream = getattr(self.server, "upstream_origin")
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in {"accept", "last-event-id"}
        }
        request = Request(f"{upstream}{self.path}", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=5) as response:
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get_content_type())
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                while chunk := response.read(4_096):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except HTTPError as error:
            self._bytes(error.code, error.read(), error.headers.get_content_type())
        except URLError:
            self._json(HTTPStatus.BAD_GATEWAY, {"message": "Shioaji market stream unavailable"})
        except (BrokenPipeError, ConnectionResetError):
            # The browser can leave a stream at any time; upstream state is
            # intentionally not changed because subscribe lifecycle is still
            # an explicit compatibility task.
            return

    def _read_query_body(self) -> bytes:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 65_536:
            raise ValueError("invalid read query body size")
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("read query must be JSON")
        body = self.rfile.read(size)
        parsed = json.loads(body)
        if not isinstance(parsed, (dict, list)):
            raise ValueError("read query must be a JSON object or array")
        return body

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

    def _manual_place_order_intent(self) -> Intent:
        """Map the upstream stock/futures limit-order shape into the common spine.

        This is deliberately an ingress-only compatibility step.  The fixed
        DISARMED gate records the request and returns a risk receipt; no
        sidecar order endpoint is contacted until a future worker is able to
        own the complete durable delivery protocol.
        """
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 65_536:
            raise ValueError("invalid manual order body size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("manual order body must be an object")
        contract = value.get("contract")
        stock_order = value.get("stock_order")
        futures_order = value.get("futures_order")
        if not isinstance(contract, dict) or bool(stock_order) == bool(futures_order):
            raise ValueError("one stock or futures order is required")
        order = stock_order if stock_order else futures_order
        if not isinstance(order, dict) or not isinstance(order.get("account"), dict):
            raise ValueError("manual order account is required")
        account = order["account"]
        return Intent(
            source=Source.MANUAL_UI,
            account_id=str(account["account_id"]),
            symbol=str(contract["code"]),
            side=str(order["action"]).upper(),
            quantity=int(order["quantity"]),
            limit_price=str(order["price"]),
        )

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path == "/api/v1/autotrade/health":
            self._json(HTTPStatus.OK, health_document())
            return
        if path == "/api/v1/autotrade/overview":
            self._json(HTTPStatus.OK, overview_document(getattr(self.server, "order_store")))
            return
        if path == MARKET_SSE_PATH:
            self._relay_market_sse()
            return
        if is_allowed_read_path(path):
            self._proxy_read()
            return
        if path.startswith("/api/"):
            self._json(HTTPStatus.NOT_FOUND, {"message": "read route not permitted"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"message": "route not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path == "/api/v1/order/place_order":
            try:
                intent = self._manual_place_order_intent()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid manual order"})
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
        if path == "/api/v1/autotrade/intents":
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
        if path in READ_QUERY_POST_PATHS or is_allowed_market_stream_post(path):
            try:
                body = self._read_query_body()
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid read query"})
                return
            self._proxy_read_query(body)
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
    database_url = os.getenv("AUTOTRADE_GATEWAY_DATABASE_URL")
    if database_url:
        from .postgres_intent_store import PostgresIntentStore

        store = PostgresIntentStore(
            database_url,
            os.getenv("AUTOTRADE_GATEWAY_PG_SCHEMA", "autotrade_gateway"),
        )
    else:
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
