"""Loopback-only Shioaji HTTP dispatch seam; never enabled by the Gateway."""

from __future__ import annotations

import json
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .order_core import Intent


class BrokerProtocolError(ValueError):
    """A durable intent cannot be safely mapped to the Shioaji HTTP contract."""


class HttpResponse(Protocol):
    def __enter__(self) -> HttpResponse: ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def read(self) -> bytes: ...


class HttpOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> HttpResponse: ...


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_OPERATION_PATHS = {
    "PLACE": frozenset({"/api/v1/order/place_order", "/api/v1/order/place_comboorder"}),
    "CANCEL": frozenset({"/api/v1/order/cancel_order", "/api/v1/order/cancel_comboorder"}),
    "UPDATE_PRICE": frozenset({"/api/v1/order/update_price"}),
    "UPDATE_QUANTITY": frozenset({"/api/v1/order/update_qty"}),
}


def durable_broker_request(intent: Intent) -> tuple[str, dict[str, object]]:
    """Return only an immutable, operation-matched request from an intent."""
    request = intent.broker_request
    if not isinstance(request, dict):
        raise BrokerProtocolError("durable broker request is unavailable")
    path = request.get("path")
    body = request.get("body")
    if not isinstance(path, str) or path not in _OPERATION_PATHS.get(intent.operation, frozenset()):
        raise BrokerProtocolError("durable broker request path does not match operation")
    if not isinstance(body, dict):
        raise BrokerProtocolError("durable broker request body is invalid")
    return path, body


def broker_order_identity(document: object) -> str:
    """Accept the canonical Shioaji Trade response identity and nothing else."""
    if not isinstance(document, dict):
        raise BrokerProtocolError("broker response is not an object")
    order = document.get("order")
    if not isinstance(order, dict) or not isinstance(order.get("id"), str) or not order["id"]:
        raise BrokerProtocolError("broker response has no order identity")
    return order["id"]


class ShioajiHttpDispatcher:
    """Execution-worker adapter for a local sidecar HTTP API.

    The adapter owns no Shioaji SDK session or credential. Its origin is
    constrained to loopback, and the currently running Gateway deliberately
    does not construct this class. A future explicit simulation activation
    must separately wire it into the single ExecutionWorker owner.
    """

    def __init__(
        self,
        origin: str,
        *,
        timeout_seconds: float = 5,
        opener: HttpOpener = urlopen,
    ) -> None:
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Shioaji HTTP origin must be a credential-free loopback origin")
        if timeout_seconds <= 0:
            raise ValueError("HTTP dispatcher timeout must be positive")
        self.origin = origin.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def dispatch(self, intent: Intent) -> str:
        path, body = durable_broker_request(intent)
        payload = json.dumps(body, separators=(",", ":")).encode()
        request = Request(
            f"{self.origin}{path}",
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            try:
                document = json.loads(response.read())
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise BrokerProtocolError("broker response is invalid JSON") from error
        return broker_order_identity(document)
