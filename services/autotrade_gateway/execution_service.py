"""One stoppable execution owner, shared by manual and strategy ingress.

PAPER is an in-process broker with no network capability. SIM uses the pinned
loopback sidecar, requires a process opt-in and a separate short-lived arm.
Neither transport obtains credentials or creates a Shioaji SDK session.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
from typing import Any, Callable, Mapping, Protocol, cast
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
    futures_root,
    now,
    risk_quantity,
)
from .simulation_snapshot import (
    ContractRef,
    SimulationFuturesAccount,
    SimulationSnapshotProvider,
)
from .strategy_plugins import PluginStrategyRuntime, StrategyPlugin
from .worker import DispatchRefused, ExecutionWorker


class Transport(Protocol):
    mode: str
    account_id: str
    broker_id: str
    futures_account_id: str | None
    multipliers: dict[str, Decimal]

    @property
    def account_ids(self) -> tuple[str, ...]: ...
    def broker_id_for(self, account_id: str) -> str | None: ...
    def resolve_contract(self, symbol: str) -> ContractRef | None: ...
    def context(self, intent: Intent) -> RiskContext | None: ...
    def dispatch(self, intent: Intent) -> str: ...
    def trades(self) -> list[dict]: ...


class SidecarTransport:
    """Stock (S) account always; a futures (F) account only when verified.

    Futures stay unavailable unless the SIM session exposes exactly one
    verified F account (or the configured one), so a stock-only API key can
    never route a futures order to an unrelated account.
    """

    mode = "SIM"

    def __init__(
        self,
        origin: str,
        account_id: str | None = None,
        futures_account_id: str | None = None,
    ) -> None:
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
        futures = self.provider.simulation_futures_accounts()
        if futures_account_id:
            futures = tuple(
                account for account in futures if account.account_id == futures_account_id
            )
            if len(futures) != 1:
                raise ValueError("Selected SIM futures account is not available")
        self.futures_account: SimulationFuturesAccount | None = (
            futures[0] if len(futures) == 1 else None
        )
        self.futures_account_id = (
            self.futures_account.account_id if self.futures_account else None
        )
        self.multipliers: dict[str, Decimal] = dict(RiskLimits().futures_multipliers)
        self.dispatcher = ShioajiHttpDispatcher(origin)
        self.authorize: Callable[[], None] = lambda: None
        self._contracts: dict[str, ContractRef] = {}

    @property
    def account_ids(self) -> tuple[str, ...]:
        return (self.account_id,) + (
            (self.futures_account_id,) if self.futures_account_id else ()
        )

    def broker_id_for(self, account_id: str) -> str | None:
        if account_id == self.account_id:
            return self.broker_id
        if self.futures_account and account_id == self.futures_account.account_id:
            return self.futures_account.broker_id
        return None

    def resolve_contract(self, symbol: str) -> ContractRef | None:
        if symbol not in self._contracts:
            contract = self.provider.contract(symbol)
            if contract is None:
                return None
            self._contracts[symbol] = contract
        return self._contracts[symbol]

    def _verified(self, account_id: str) -> bool:
        if account_id == self.account_id:
            return self.account in self.provider.simulation_stock_accounts()
        if self.futures_account and account_id == self.futures_account.account_id:
            return self.futures_account in self.provider.simulation_futures_accounts()
        return False

    def context(self, intent: Intent) -> RiskContext | None:
        if not self._verified(intent.account_id):
            return None
        contract = self.resolve_contract(intent.symbol)
        if contract is None:
            return None
        if intent.account_id == self.account_id:
            if contract.security_type != "STK":
                return None
            return self.provider.stock_risk_context(
                self.account, intent.symbol, exchange=contract.exchange
            )
        multiplier = self.multipliers.get(futures_root(intent.symbol) or "")
        if (
            contract.security_type != "FUT"
            or multiplier is None
            or self.futures_account is None
        ):
            return None
        return self.provider.futures_risk_context(
            self.futures_account, contract, multiplier=multiplier
        )

    def dispatch(self, intent: Intent) -> str:
        # Check environment again at the mutation boundary, not just boot.
        if not self._verified(intent.account_id):
            raise ValueError("verified SIM account is no longer available")
        self.authorize()
        return self.dispatcher.dispatch(intent)

    def trades(self) -> list[dict]:
        accounts = [("S", self.account.broker_id, self.account.account_id)]
        if self.futures_account is not None:
            accounts.append(
                ("F", self.futures_account.broker_id, self.futures_account.account_id)
            )
        rows: list[dict] = []
        for account_type, broker_id, account_id in accounts:
            if not self._verified(account_id):
                raise ValueError("verified SIM session is unavailable")
            result = self.provider._request(
                "/api/v1/order/trades",
                {
                    "account_type": account_type,
                    "broker_id": broker_id,
                    "account_id": account_id,
                },
            )
            if not isinstance(result, list) or any(
                not isinstance(row, dict) for row in result
            ):
                raise ValueError("broker trade snapshot unavailable")
            rows.extend(result)
        return rows


def _symbol_list(value: str | None) -> tuple[str, ...]:
    symbols = tuple(dict.fromkeys(item.strip().upper() for item in (value or "").split(",") if item.strip()))
    if any(not symbol.isalnum() or len(symbol) > 12 for symbol in symbols):
        raise ValueError("symbol lists must contain alphanumeric codes only")
    return symbols


def risk_limits_from_environment(environment: Mapping[str, str]) -> RiskLimits:
    """Operator-tunable SIM/PAPER limits. Accounts are never configurable here.

    ``AUTOTRADE_CB_SYMBOLS`` lists convertible bonds that may trade; they are
    allowed and use the longer last-trade age because they print rarely.
    Invalid values refuse to start the Gateway instead of falling back.
    """
    base = RiskLimits(cooldown_seconds=2)
    cb = _symbol_list(environment.get("AUTOTRADE_CB_SYMBOLS"))
    extra = _symbol_list(environment.get("AUTOTRADE_ALLOWED_SYMBOLS"))

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = environment.get(name)
        if raw is None or raw == "":
            return default
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value

    return replace(
        base,
        allowed_symbols=tuple(dict.fromkeys((*base.allowed_symbols, *extra, *cb))),
        slow_quote_symbols=cb,
        board_lot_only_symbols=cb,
        slow_quote_max_age_seconds=integer("AUTOTRADE_SLOW_QUOTE_MAX_AGE_SECONDS", 60, 5, 3600),
        max_position_per_symbol=integer(
            "AUTOTRADE_MAX_POSITION_PER_SYMBOL", base.max_position_per_symbol, 1, 1_000_000
        ),
        max_futures_order_contracts=integer("AUTOTRADE_MAX_FUTURES_ORDER_CONTRACTS", 1, 1, 10),
        max_futures_position=integer("AUTOTRADE_MAX_FUTURES_POSITION", 2, 1, 20),
    )


MAX_DISPATCHES_PER_TICK = 20

# Broker trade statuses meaning the broker has received the order. PendingSubmit
# (sent, not yet confirmed) and Inactive are deliberately not acknowledgements.
BROKER_ACKNOWLEDGED = frozenset({"Submitted", "PreSubmitted", "PartFilled"})

PAPER_CONTRACTS: dict[str, ContractRef] = {
    "2330": ContractRef("STK", "TSE", "2330"),
    "2317": ContractRef("STK", "TSE", "2317"),
    "0050": ContractRef("STK", "TSE", "0050"),
    "99991": ContractRef("STK", "OTC", "99991"),
    "TXFR1": ContractRef("FUT", "TAIFEX", "TXFR1"),
}


class PaperTransport:
    """A clearly labelled local fill simulator. Prices are synthetic.

    Like the broker, stocks/CBs and futures live in separate accounts.
    Synthetic prices can be moved in PAPER mode only, to exercise strategy
    triggers without a market.
    """

    mode = "PAPER"
    account_id = "PAPER-LOCAL"
    futures_account_id: str | None = "PAPER-FUT"
    broker_id = "LOCAL"

    def __init__(
        self,
        store: IntentStore,
        prices: dict[str, Decimal] | None = None,
        cb_symbols: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.rows: dict[str, dict] = {}
        # Convertible bonds configured at launch (AUTOTRADE_CB_SYMBOLS) are
        # OTC stock contracts; they start at a synthetic price of 100.
        self.contracts = dict(PAPER_CONTRACTS)
        for code in cb_symbols:
            self.contracts.setdefault(code, ContractRef("STK", "OTC", code))
        self.multipliers: dict[str, Decimal] = dict(RiskLimits().futures_multipliers)
        self.prices = {
            "2330": Decimal("1000"),
            "2317": Decimal("200"),
            "0050": Decimal("100"),
            "99991": Decimal("100"),
            "TXFR1": Decimal("48700"),
        }
        for code in cb_symbols:
            self.prices.setdefault(code, Decimal("100"))
        for symbol, price in (prices or {}).items():
            self.set_price(symbol, price)

    @property
    def account_ids(self) -> tuple[str, ...]:
        return (self.account_id, self.futures_account_id or "")

    def broker_id_for(self, account_id: str) -> str | None:
        return self.broker_id if account_id in self.account_ids else None

    def resolve_contract(self, symbol: str) -> ContractRef | None:
        return self.contracts.get(symbol)

    def set_price(self, symbol: str, price: Decimal) -> None:
        if symbol not in self.contracts or not price.is_finite() or price <= 0:
            raise ValueError("unknown PAPER symbol or invalid synthetic price")
        self.prices[symbol] = price

    def _book(
        self, account_id: str
    ) -> tuple[dict[str, tuple[int, Decimal]], dict[str, Decimal], dict[str, Decimal], Decimal]:
        """Rebuild cost basis from immutable executions, including after restart.

        This is explicitly an estimate without commission or tax. Futures are
        keyed by product root and valued with the contract multiplier.
        """
        futures = account_id == self.futures_account_id
        balances: dict[str, tuple[int, Decimal]] = {}
        marks: dict[str, Decimal] = {}
        multipliers: dict[str, Decimal] = {}
        realized = Decimal("0")
        today = now().astimezone(ZoneInfo("Asia/Taipei")).date()
        for fill in reversed(self.store.observation_fills()):
            order = self.store.load_intent(str(fill["intent_id"]))
            if order.account_id != account_id or order.operation != "PLACE":
                continue
            key = (futures_root(order.symbol) or order.symbol) if futures else order.symbol
            multiplier = (
                self.multipliers.get(futures_root(order.symbol) or "", Decimal("1"))
                if futures
                else Decimal("1")
            )
            amount = int(str(fill["quantity"])) * (1 if order.side == "BUY" else -1)
            price = Decimal(str(fill["price"]))
            marks[key] = self.prices.get(order.symbol, price)
            multipliers[key] = multiplier
            quantity, average = balances.get(key, (0, Decimal("0")))
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
                        * multiplier
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
            balances[key] = (remaining, average)
        return balances, marks, multipliers, realized

    @property
    def position_snapshots(self) -> tuple[PositionSnapshot, ...]:
        observed = now()
        snapshots: list[PositionSnapshot] = []
        for account_type, account_id in (("S", self.account_id), ("F", self.futures_account_id)):
            if not account_id:
                continue
            balances, marks, multipliers, _ = self._book(account_id)
            snapshots.extend(
                PositionSnapshot(
                    snapshot_id=f"paper:{uuid4()}",
                    account_type=account_type,
                    account_id=account_id,
                    symbol=key,
                    side="LONG" if quantity > 0 else "SHORT",
                    quantity=str(abs(quantity)),
                    average_price=str(average),
                    market_price=str(marks[key]),
                    unrealized_pnl=str(
                        Decimal(quantity) * (marks[key] - average) * multipliers[key]
                    ),
                    observed_at=observed,
                )
                for key, (quantity, average) in balances.items()
                if quantity
            )
        return tuple(snapshots)

    def context(self, intent: Intent) -> RiskContext | None:
        contract = self.resolve_contract(intent.symbol)
        if contract is None or intent.symbol not in self.prices:
            return None
        expected = (
            self.futures_account_id if contract.security_type == "FUT" else self.account_id
        )
        if intent.account_id != expected:
            return None
        balances, marks, multipliers, realized = self._book(intent.account_id)
        positions = {
            key: RiskPosition(quantity, marks[key])
            for key, (quantity, _) in balances.items()
        }
        unrealized = sum(
            (
                Decimal(quantity) * (marks[key] - average) * multipliers[key]
                for key, (quantity, average) in balances.items()
            ),
            Decimal("0"),
        )
        return RiskContext(
            self.prices[intent.symbol], now(), positions, realized, unrealized
        )

    def dispatch(self, intent: Intent) -> str:
        if intent.account_id not in self.account_ids:
            raise ValueError("paper account required")
        if intent.operation != "PLACE":
            target = self.rows.get(intent.target_order_id or "")
            if target is None:
                raise ValueError("paper target is unavailable")
            changes = intent.operation_payload or {}
            if intent.operation == "CANCEL":
                # Like Shioaji SIM: order_quantity keeps the ordered size and
                # the cancelled part is reported separately.
                target["status"]["status"] = "Cancelled"
                target["status"]["cancel_quantity"] = target["_intent"].quantity
            elif intent.operation == "UPDATE_PRICE":
                target["status"]["modified_price"] = changes["price"]
            elif intent.operation == "UPDATE_QUANTITY":
                target["status"]["order_quantity"] = changes["quantity"]
                target["status"]["cancel_quantity"] = (
                    target["_intent"].quantity - changes["quantity"]
                )
            return str(target["order"]["id"])
        identity = f"PAPER-{uuid4()}"
        body = cast(dict[str, Any], (intent.broker_request or {}).get("body", {}))
        if isinstance(body.get("futures_order"), dict):
            kind = {"oc_type": body["futures_order"].get("octype", "Auto")}
        else:
            kind = {
                "order_lot": body.get("stock_order", {}).get(
                    "order_lot", "Common" if intent.broker_request else "IntradayOdd"
                )
            }
        self.rows[identity] = {
            "contract": {"code": intent.symbol},
            "order": {
                "id": identity,
                "quantity": intent.quantity,
                "price": intent.limit_price,
                "action": intent.side.title(),
                **kind,
                "account": {"account_id": intent.account_id, "broker_id": self.broker_id},
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
        limits: RiskLimits | None = None,
        strategy_plugin: StrategyPlugin | None = None,
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
        base = limits or RiskLimits(cooldown_seconds=2)
        # Accounts come only from the verified transport, never from config.
        transport.multipliers = dict(base.futures_multipliers)
        self.coordinator = OrderCoordinator(
            store,
            gate=store.trading_gate(),
            risk=RiskEngine(replace(base, allowed_accounts=transport.account_ids)),
            risk_context_provider=transport.context,
        )
        self.worker = ExecutionWorker(store, self)
        self.strategy: MarketStrategyRuntime | PluginStrategyRuntime
        if strategy_plugin is not None:
            self.strategy = PluginStrategyRuntime(self.coordinator, strategy_plugin, transport)
        else:
            self.strategy = MarketStrategyRuntime(
                self.coordinator,
                account_id=transport.account_id,
                broker_id=transport.broker_id,
            )

    def dispatch(self, intent: Intent) -> str:
        # Revalidate the actual pending order, after all potentially slow reads.
        evaluated = self.coordinator.evaluated_intent(intent)
        context = self.coordinator.with_reservations(
            evaluated,
            self.transport.context(evaluated) if intent.operation != "CANCEL" else None,
        )
        try:
            self._require_authority()
        except ValueError as error:
            raise DispatchRefused(("execution_authority_expired",)) from error
        verdict = self.coordinator.risk.evaluate(
            evaluated, self.coordinator.gate, context
        )
        if not verdict.allowed:
            # Raised before any broker I/O: the order is rejected, not UNKNOWN.
            raise DispatchRefused(verdict.reasons)
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
            context = self.strategy.on_tick()
            if self.coordinator.gate is Gate.ARMED:
                # At most one in-flight order per product; different products
                # may be sent in the same tick.
                for _ in range(MAX_DISPATCHES_PER_TICK):
                    if not self.worker.dispatch_once(self.coordinator.dispatch_ready):
                        break
            else:
                # Still queued, never handed to the broker: definitely not
                # sent, so they are rejected rather than marked UNKNOWN.
                for intent_id in self.store.pending_outbox_ids():
                    self.store.mark_dispatch_refused(intent_id, ("dispatch_gate_closed",))
            if now().timestamp() - self._last_account_snapshot >= 10:
                if isinstance(self.transport, PaperTransport):
                    snapshots = self.transport.position_snapshots
                    self.store.record_position_snapshots(snapshots)
                    self.store.record_audit_event(
                        None,
                        "PAPER_POSITION_VIEW",
                        {
                            "snapshot_ids": [
                                snapshot.snapshot_id for snapshot in snapshots
                            ],
                            "observed_at": now().isoformat(),
                        },
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
                        # Stock-account position value (shares x price), as
                        # used by the gross exposure limit.
                        "gross_exposure": str(
                            context.gross_exposure
                            if context.gross_exposure is not None
                            else sum(
                                (
                                    abs(position.quantity) * position.market_price
                                    for position in context.positions.values()
                                ),
                                Decimal("0"),
                            )
                        ),
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
            if intent.account_id not in self.transport.account_ids:
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
                    # Only the broker's own acknowledgement makes an order
                    # SUBMITTED; PendingSubmit keeps the next same-product
                    # order queued.
                    target: Status | None = (
                        Status.PART_FILLED
                        if deals or row["status"] == "PART_FILLED"
                        else Status.SUBMITTED
                        if status.get("status") in BROKER_ACKNOWLEDGED or row["status"] == "SUBMITTED"
                        else None
                    )
                    if target is not None and row["status"] != target.value:
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
        code = trade.get("contract", {}).get("code")
        if (
            not isinstance(account, dict)
            or account.get("account_id") != intent.account_id
            or account.get("broker_id") != self.transport.broker_id_for(intent.account_id)
            or not self._same_contract(intent.symbol, code)
            or order.get("action", "").upper() != expected_side
        ):
            raise ValueError("broker account or instrument mismatch")
        if intent.operation != "PLACE":
            return
        body = cast(dict[str, Any], (intent.broker_request or {}).get("body", {}))
        if isinstance(body.get("futures_order"), dict):
            if "order_lot" in order:
                raise ValueError("broker lot mismatch")
        else:
            expected_lot = body.get("stock_order", {}).get(
                "order_lot", "Common" if intent.broker_request else "IntradayOdd"
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
        # Shioaji SIM keeps order_quantity at the ordered size after a cancel
        # (1 ordered, 1 cancelled); a remaining-size report (0 ordered,
        # 1 cancelled) is accepted too. Either way the cancelled part alone
        # never exceeds the order, which is checked again with fills below.
        if (
            type(effective_quantity) is not int
            or type(cancelled) is not int
            or effective_quantity < 0
            or cancelled < 0
            or cancelled > intent.quantity
            or (
                status.get("status") != "Cancelled"
                and effective_quantity not in quantities
            )
            or (
                effective_quantity + cancelled > intent.quantity
                and effective_quantity != intent.quantity
            )
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
        if total + cancelled > intent.quantity:
            raise ValueError("cancelled and filled quantity exceed the order")
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

    def _same_contract(self, symbol: str, code: object) -> bool:
        """A rolling futures alias (TXFR1) may be reported as its month code."""
        if code == symbol:
            return True
        if not isinstance(code, str) or not symbol.endswith(("R1", "R2")):
            return False
        contract = self.transport.resolve_contract(symbol)
        return contract is not None and contract.target_code == code

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
