"""One stoppable execution owner, shared by manual and strategy ingress.

PAPER is an in-process broker with no network capability. SIM uses the pinned
loopback sidecar, requires a process opt-in and a separate short-lived arm.
Neither transport obtains credentials or creates a Shioaji SDK session.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
from typing import Any, Callable, Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from .broker_http import ShioajiHttpDispatcher
from .market_strategy import MarketStrategyRuntime
from .order_core import (
    Fill,
    Gate,
    Intent,
    IntentStore,
    OrderCoordinator,
    RiskContext,
    RiskEngine,
    RiskLimits,
    RiskPosition,
    PositionSnapshot,
    Status,
    now,
    risk_quantity,
)
from .simulation_snapshot import SimulationSnapshotProvider
from .worker import ExecutionWorker


class Transport(Protocol):
    mode: str
    account_id: str
    broker_id: str

    def context(self, intent: Intent) -> RiskContext | None: ...
    def dispatch(self, intent: Intent) -> str: ...
    def trades(self) -> list[dict]: ...


class SidecarTransport:
    mode = "SIM"

    def __init__(self, origin: str, account_id: str | None = None) -> None:
        self.provider = SimulationSnapshotProvider(origin)
        accounts = self.provider.simulation_stock_accounts()
        if account_id:
            accounts = tuple(
                account for account in accounts if account.account_id == account_id
            )
        if len(accounts) != 1:
            raise ValueError("Select exactly one verified SIM stock account")
        self.account = accounts[0]
        self.account_id, self.broker_id = (
            self.account.account_id,
            self.account.broker_id,
        )
        self.dispatcher = ShioajiHttpDispatcher(origin)
        self.authorize: Callable[[], None] = lambda: None

    def context(self, intent: Intent) -> RiskContext | None:
        if intent.account_id != self.account_id:
            return None
        if self.account not in self.provider.simulation_stock_accounts():
            return None
        return self.provider.stock_risk_context(self.account, intent.symbol)

    def dispatch(self, intent: Intent) -> str:
        # Check environment again at the mutation boundary, not just boot.
        if (
            intent.account_id != self.account_id
            or self.account not in self.provider.simulation_stock_accounts()
        ):
            raise ValueError("verified SIM account is no longer available")
        self.authorize()
        return self.dispatcher.dispatch(intent)

    def trades(self) -> list[dict]:
        if self.account not in self.provider.simulation_stock_accounts():
            raise ValueError("verified SIM session is unavailable")
        rows = self.provider._request(
            "/api/v1/order/trades",
            {
                "account_type": "S",
                "broker_id": self.broker_id,
                "account_id": self.account_id,
            },
        )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("broker trade snapshot unavailable")
        return rows


class PaperTransport:
    """A clearly labelled local fill simulator. Prices are synthetic."""

    mode = "PAPER"
    account_id = "PAPER-LOCAL"
    broker_id = "LOCAL"

    def __init__(self, store: IntentStore) -> None:
        self.store = store
        self.rows: dict[str, dict] = {}
        self.position_snapshots: tuple[PositionSnapshot, ...] = ()
        self.prices = {
            "2330": Decimal("1000"),
            "2317": Decimal("200"),
            "0050": Decimal("100"),
        }

    def context(self, intent: Intent) -> RiskContext | None:
        if intent.account_id != self.account_id or intent.symbol not in self.prices:
            return None
        # Rebuild paper cost basis from immutable executions, including after
        # restart. This is explicitly an estimate without commission or tax.
        balances: dict[str, tuple[int, Decimal]] = {}
        realized = Decimal("0")
        today = now().astimezone(ZoneInfo("Asia/Taipei")).date()
        for fill in reversed(self.store.observation_fills()):
            order = self.store.load_intent(str(fill["intent_id"]))
            if order.account_id != self.account_id or order.operation != "PLACE":
                continue
            amount = int(str(fill["quantity"])) * (1 if order.side == "BUY" else -1)
            price = Decimal(str(fill["price"]))
            quantity, average = balances.get(order.symbol, (0, Decimal("0")))
            if quantity and (quantity > 0) != (amount > 0):
                closed = min(abs(quantity), abs(amount))
                if (
                    datetime.fromisoformat(str(fill["filled_at"]))
                    .astimezone(ZoneInfo("Asia/Taipei"))
                    .date()
                    == today
                ):
                    realized += (
                        Decimal(closed)
                        * (price - average)
                        * (1 if quantity > 0 else -1)
                    )
                remaining = quantity + amount
                average = (
                    price
                    if remaining and (remaining > 0) != (quantity > 0)
                    else average
                )
            else:
                remaining = quantity + amount
                average = (abs(quantity) * average + abs(amount) * price) / abs(
                    remaining
                )
            balances[order.symbol] = (remaining, average)
        positions = {
            symbol: RiskPosition(quantity, self.prices[symbol])
            for symbol, (quantity, _) in balances.items()
        }
        unrealized = sum(
            (
                Decimal(quantity) * (self.prices[symbol] - average)
                for symbol, (quantity, average) in balances.items()
            ),
            Decimal("0"),
        )
        observed = now()
        self.position_snapshots = tuple(
            PositionSnapshot(
                snapshot_id=f"paper:{uuid4()}",
                account_type="S",
                account_id=self.account_id,
                symbol=symbol,
                side="LONG" if quantity > 0 else "SHORT",
                quantity=str(abs(quantity)),
                average_price=str(average),
                market_price=str(self.prices[symbol]),
                unrealized_pnl=str(Decimal(quantity) * (self.prices[symbol] - average)),
                observed_at=observed,
            )
            for symbol, (quantity, average) in balances.items()
            if quantity
        )
        return RiskContext(
            self.prices[intent.symbol], observed, positions, realized, unrealized
        )

    def dispatch(self, intent: Intent) -> str:
        if intent.account_id != self.account_id:
            raise ValueError("paper account required")
        if intent.operation != "PLACE":
            target = self.rows.get(intent.target_order_id or "")
            if target is None:
                raise ValueError("paper target is unavailable")
            changes = intent.operation_payload or {}
            if intent.operation == "CANCEL":
                target["status"]["status"] = "Cancelled"
                target["status"]["cancel_quantity"] = target["_intent"].quantity
                target["status"]["order_quantity"] = 0
            elif intent.operation == "UPDATE_PRICE":
                target["status"]["modified_price"] = changes["price"]
            elif intent.operation == "UPDATE_QUANTITY":
                target["status"]["order_quantity"] = changes["quantity"]
                target["status"]["cancel_quantity"] = (
                    target["_intent"].quantity - changes["quantity"]
                )
            return str(target["order"]["id"])
        identity = f"PAPER-{uuid4()}"
        self.rows[identity] = {
            "contract": {"code": intent.symbol},
            "order": {
                "id": identity,
                "quantity": intent.quantity,
                "price": intent.limit_price,
                "action": intent.side.title(),
                "order_lot": cast(
                    dict[str, Any], (intent.broker_request or {}).get("body", {})
                )
                .get("stock_order", {})
                .get("order_lot", "Common" if intent.broker_request else "IntradayOdd"),
                "account": {"account_id": self.account_id, "broker_id": self.broker_id},
            },
            "status": {
                "id": identity,
                "status": "Submitted",
                "deals": [],
                "order_quantity": intent.quantity,
                "cancel_quantity": 0,
                "modified_price": 0,
            },
            "_intent": intent,
        }
        return identity

    def trades(self) -> list[dict]:
        for row in self.rows.values():
            intent = row["_intent"]
            price = self.prices[intent.symbol]
            limit = Decimal(
                str(row["status"].get("modified_price") or row["order"]["price"])
            )
            crosses = intent.order_price_type != "LMT" or (
                price <= limit if intent.side == "BUY" else price >= limit
            )
            if row["status"]["status"] == "Submitted" and crosses:
                row["status"].update(
                    status="Filled",
                    deals=[
                        {
                            "seq": f"{row['order']['id']}-fill",
                            "price": str(price),
                            "quantity": row["status"].get(
                                "order_quantity", row["order"]["quantity"]
                            ),
                            "ts": now().timestamp(),
                        }
                    ],
                )
        return list(self.rows.values())


class ExecutionService:
    def __init__(
        self,
        store: IntentStore,
        transport: Transport,
        publish: Callable[[dict], None],
        *,
        allow_execution: bool = False,
        interval: float = 2,
    ) -> None:
        self.store, self.transport, self.publish = store, transport, publish
        self.allow_execution, self.interval = allow_execution, interval
        self.ownership_check: Callable[[], None] = lambda: None
        if isinstance(transport, SidecarTransport):
            transport.authorize = self._require_authority
        self.stopping = Event()
        self.thread: Thread | None = None
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.arm_expires_at: float = 0
        self._last_account_snapshot: float = 0
        self.latest_trades: dict[str, dict] = {}
        previous_modes = {
            event["details"].get("mode")
            for event in store.observation_audit_events()
            if event["event_type"] == "EXECUTION_RUNTIME_STARTED"
            and isinstance(event["details"], dict)
        }
        if previous_modes - {transport.mode} or (
            not previous_modes and store.observation_orders()
        ):
            raise ValueError(
                "Use a dedicated journal for this runtime mode; preserve existing history"
            )
        store.record_audit_event(
            None, "EXECUTION_RUNTIME_STARTED", {"mode": transport.mode}
        )
        self.coordinator = OrderCoordinator(
            store,
            gate=store.trading_gate(),
            risk=RiskEngine(
                RiskLimits(allowed_accounts=(transport.account_id,), cooldown_seconds=2)
            ),
            risk_context_provider=transport.context,
        )
        self.worker = ExecutionWorker(store, self)
        self.strategy = MarketStrategyRuntime(
            self.coordinator,
            account_id=transport.account_id,
            broker_id=transport.broker_id,
        )

    def dispatch(self, intent: Intent) -> str:
        # Revalidate the actual pending order, after all potentially slow reads.
        evaluated = self.coordinator.evaluated_intent(intent)
        context = (
            self.transport.context(evaluated) if intent.operation != "CANCEL" else None
        )
        self._require_authority()
        verdict = self.coordinator.risk.evaluate(
            evaluated, self.coordinator.gate, context
        )
        if not verdict.allowed:
            raise ValueError("dispatch risk preflight rejected")
        return self.transport.dispatch(intent)

    def _require_authority(self) -> None:
        self.ownership_check()
        if (
            self.store.trading_gate() is Gate.KILLED
            or self.coordinator.gate is not Gate.ARMED
            or now().timestamp() >= self.arm_expires_at
        ):
            self.coordinator.gate = self.store.trading_gate()
            raise ValueError("execution authority expired")

    def document(self) -> dict:
        return {
            "mode": self.transport.mode,
            "execution_available": self.allow_execution,
            "running": bool(self.thread and self.thread.is_alive()),
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "trading_gate": self.coordinator.gate.value,
            "arm_expires_at": self.arm_expires_at or None,
            "strategy_id": self.strategy.strategy_id,
        }

    def control(self, action: str, reason: str, key: str) -> dict:
        with self.coordinator.lock:
            if not reason.strip() or not key or action not in {"arm", "disarm"}:
                raise ValueError(
                    "execution control requires action, reason and idempotency key"
                )
            old = self.store.strategy_control_receipt(key)
            if old:
                if (
                    old["strategy_id"] != "execution"
                    or old["action"] != action
                    or old["reason"] != reason
                ):
                    raise ValueError("idempotency key collision")
                return {**self.document(), "applied": False}
            if action == "arm":
                if not self.allow_execution or self.store.trading_gate() is Gate.KILLED:
                    raise ValueError("execution unavailable or kill switch active")
                if any(
                    row["status"]
                    in {
                        "UNKNOWN",
                        "RECONCILING",
                        "MANUAL_REVIEW",
                        "APPROVED",
                        "SENDING",
                    }
                    for row in self.store.observation_orders()
                ):
                    raise ValueError("resolve uncertain deliveries before arming")
                context = self.strategy._context()
                if context is None or not self.strategy._fresh(context):
                    raise ValueError("fresh market and account facts are required")
                self.transport.trades()  # prove readback before allowing a dispatch
                self.arm_expires_at = now().timestamp() + 300
                self.coordinator.gate = Gate.ARMED
            else:
                self.coordinator.gate = self.store.trading_gate()
                self.arm_expires_at = 0
            receipt = self.document()
            self.store.record_strategy_control(
                key, "execution", action, reason, receipt
            )
            self.store.record_audit_event(
                None,
                "EXECUTION_CONTROL",
                {"action": action, "reason": reason, "mode": self.transport.mode},
            )
            return {**receipt, "applied": True}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = Thread(target=self._run, name="autotrade-execution", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError(
                    "execution owner has not stopped; journal must remain open"
                )
        self.coordinator.gate = self.store.trading_gate()

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as error:
                # No raw upstream exception/account content in UI or logs.
                self.coordinator.gate = self.store.trading_gate()
                self.last_error = f"runtime_unavailable:{type(error).__name__}"
            self.stopping.wait(self.interval)

    def tick(self) -> None:
        with self.coordinator.lock:
            self.ownership_check()
            if self.store.trading_gate() is Gate.KILLED:
                self.coordinator.gate = Gate.KILLED
            if (
                self.coordinator.gate is Gate.ARMED
                and now().timestamp() >= self.arm_expires_at
            ):
                self.coordinator.gate = Gate.DISARMED
            before = {
                str(row["intent_id"]): row for row in self.store.observation_orders()
            }
            try:
                self._apply_trades(self.transport.trades())
            except Exception:
                self.coordinator.gate = self.store.trading_gate()
                self.latest_trades = {}
                for row in self.store.observation_orders():
                    if row["status"] in {"PENDING_SUBMIT", "SUBMITTED", "PART_FILLED"}:
                        self.store.mark_delivery_unknown(
                            str(row["intent_id"]), "broker_readback_unavailable"
                        )
                raise
            context = self.strategy._context()
            if context is None:
                raise ValueError("market/account snapshot unavailable")
            self.strategy.on_market(context)
            if self.coordinator.gate is Gate.ARMED:
                self.worker.dispatch_once()
            else:
                for row in self.store.observation_orders():
                    if self.store.outbox_state(str(row["intent_id"])) == "PENDING":
                        self.store.mark_delivery_unknown(
                            str(row["intent_id"]), "dispatch_gate_closed"
                        )
            if now().timestamp() - self._last_account_snapshot >= 10:
                if isinstance(self.transport, PaperTransport):
                    self.store.record_position_snapshots(
                        self.transport.position_snapshots
                    )
                self.store.record_audit_event(
                    None,
                    "ACCOUNT_PNL_SNAPSHOT",
                    {
                        "realized_pnl": str(context.realized_pnl)
                        if context.realized_pnl is not None
                        else None,
                        "unrealized_pnl": str(context.unrealized_pnl)
                        if context.unrealized_pnl is not None
                        else None,
                        "source": "PAPER_ESTIMATE"
                        if self.transport.mode == "PAPER"
                        else "BROKER_ACCOUNT",
                        "scope": "ACCOUNT",
                        "observed_at": now().isoformat(),
                    },
                )
                self._last_account_snapshot = now().timestamp()
            for row in self.store.observation_orders():
                if before.get(str(row["intent_id"])) != row:
                    self.publish(row)
            self.last_tick, self.last_error = now().isoformat(), None

    def _apply_trades(self, trades: list[dict]) -> None:
        by_id: dict[str, dict] = {}
        for candidate in trades:
            order, status = candidate.get("order"), candidate.get("status")
            if (
                not isinstance(order, dict)
                or not isinstance(status, dict)
                or not order.get("id")
                or order["id"] != status.get("id")
            ):
                raise ValueError("invalid broker identity")
            identity = str(order["id"])
            if identity in by_id:
                raise ValueError("duplicate broker identity")
            by_id[identity] = deepcopy(candidate)
        # Publish cached readback only after every matched fact validates.
        for row in self.store.observation_orders():
            trade = by_id.get(str(row.get("broker_order_id")))
            if trade is not None:
                intent = self.store.load_intent(str(row["intent_id"]))
                self._validate_trade(intent, trade)
        self.latest_trades = by_id
        for row in self.store.observation_orders():
            identity = str(row.get("broker_order_id") or "")
            trade = by_id.get(str(identity)) if identity else None
            if trade is None:
                if row["status"] in {"PENDING_SUBMIT", "SUBMITTED", "PART_FILLED"}:
                    age = (
                        now() - datetime.fromisoformat(str(row["updated_at"]))
                    ).total_seconds()
                    if age > 30:
                        self.store.mark_delivery_unknown(
                            str(row["intent_id"]), "broker_readback_missing"
                        )
                continue
            intent_id = str(row["intent_id"])
            intent = self.store.load_intent(intent_id)
            if intent.account_id != self.transport.account_id:
                continue
            status = trade["status"]
            terminal = {
                "Filled": Status.FILLED,
                "Cancelled": Status.CANCELLED,
                "Failed": Status.BROKER_REJECTED,
            }.get(status.get("status"))
            if intent.operation != "PLACE":
                changes = intent.operation_payload or {}
                completed = (
                    intent.operation == "CANCEL"
                    and status.get("status") == "Cancelled"
                    or intent.operation == "UPDATE_PRICE"
                    and Decimal(
                        str(status.get("modified_price") or trade["order"].get("price"))
                    )
                    == Decimal(str(changes.get("price")))
                    or intent.operation == "UPDATE_QUANTITY"
                    and status.get("order_quantity", trade["order"].get("quantity"))
                    == changes.get("quantity")
                )
                terminal = (
                    Status.COMPLETED
                    if completed
                    else Status.BROKER_REJECTED
                    if terminal
                    else None
                )
            else:
                multiplier = risk_quantity(intent) // intent.quantity
                deals = status.get("deals")
                if not isinstance(deals, list):
                    raise ValueError("broker deals unavailable")
                for deal in deals:
                    if (
                        not isinstance(deal, dict)
                        or not deal.get("seq")
                        or type(deal.get("quantity")) is not int
                        or deal["quantity"] <= 0
                    ):
                        raise ValueError("invalid broker fill")
                    self.store.record_fill(
                        Fill(
                            f"{identity}:{deal['seq']}",
                            intent_id,
                            str(identity),
                            deal["quantity"] * multiplier,
                            str(deal["price"]),
                            datetime.fromtimestamp(float(deal["ts"]), timezone.utc),
                        )
                    )
                if terminal is Status.FILLED and not deals:
                    raise ValueError("filled status has no executions")
                if not terminal and row["status"] in {
                    "PENDING_SUBMIT",
                    "SUBMITTED",
                    "PART_FILLED",
                }:
                    target = (
                        Status.PART_FILLED
                        if deals or row["status"] == "PART_FILLED"
                        else Status.SUBMITTED
                    )
                    if row["status"] != target.value:
                        self.store.transition(intent_id, target)
            if terminal and row["status"] in {"UNKNOWN", "RECONCILING"}:
                self.store.record_reconciled_terminal(
                    intent_id, str(identity), terminal
                )
            elif terminal and row["status"] in {
                "PENDING_SUBMIT",
                "SUBMITTED",
                "PART_FILLED",
            }:
                self.store.record_terminal_callback(intent_id, str(identity), terminal)

    def _validate_trade(self, intent: Intent, trade: dict) -> None:
        order, status = trade["order"], trade["status"]
        account = order.get("account", {})
        expected_side = intent.side
        if intent.operation != "PLACE":
            original = next(
                (
                    row
                    for row in self.store.observation_orders()
                    if row["broker_order_id"] == intent.target_order_id
                    and self.store.load_intent(str(row["intent_id"])).operation
                    == "PLACE"
                ),
                None,
            )
            if original is None or intent.target_order_id != order["id"]:
                raise ValueError("command target is not bound")
            expected_side = self.store.load_intent(str(original["intent_id"])).side
        if (
            not isinstance(account, dict)
            or account.get("account_id") != intent.account_id
            or account.get("broker_id") != self.transport.broker_id
            or trade.get("contract", {}).get("code") != intent.symbol
            or order.get("action", "").upper() != expected_side
        ):
            raise ValueError("broker account or instrument mismatch")
        if intent.operation != "PLACE":
            return
        expected_lot = (
            cast(dict[str, Any], (intent.broker_request or {}).get("body", {}))
            .get("stock_order", {})
            .get("order_lot", "Common" if intent.broker_request else "IntradayOdd")
        )
        if order.get("order_lot") != expected_lot:
            raise ValueError("broker lot mismatch")
        quantities = {intent.quantity}
        for row in self.store.observation_orders():
            command = self.store.load_intent(str(row["intent_id"]))
            if (
                command.target_order_id == order["id"]
                and command.operation == "UPDATE_QUANTITY"
                and row["status"] in {"PENDING_SUBMIT", "SUBMITTED", "COMPLETED"}
            ):
                quantity = (command.operation_payload or {}).get("quantity")
                if type(quantity) is int:
                    quantities.add(quantity)
        if (
            type(order.get("quantity")) is not int
            or order["quantity"] not in quantities
        ):
            raise ValueError("unapproved broker quantity")
        effective_quantity = status.get("order_quantity", order["quantity"])
        cancelled = status.get("cancel_quantity", 0)
        if (
            type(effective_quantity) is not int
            or type(cancelled) is not int
            or effective_quantity < 0
            or cancelled < 0
            or (
                status.get("status") != "Cancelled"
                and effective_quantity not in quantities
            )
            or effective_quantity + cancelled > intent.quantity
        ):
            raise ValueError("inconsistent broker quantity snapshot")
        deals = status.get("deals")
        if not isinstance(deals, list):
            raise ValueError("broker executions unavailable")
        seen: set[str] = set()
        total = 0
        for deal in deals:
            if (
                not isinstance(deal, dict)
                or not deal.get("seq")
                or str(deal["seq"]) in seen
                or type(deal.get("quantity")) is not int
                or deal["quantity"] <= 0
            ):
                raise ValueError("invalid or duplicate broker fill")
            seen.add(str(deal["seq"]))
            price = Decimal(str(deal.get("price")))
            timestamp = datetime.fromtimestamp(float(deal["ts"]), timezone.utc)
            if not price.is_finite() or price <= 0 or timestamp > now():
                raise ValueError("invalid fill price or timestamp")
            total += deal["quantity"]
        multiplier = risk_quantity(intent) // intent.quantity
        persisted = [
            fill
            for fill in self.store.observation_fills()
            if fill["intent_id"] == intent.intent_id
        ]
        combined = {
            str(fill["fill_id"]): int(str(fill["quantity"])) for fill in persisted
        }
        combined.update(
            {
                f"{order['id']}:{deal['seq']}": deal["quantity"] * multiplier
                for deal in deals
            }
        )
        if (
            sum(combined.values()) > intent.quantity * multiplier
            or total > intent.quantity
        ):
            raise ValueError("excess broker executions")
        if status.get("status") == "Filled" and total != effective_quantity:
            raise ValueError("incomplete filled snapshot")

    def trade_document(self, intent_id: str) -> dict:
        row = next(
            (
                item
                for item in self.store.observation_orders()
                if item["intent_id"] == intent_id
            ),
            None,
        )
        if row is None:
            raise KeyError(intent_id)
        trade = self.latest_trades.get(str(row.get("broker_order_id")))
        # Only broker-confirmed identities; an accepted intent is not a Trade.
        public_trade = (
            None
            if (
                trade is None
                or (
                    self.store.load_intent(intent_id).operation != "PLACE"
                    and row["status"] != "COMPLETED"
                )
            )
            else {
                "contract": trade["contract"],
                "order": {
                    key: value
                    for key, value in trade["order"].items()
                    if key
                    in {
                        "id",
                        "seqno",
                        "ordno",
                        "action",
                        "price",
                        "quantity",
                        "order_type",
                        "price_type",
                        "order_lot",
                        "octype",
                        "custom_field",
                    }
                },
                "status": trade["status"],
            }
        )
        return {"intent_id": intent_id, "status": row["status"], "trade": public_trade}
