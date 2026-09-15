"""Fail-closed Gateway seed; it owns no broker session or mutation path."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from .order_core import (
    DuplicateIntentError,
    Gate,
    Intent,
    IntentStore,
    OrderCoordinator,
    SQLiteIntentStore,
    Source,
)
from .strategy_runtime import StrategyRuntime


# These GET routes have no order-mutation semantics in the Shioaji HTTP
# contract.  POST-shaped read APIs and SSE need dedicated adapters: forwarding
# them generically would blur the only-mutation-owner boundary.
READ_PROXY_EXACT_PATHS = frozenset({
    "/api/v1/health",
    "/api/v1/info",
    "/api/v1/watchlist",
    "/api/v1/auth/accounts",
    "/api/v1/auth/ca_expiretime",
    "/api/v1/auth/usage",
    "/api/v1/order/trades",
    "/api/v1/order/combotrades",
    "/api/v1/order/stock_reserve_summary",
    "/api/v1/order/stock_reserve_detail",
    "/api/v1/order/earmarking_detail",
})
READ_PROXY_PREFIXES = ("/api/v1/data/", "/api/v1/monitor/")
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
COMBO_CONTRACT_BUILD_PATH = "/api/v1/data/contracts/combo"
MARKET_STREAM_POST_PATHS = frozenset({
    "/api/v1/stream/subscribe",
    "/api/v1/stream/unsubscribe",
})
TRADE_CALLBACK_SUBSCRIPTION_PATH = "/api/v1/auth/subscribe_trade"
WATCHLIST_PATH = "/api/v1/watchlist"
_WATCHLIST_ID = r"[A-Za-z0-9_-]{1,128}"
MARKET_STREAM_CAPABILITIES = frozenset({
    "calculated_index",
    "index_components",
    "index_contribution",
    "industry_contribution",
    "scanner",
})
RISK_REASON_LABELS = {
    "trading_gate_not_armed": "交易閘門尚未啟用",
    "target_order_account_unverified": "目標委託的帳戶尚未驗證",
    "target_order_id_required": "缺少目標委託識別碼",
    "simulation_account_required": "僅允許 simulation 帳戶",
    "simulation_account_not_allowed": "此 simulation 帳戶不在 allowlist",
    "symbol_not_allowed": "商品不在 allowlist",
    "quantity_limit_exceeded": "數量超過單筆限額",
    "notional_limit_exceeded": "名目金額超過單筆限額",
    "risk_context_unavailable": "缺少權威市場與帳務風控快照",
    "stale_quote": "行情已過期或不可用",
    "price_deviation_exceeded": "委託價格偏離最新行情過大",
    "position_limit_exceeded": "預估商品部位超過限額",
    "gross_exposure_limit_exceeded": "預估總曝險超過限額",
    "daily_loss_limit_exceeded": "每日損失已達限額",
}


AUTOTRADE_EVENTS_PATH = "/api/v1/autotrade/events"
_DISABLED_STRATEGY_CONTROL_PATHS = frozenset(
    f"/api/v1/autotrade/strategies/reference-fixture/{action}"
    for action in ("start", "pause", "resume", "stop")
)


def autotrade_event_id(observation: dict[str, object]) -> str:
    """Make the immutable observation version suitable for Last-Event-ID."""
    return f"{observation['intent_id']}:{observation['updated_at']}"


class AutotradeEventHub:
    """In-process fan-out for durable observation notifications only.

    The durable journal remains the source of truth: every new SSE connection
    first replays its current projection, so an event lost during a process
    restart cannot be mistaken for a broker delivery failure.  The hub only
    wakes connected observers after a durable write; it has no mutation or
    broker capability.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._listeners: set[Queue[dict[str, object]]] = set()

    def subscribe(self) -> Queue[dict[str, object]]:
        listener: Queue[dict[str, object]] = Queue()
        with self._lock:
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: Queue[dict[str, object]]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def publish(self, observation: dict[str, object]) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener.put(observation)


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


def is_allowed_watchlist_mutation(path: str, method: str) -> bool:
    """Allow only the upstream's non-broker watchlist CRUD shapes."""
    if method == "POST" and path == WATCHLIST_PATH:
        return True
    match = re.fullmatch(rf"{WATCHLIST_PATH}/({_WATCHLIST_ID})(/contracts)?", path)
    if match is None:
        return False
    has_contracts = match.group(2) is not None
    return (method == "PUT" and not has_contracts) or (method == "POST" and has_contracts) or method == "DELETE"


def is_valid_combo_contract_query(value: object) -> bool:
    """Recognize the broker-free, two-leg futures contract resolver shape.

    The Shioaji sidecar owns canonicalization of a futures spread's contract
    identity. That is not an order or account mutation, but it still merits
    its own narrow validation instead of joining the generic read-query set.
    """
    if not isinstance(value, dict) or set(value) != {"legs"}:
        return False
    legs = value.get("legs")
    if not isinstance(legs, list) or len(legs) != 2:
        return False
    allowed_fields = {"security_type", "region", "exchange", "code", "target_code"}
    for leg in legs:
        if not isinstance(leg, dict) or set(leg) != allowed_fields:
            return False
        if leg.get("security_type") != "FUT" or leg.get("region") != "TW":
            return False
        if not isinstance(leg.get("exchange"), str) or not leg["exchange"]:
            return False
        if not isinstance(leg.get("code"), str) or not leg["code"]:
            return False
        if leg.get("target_code") is not None:
            return False
    return True


def health_document(strategy_runtime: StrategyRuntime | None = None) -> dict[str, Any]:
    """Stable first read contract consumed by the Web Autotrade panels."""
    observations = strategy_runtime.observations() if strategy_runtime else []
    return {
        "environment": "SIM",
        "trading_gate": "DISARMED",
        "strategy_count": len(observations),
        "enabled_strategy_count": sum(item.get("enabled") is True for item in observations),
        "gateway_status": "READY",
    }


def strategy_runs_document(store: IntentStore) -> list[dict[str, object]]:
    """Return lifecycle facts owned by the durable strategy-run journal."""
    return store.observation_strategy_runs()


def latest_risk_verdict(orders: list[dict[str, object]]) -> dict[str, object] | None:
    """Project the latest durable ingress verdict without inventing risk facts."""
    if not orders:
        return None
    latest = orders[0]
    reasons = latest.get("risk_reasons")
    return {
        "intent_id": latest.get("intent_id"),
        "correlation_id": latest.get("correlation_id"),
        "status": latest.get("status"),
        "allowed": latest.get("status") != "RISK_REJECTED",
        "reasons": list(reasons) if isinstance(reasons, list) else [],
        "source": latest.get("source"),
        "updated_at": latest.get("updated_at"),
    }


def reconciliation_document(store: IntentStore, orders: list[dict[str, object]]) -> list[dict[str, object]]:
    """Join open reconciliation evidence to immutable order observations."""
    cases = {
        case["intent_id"]: case for case in store.observation_reconciliation_cases()
        if isinstance(case.get("intent_id"), str)
    }
    return [
        {**item, "reconciliation_case": cases.get(item.get("intent_id"))}
        for item in orders if item["status"] in {"UNKNOWN", "RECONCILING", "MANUAL_REVIEW"}
    ]


def overview_document(
    store: IntentStore | None = None,
    strategy_runtime: StrategyRuntime | None = None,
    coordinator: OrderCoordinator | None = None,
) -> dict[str, Any]:
    """Read model for the Autotrade panels.

    This seed deliberately reports only durable facts it owns.  It must not
    manufacture strategy activity, orders, or broker callbacks merely to make
    a dashboard appear busy.
    """
    orders = store.observation_orders() if store is not None else []
    runs = strategy_runs_document(store) if store is not None else []
    return {
        "health": health_document(strategy_runtime),
        "strategies": strategy_runtime.observations() if strategy_runtime else [],
        "signals": store.observation_signals() if store is not None else [],
        "runs": runs,
        "decisions": orders,
        "orders": orders,
        "risk": {
            "mode": "ENFORCING",
            "limits_source": "gateway_static_simulation_v1" if coordinator else "not_configured",
            "limits": coordinator.risk.limits.document() if coordinator else {},
            "last_verdict": latest_risk_verdict(orders),
        },
        "reconciliation": reconciliation_document(store, orders) if store is not None else [],
        "performance": {
            "run_count": len(runs),
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
        reasons = document.get("risk_reasons")
        if "message" not in document and isinstance(reasons, list) and reasons:
            labels = [RISK_REASON_LABELS.get(reason, str(reason)) for reason in reasons]
            document = {**document, "message": f"風控拒絕：{'；'.join(labels)}"}
        payload = json.dumps(document, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _with_idempotency_key(self, intent: Intent) -> Intent:
        key = self.headers.get("Idempotency-Key")
        if key is None:
            return intent
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise ValueError("invalid idempotency key")
        return replace(
            intent,
            intent_id=str(uuid5(NAMESPACE_URL, f"manual-intent:{key}")),
            correlation_id=str(uuid5(NAMESPACE_URL, f"manual-correlation:{key}")),
        )

    def _accept_or_reuse_intent(self, intent: Intent) -> None:
        store = getattr(self.server, "order_store")
        if store.has_intent(intent.intent_id):
            self._emit_existing_or_collision(intent)
            return
        try:
            status, verdict = getattr(self.server, "order_coordinator").accept(intent)
        except DuplicateIntentError:
            # A second worker may have observed the id before the first worker
            # committed it.  The unique constraint is the arbiter; now return
            # the durable receipt rather than leaking a database error or
            # creating another intent.
            self._emit_existing_or_collision(intent)
            return
        self._json(HTTPStatus.ACCEPTED if verdict.allowed else HTTPStatus.CONFLICT, {
            "intent_id": intent.intent_id, "correlation_id": intent.correlation_id,
            "status": status.value, "risk_reasons": list(verdict.reasons),
        })
        observation = next(
            item for item in store.observation_orders()
            if item["intent_id"] == intent.intent_id
        )
        getattr(self.server, "autotrade_event_hub").publish(observation)

    def _emit_existing_or_collision(self, intent: Intent) -> None:
        """Return a retry receipt only when it represents this exact request."""
        store = getattr(self.server, "order_store")
        if not store.has_intent(intent.intent_id):
            # This can only happen if an out-of-process store was replaced
            # between conflict detection and the read.  Do not manufacture a
            # receipt or retry it implicitly.
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "message": "idempotency receipt is not available; retry with the same key",
                "intent_id": intent.intent_id,
            })
            return
        if not store.request_matches(intent):
            self._json(HTTPStatus.CONFLICT, {
                "message": "idempotency key is already used by a different request",
                "intent_id": intent.intent_id,
            })
            return
        existing = next(item for item in store.observation_orders() if item["intent_id"] == intent.intent_id)
        self._json(HTTPStatus.CONFLICT, {
            "intent_id": existing["intent_id"], "correlation_id": existing["correlation_id"],
            "status": existing["status"], "risk_reasons": existing["risk_reasons"],
        })

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
        except (URLError, TimeoutError):
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
        except (URLError, TimeoutError):
            self._json(HTTPStatus.BAD_GATEWAY, {"message": "Shioaji read service unavailable"})

    def _proxy_watchlist_mutation(self, body: bytes | None) -> None:
        """Proxy a narrowly-scoped UI preference mutation without credentials."""
        upstream = getattr(self.server, "upstream_origin")
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in {"accept", "content-type"}
        }
        request = Request(f"{upstream}{self.path}", data=body, headers=headers, method=self.command)
        try:
            with urlopen(request, timeout=5) as response:
                self._bytes(
                    response.status,
                    response.read(),
                    response.headers.get_content_type(),
                )
        except HTTPError as error:
            self._bytes(error.code, error.read(), error.headers.get_content_type())
        except (URLError, TimeoutError):
            self._json(HTTPStatus.BAD_GATEWAY, {"message": "Shioaji watchlist service unavailable"})

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
        except (URLError, TimeoutError):
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

    def _combo_contract_query_body(self) -> bytes:
        body = self._read_query_body()
        if not is_valid_combo_contract_query(json.loads(body)):
            raise ValueError("invalid futures combo contract query")
        return body

    def _watchlist_mutation_body(self) -> bytes | None:
        size = int(self.headers.get("Content-Length", "0"))
        if size == 0 and self.command == "DELETE":
            return None
        if size <= 0 or size > 65_536:
            raise ValueError("invalid watchlist mutation body size")
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("watchlist mutation must be JSON")
        body = self.rfile.read(size)
        if not isinstance(json.loads(body), dict):
            raise ValueError("watchlist mutation must be a JSON object")
        return body

    def _sse_event(self, observation: dict[str, object]) -> bytes:
        event_id = autotrade_event_id(observation)
        payload = json.dumps(observation, separators=(",", ":"))
        return f"id: {event_id}\nevent: autotrade-order\ndata: {payload}\n\n".encode()

    def _autotrade_events(self) -> None:
        """Replay durable observations and stream later local projection writes."""
        store = getattr(self.server, "order_store")
        observations = list(reversed(store.observation_orders()))
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            for index, observation in enumerate(observations):
                if autotrade_event_id(observation) == last_event_id:
                    observations = observations[index + 1:]
                    break
        hub = getattr(self.server, "autotrade_event_hub")
        listener = hub.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # This is one long-lived stream, not an HTTP keep-alive connection.
        # Once the browser leaves it, BaseHTTPRequestHandler must not try to
        # parse a second request from a reset SSE socket.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 3000\n\n")
            for observation in observations:
                self.wfile.write(self._sse_event(observation))
            self.wfile.flush()
            while True:
                try:
                    observation = listener.get(timeout=1)
                    self.wfile.write(self._sse_event(observation))
                except Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            hub.unsubscribe(listener)

    def _intent(self) -> Intent:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 8_192:
            raise ValueError("invalid request body size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("intent body must be an object")
        source = Source(value["source"])
        # Browser-originated traffic is a manual audit ingress only.  A
        # client must never be able to impersonate Strategy or System Trigger
        # and thereby bypass the runtime identity boundary when a future gate
        # is armed.  Those sources call the shared coordinator in-process.
        if source is not Source.MANUAL_UI:
            raise ValueError("browser intent source must be MANUAL_UI")
        return self._with_idempotency_key(Intent(
            source=source,
            account_id=str(value["account_id"]),
            symbol=str(value["symbol"]),
            side=str(value["side"]),
            quantity=int(value["quantity"]),
            limit_price=str(value["limit_price"]),
            strategy_id=str(value["strategy_id"]) if value.get("strategy_id") else None,
            run_id=str(value["run_id"]) if value.get("run_id") else None,
            order_price_type=str(value.get("order_price_type", "LMT")).upper(),
        ))

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
        return self._with_idempotency_key(Intent(
            source=Source.MANUAL_UI,
            account_id=str(account["account_id"]),
            symbol=str(contract["code"]),
            side=str(order["action"]).upper(),
            quantity=int(order["quantity"]),
            limit_price=str(order["price"]),
            order_price_type=str(order.get("price_type", "LMT")).upper(),
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {"contract": contract, "stock_order" if stock_order else "futures_order": order},
            },
        ))

    def _manual_combo_order_intent(self) -> Intent:
        """Map an upstream combo request while retaining its leg identity."""
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 65_536:
            raise ValueError("invalid combo order body size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("combo order body must be an object")
        combo_contract = value.get("combo_contract")
        order = value.get("order")
        if not isinstance(combo_contract, dict) or not isinstance(order, dict):
            raise ValueError("combo contract and order are required")
        legs = combo_contract.get("legs")
        if not isinstance(legs, list) or not legs or not all(isinstance(leg, dict) and leg.get("code") for leg in legs):
            raise ValueError("combo legs are required")
        account = order.get("account")
        if not isinstance(account, dict):
            raise ValueError("combo order account is required")
        return self._with_idempotency_key(Intent(
            source=Source.MANUAL_UI,
            account_id=str(account["account_id"]),
            # The individual legs are retained below; COMBO is only the
            # aggregate risk symbol used by the current common policy seam.
            symbol="COMBO",
            side=str(order["action"]).upper(),
            quantity=int(order["quantity"]),
            limit_price=str(order["price"]),
            order_price_type=str(order.get("price_type", "LMT")).upper(),
            instrument={"kind": "COMBO", "legs": legs},
            broker_request={
                "path": "/api/v1/order/place_comboorder",
                "body": {"combo_contract": combo_contract, "order": order},
            },
        ))

    def _manual_cancel_intent(self, *, combo: bool) -> Intent:
        return self._manual_target_order_intent(
            "CANCEL",
            (),
            broker_path="/api/v1/order/cancel_comboorder" if combo else "/api/v1/order/cancel_order",
        )

    def _manual_target_order_intent(
        self,
        operation: str,
        change_fields: tuple[str, ...],
        *,
        broker_path: str | None = None,
    ) -> Intent:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 8_192:
            raise ValueError("invalid target order body size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("target order body must be an object")
        target_order_id = value.get("trade_id")
        if not isinstance(target_order_id, str) or not target_order_id:
            raise ValueError("trade_id is required")
        changes: dict[str, object] = {}
        for field in change_fields:
            if field not in value:
                raise ValueError(f"{field} is required")
            changes[field] = value[field]
        return self._with_idempotency_key(Intent(
            source=Source.MANUAL_UI,
            account_id="SIM-UNVERIFIED",
            symbol="CANCEL",
            side="CANCEL",
            quantity=0,
            limit_price="0",
            operation=operation,
            target_order_id=target_order_id,
            operation_payload=changes or None,
            broker_request={
                "path": broker_path or {
                    "CANCEL": "/api/v1/order/cancel_order",
                    "UPDATE_PRICE": "/api/v1/order/update_price",
                    "UPDATE_QUANTITY": "/api/v1/order/update_qty",
                }[operation],
                "body": value,
            },
        ))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        if path == "/api/v1/autotrade/health":
            self._json(HTTPStatus.OK, health_document(getattr(self.server, "strategy_runtime", None)))
            return
        if path == "/api/v1/autotrade/overview":
            self._json(
                HTTPStatus.OK,
                overview_document(
                    getattr(self.server, "order_store"),
                    getattr(self.server, "strategy_runtime", None),
                    getattr(self.server, "order_coordinator", None),
                ),
            )
            return
        if path == "/api/v1/autotrade/strategies":
            self._json(HTTPStatus.OK, {"items": getattr(self.server, "strategy_runtime").observations()})
            return
        if path.startswith("/api/v1/autotrade/strategies/"):
            strategy_id = path.removeprefix("/api/v1/autotrade/strategies/")
            strategies = getattr(self.server, "strategy_runtime").observations()
            strategy = next((item for item in strategies if item.get("strategy_id") == strategy_id), None)
            if strategy is None:
                self._json(HTTPStatus.NOT_FOUND, {"message": "strategy not found"})
            else:
                self._json(HTTPStatus.OK, strategy)
            return
        if path == "/api/v1/autotrade/signals":
            # Signals are immutable store projections.  No process-memory
            # event list is exposed, so a restart cannot fabricate activity.
            self._json(HTTPStatus.OK, {
                "items": getattr(self.server, "order_store").observation_signals(),
            })
            return
        if path == "/api/v1/autotrade/intents":
            self._json(
                HTTPStatus.OK,
                {"items": getattr(self.server, "order_store").observation_orders()},
            )
            return
        if path.startswith("/api/v1/autotrade/intents/"):
            self._durable_observation_detail(path.removeprefix("/api/v1/autotrade/intents/"))
            return
        if path == "/api/v1/autotrade/orders":
            # The order projection intentionally comes from the same durable
            # intent journal, so a browser cannot receive a broker status
            # that is detached from its source, risk verdict, or correlation.
            self._json(
                HTTPStatus.OK,
                {"items": getattr(self.server, "order_store").observation_orders()},
            )
            return
        if path.startswith("/api/v1/autotrade/orders/"):
            self._durable_observation_detail(path.removeprefix("/api/v1/autotrade/orders/"))
            return
        if path == "/api/v1/autotrade/runs":
            self._json(
                HTTPStatus.OK,
                {"items": strategy_runs_document(getattr(self.server, "order_store"))},
            )
            return
        if path == "/api/v1/autotrade/decisions":
            # Every durable intent is also a durable risk decision.  This
            # endpoint is intentionally a projection only: it cannot retry,
            # alter a verdict, or fabricate a signal/callback.
            self._json(
                HTTPStatus.OK,
                {"items": getattr(self.server, "order_store").observation_orders()},
            )
            return
        if path == "/api/v1/autotrade/reconciliation":
            store = getattr(self.server, "order_store")
            observations = store.observation_orders()
            self._json(
                HTTPStatus.OK,
                {
                    "items": [
                        *reconciliation_document(store, observations),
                    ],
                },
            )
            return
        if path == AUTOTRADE_EVENTS_PATH:
            self._autotrade_events()
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

    def _durable_observation_detail(self, intent_id: str) -> None:
        """Return an immutable detail projection without a sidecar lookup."""
        observation = next(
            (item for item in getattr(self.server, "order_store").observation_orders()
             if item.get("intent_id") == intent_id),
            None,
        )
        if observation is None:
            self._json(HTTPStatus.NOT_FOUND, {"message": "intent not found"})
        else:
            self._json(HTTPStatus.OK, observation)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        strategy_runtime = getattr(self.server, "strategy_runtime")
        if path == "/api/v1/autotrade/strategies/reference-fixture/validate":
            self._json(HTTPStatus.OK, strategy_runtime.validate_document())
            return
        if path == "/api/v1/autotrade/strategies/reference-fixture/dry-run":
            self._json(HTTPStatus.OK, strategy_runtime.dry_run_document())
            return
        if path in _DISABLED_STRATEGY_CONTROL_PATHS:
            # Keep denial server-side even when a stale browser renders an
            # enabled control.  This route must not instantiate a runtime,
            # create an intent, or contact the sidecar.
            self._json(HTTPStatus.FORBIDDEN, {"message": "strategy runtime is disabled"})
            return
        if path in {"/api/v1/order/cancel_order", "/api/v1/order/cancel_comboorder"}:
            try:
                intent = self._manual_cancel_intent(combo=path == "/api/v1/order/cancel_comboorder")
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid cancel order"})
                return
            self._accept_or_reuse_intent(intent)
            return
        target_operation = {
            "/api/v1/order/update_price": ("UPDATE_PRICE", ("price",)),
            "/api/v1/order/update_qty": ("UPDATE_QUANTITY", ("quantity",)),
        }.get(path)
        if target_operation:
            try:
                intent = self._manual_target_order_intent(*target_operation)
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid target order mutation"})
                return
            self._accept_or_reuse_intent(intent)
            return
        if path == "/api/v1/order/place_comboorder":
            try:
                intent = self._manual_combo_order_intent()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid combo order"})
                return
            self._accept_or_reuse_intent(intent)
            return
        if path == "/api/v1/order/place_order":
            try:
                intent = self._manual_place_order_intent()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid manual order"})
                return
            self._accept_or_reuse_intent(intent)
            return
        if path == "/api/v1/autotrade/intents":
            try:
                intent = self._intent()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid autotrade intent"})
                return
            self._accept_or_reuse_intent(intent)
            return
        if path == COMBO_CONTRACT_BUILD_PATH:
            try:
                combo_body = self._combo_contract_query_body()
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid futures combo contract query"})
                return
            self._proxy_read_query(combo_body)
            return
        if (
            path in READ_QUERY_POST_PATHS
            or path == TRADE_CALLBACK_SUBSCRIPTION_PATH
            or is_allowed_market_stream_post(path)
        ):
            try:
                body = self._read_query_body()
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid read query"})
                return
            self._proxy_read_query(body)
            return
        if is_allowed_watchlist_mutation(path, self.command):
            try:
                watchlist_body = self._watchlist_mutation_body()
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"message": "invalid watchlist mutation"})
                return
            self._proxy_watchlist_mutation(watchlist_body)
            return
        self._json(HTTPStatus.FORBIDDEN, {"message": "Autotrade mutation is disabled"})

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def create_server(
    host: str = "127.0.0.1",
    port: int = 18082,
    upstream_origin: str | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    # An observer disconnect must never keep the Gateway alive during a
    # restart.  Each SSE request has its own cleanup path and is read-only.
    server.daemon_threads = True
    resolved_upstream_origin = upstream_origin or os.getenv("SHIOAJI_HTTP_ORIGIN") or "http://127.0.0.1:21322"
    server.upstream_origin = resolved_upstream_origin.rstrip("/")  # type: ignore[attr-defined]
    database_url = os.getenv("AUTOTRADE_GATEWAY_DATABASE_URL")
    if database_url:
        from .postgres_intent_store import PostgresIntentStore

        store: IntentStore = PostgresIntentStore(
            database_url,
            os.getenv("AUTOTRADE_GATEWAY_PG_SCHEMA", "autotrade_gateway"),
        )
    else:
        store = SQLiteIntentStore(os.getenv("AUTOTRADE_GATEWAY_DB", ":memory:"))
    # A process crash or restart cannot prove that a leased/pending broker
    # delivery did not reach the broker.  Recover before serving any route so
    # no worker could ever treat the stale outbox record as retryable.
    store.recover()
    server.order_store = store  # type: ignore[attr-defined]
    server.order_coordinator = OrderCoordinator(store, gate=Gate.DISARMED)  # type: ignore[attr-defined]
    server.strategy_runtime = StrategyRuntime(server.order_coordinator)  # type: ignore[attr-defined]
    server.autotrade_event_hub = AutotradeEventHub()  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    running_server = create_server()
    try:
        running_server.serve_forever()
    finally:
        getattr(running_server, "order_store").close()
