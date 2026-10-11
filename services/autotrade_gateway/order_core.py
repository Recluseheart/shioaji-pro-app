"""Durable, broker-free order spine for the Autotrade Gateway.

The HTTP handler intentionally does not expose this as a mutation endpoint
yet.  This module makes the invariant testable before an Execution Gateway
worker and Shioaji HTTP adapter are connected: every request is attributed,
risk-evaluated and persisted before it could ever reach a broker.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Protocol
from uuid import uuid4


def now() -> datetime:
    return datetime.now(timezone.utc)


class Source(str, Enum):
    MANUAL_UI = "MANUAL_UI"
    STRATEGY = "STRATEGY"
    SYSTEM_TRIGGER = "SYSTEM_TRIGGER"


class Gate(str, Enum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    KILLED = "KILLED"


class Status(str, Enum):
    INTENT_CREATED = "INTENT_CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PART_FILLED = "PART_FILLED"
    COMPLETED = "COMPLETED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    BROKER_REJECTED = "BROKER_REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class DuplicateIntentError(Exception):
    """A durable intent id was already recorded by a concurrent retry."""


_UNCERTAIN = {
    Status.APPROVED,
    Status.SENDING,
    Status.PENDING_SUBMIT,
    Status.SUBMITTED,
    Status.PART_FILLED,
}

# Orders whose unfilled remainder still reserves risk capacity.
_OPEN_ORDER_STATUSES = frozenset({
    *(status.value for status in _UNCERTAIN), Status.UNKNOWN.value, Status.RECONCILING.value,
})

# Sent (or possibly sent) but not yet acknowledged by the broker; the next
# order for the same product waits. MANUAL_REVIEW also blocks the product.
_AWAITING_BROKER_ACK = frozenset({
    Status.SENDING.value, Status.PENDING_SUBMIT.value, Status.UNKNOWN.value,
    Status.RECONCILING.value, Status.MANUAL_REVIEW.value,
})


@dataclass(frozen=True)
class Intent:
    source: Source
    account_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: str
    strategy_id: str | None = None
    run_id: str | None = None
    order_price_type: str = "LMT"
    instrument: dict[str, object] | None = None
    operation: str = "PLACE"
    target_order_id: str | None = None
    # Only the durable store may set this after resolving target_order_id to a
    # still-operable, broker-bound intent. Browser request fields never decide
    # whether a cancel/replace target belongs to the verified SIM account.
    target_order_verified: bool = False
    operation_payload: dict[str, object] | None = None
    # The normalized sidecar request is durable execution evidence, never a
    # browser read-model field. It is only read by the future worker owner.
    broker_request: dict[str, object] | None = None
    intent_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=now)


@dataclass(frozen=True)
class StrategySignal:
    """An immutable strategy observation linked to its durable intent."""

    signal_id: str
    strategy_id: str
    run_id: str
    event_id: str
    signal_type: str
    symbol: str
    side: str
    intent_id: str
    correlation_id: str
    status: Status
    risk_reasons: tuple[str, ...]
    observed_at: datetime


@dataclass(frozen=True)
class StrategyRun:
    """A durable, non-broker lifecycle record for one strategy invocation.

    The initial state is deliberately restricted to completed test fixtures.
    Production start/pause/resume/stop remains disabled until a separately
    authorized runner owns the lifecycle and heartbeat source.
    """

    run_id: str
    strategy_id: str
    version: str
    config_checksum: str
    state: str
    last_intent_id: str | None
    last_status: Status | None
    intent_count: int
    started_at: datetime
    heartbeat_at: datetime | None
    ended_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class StrategyDefinition:
    """Durable metadata for a registered strategy, never a runner control."""

    strategy_id: str
    version: str
    config_checksum: str
    state: str
    enabled: bool
    allowed_symbols: tuple[str, ...]


@dataclass(frozen=True)
class Fill:
    """One immutable broker-confirmed execution fact for a bound order."""

    fill_id: str
    intent_id: str
    broker_order_id: str
    quantity: int
    price: str
    filled_at: datetime


@dataclass(frozen=True)
class PositionSnapshot:
    """One immutable, broker-read position fact.

    Account identifiers are retained only in the local durable store so a
    later risk/reconciliation worker can bind facts to the right account.
    Observer/API projections must mask them before they leave the process.
    """

    snapshot_id: str
    account_type: str
    account_id: str
    symbol: str
    side: str
    quantity: str
    average_price: str
    market_price: str
    unrealized_pnl: str
    observed_at: datetime


def position_snapshot_values(snapshot: PositionSnapshot) -> tuple[str, ...]:
    """Validate and canonically serialize a broker-read position snapshot."""
    text_fields = {
        "snapshot_id": snapshot.snapshot_id,
        "account_type": snapshot.account_type,
        "account_id": snapshot.account_id,
        "symbol": snapshot.symbol,
    }
    if any(not isinstance(value, str) or not value or len(value) > 128 for value in text_fields.values()):
        raise ValueError("position snapshot identity is invalid")
    if snapshot.side not in {"LONG", "SHORT"}:
        raise ValueError("position snapshot side is invalid")
    if snapshot.observed_at.tzinfo is None:
        raise ValueError("position snapshot timestamp has no timezone")

    def decimal_text(
        value: str, field_name: str, *, positive: bool = False, signed: bool = False,
    ) -> str:
        try:
            decimal = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"position snapshot {field_name} is invalid") from error
        if not decimal.is_finite() or (decimal <= 0 if positive else not signed and decimal < 0):
            raise ValueError(f"position snapshot {field_name} is invalid")
        return format(decimal.normalize(), "f")

    return (
        snapshot.snapshot_id,
        snapshot.account_type,
        snapshot.account_id,
        snapshot.symbol,
        snapshot.side,
        decimal_text(snapshot.quantity, "quantity", positive=True),
        decimal_text(snapshot.average_price, "average_price"),
        decimal_text(snapshot.market_price, "market_price"),
        decimal_text(snapshot.unrealized_pnl, "unrealized_pnl", signed=True),
        snapshot.observed_at.isoformat(),
    )


def intent_from_persisted_payload(payload_json: str) -> Intent:
    """Reconstruct the exact durable request for the execution owner.

    An execution worker must dispatch the immutable intent that Risk Engine
    accepted, not a browser-shaped retry or a reconstructed approximation.
    Corrupt persistence is intentionally an error: the worker will mark its
    claimed delivery UNKNOWN rather than inventing fields and sending it.
    """
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("stored intent payload is invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("stored intent payload is not an object")

    def text(field_name: str) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"stored intent {field_name} is invalid")
        return value

    def optional_text(field_name: str) -> str | None:
        value = payload.get(field_name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"stored intent {field_name} is invalid")
        return value

    def optional_object(field_name: str) -> dict[str, object] | None:
        value = payload.get(field_name)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"stored intent {field_name} is invalid")
        return value

    target_order_verified = payload.get("target_order_verified", False)
    if not isinstance(target_order_verified, bool):
        raise ValueError("stored intent target verification is invalid")

    quantity = payload.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise ValueError("stored intent quantity is invalid")
    try:
        source = Source(text("source"))
        created_at = datetime.fromisoformat(text("created_at"))
    except ValueError as error:
        raise ValueError("stored intent source or timestamp is invalid") from error
    if created_at.tzinfo is None:
        raise ValueError("stored intent timestamp has no timezone")
    return Intent(
        source=source,
        account_id=text("account_id"),
        symbol=text("symbol"),
        side=text("side"),
        quantity=quantity,
        limit_price=text("limit_price"),
        strategy_id=optional_text("strategy_id"),
        run_id=optional_text("run_id"),
        order_price_type=text("order_price_type"),
        instrument=optional_object("instrument"),
        operation=text("operation"),
        target_order_id=optional_text("target_order_id"),
        target_order_verified=target_order_verified,
        operation_payload=optional_object("operation_payload"),
        broker_request=optional_object("broker_request"),
        intent_id=text("intent_id"),
        correlation_id=text("correlation_id"),
        created_at=created_at,
    )


_REQUEST_IDENTITY_EXCLUSIONS = frozenset({"intent_id", "correlation_id", "created_at"})


def intent_request_fingerprint(intent: Intent) -> str:
    """Return the stable semantic request identity used for safe retries.

    Transport retries receive a deterministic intent id from their
    ``Idempotency-Key``.  The id itself, correlation id, and creation time
    must not decide whether a later request is the same mutation; every
    broker-relevant field must.  Keeping this canonical representation beside
    ``Intent`` makes the SQLite and PostgreSQL audit stores enforce identical
    retry semantics.
    """
    payload = asdict(intent)
    payload["source"] = intent.source.value
    for field_name in _REQUEST_IDENTITY_EXCLUSIONS:
        payload.pop(field_name, None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def persisted_request_matches(intent: Intent, payload_json: str) -> bool:
    """Compare an incoming intent to a stored payload without exposing it."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    for field_name in _REQUEST_IDENTITY_EXCLUSIONS:
        payload.pop(field_name, None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) == intent_request_fingerprint(intent)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reasons: tuple[str, ...] = ()


class IntentStore(Protocol):
    """Durable intent/outbox capabilities shared by SQLite and PostgreSQL."""

    def persist(self, intent: Intent, verdict: Verdict) -> Status: ...
    def status(self, intent_id: str) -> Status: ...
    def has_intent(self, intent_id: str) -> bool: ...
    def request_matches(self, intent: Intent) -> bool: ...
    def load_intent(self, intent_id: str) -> Intent: ...
    def verified_target_intent(self, broker_order_id: str) -> Intent | None: ...
    def outbox_state(self, intent_id: str) -> str | None: ...
    def transition(self, intent_id: str, target: Status) -> None: ...
    def recover(self) -> int: ...
    def pending_outbox_ids(self) -> list[str]: ...
    def claim_outbox(self, intent_id: str | None = None) -> str | None: ...
    def mark_delivery_unknown(self, intent_id: str, reason: str) -> None: ...
    def mark_dispatch_refused(self, intent_id: str, reasons: tuple[str, ...]) -> None: ...
    def mark_pending_submit(self, intent_id: str, broker_order_id: str) -> None: ...
    def mark_operation_pending(self, intent_id: str, broker_order_id: str) -> None: ...
    def record_terminal_callback(
        self, intent_id: str, broker_order_id: str, terminal: Status,
    ) -> None: ...
    def record_reconciled_terminal(
        self, intent_id: str, broker_order_id: str, terminal: Status,
    ) -> None: ...
    def reconciliation_refresh_receipt(self, idempotency_key: str) -> dict[str, object] | None: ...
    def begin_reconciliation_refresh(self, idempotency_key: str, intent_id: str, reason: str) -> bool: ...
    def complete_reconciliation_refresh(
        self, idempotency_key: str, intent_id: str, reason: str, receipt: dict[str, object],
    ) -> None: ...
    def observation_orders(self) -> list[dict[str, object]]: ...
    def observation_risk_decisions(self) -> list[dict[str, object]]: ...
    def observation_audit_events(self) -> list[dict[str, object]]: ...
    def record_audit_event(
        self, intent_id: str | None, event_type: str, details: dict[str, object],
    ) -> None: ...
    def trading_gate(self) -> Gate: ...
    def apply_kill_switch(self, reason: str, idempotency_key: str) -> tuple[Gate, str, bool]: ...
    def record_fill(self, fill: Fill) -> None: ...
    def observation_fills(self) -> list[dict[str, object]]: ...
    def record_position_snapshot(self, snapshot: PositionSnapshot) -> None: ...
    def record_position_snapshots(self, snapshots: tuple[PositionSnapshot, ...]) -> None: ...
    def observation_position_snapshots(self) -> list[dict[str, object]]: ...
    def record_signal(self, signal: StrategySignal) -> None: ...
    def observation_signals(self) -> list[dict[str, object]]: ...
    def record_strategy_definition(self, definition: StrategyDefinition) -> None: ...
    def set_strategy_definition_state(self, strategy_id: str, state: str, enabled: bool) -> None: ...
    def strategy_control_receipt(self, idempotency_key: str) -> dict[str, object] | None: ...
    def record_strategy_control(self, idempotency_key: str, strategy_id: str, action: str, reason: str, receipt: dict[str, object]) -> bool: ...
    def observation_strategy_definitions(self) -> list[dict[str, object]]: ...
    def record_strategy_run(self, run: StrategyRun) -> None: ...
    def start_strategy_lifecycle(self, run: StrategyRun) -> None: ...
    def transition_strategy_run(self, run_id: str, state: str, observed_at: datetime) -> None: ...
    def transition_strategy_lifecycle(
        self, run_id: str, strategy_id: str, state: str, enabled: bool, observed_at: datetime,
    ) -> None: ...
    def record_strategy_heartbeat(self, run_id: str, observed_at: datetime) -> None: ...
    def record_strategy_intent_result(
        self, run_id: str, intent_id: str, status: Status, observed_at: datetime,
    ) -> None: ...
    def observation_strategy_runs(self) -> list[dict[str, object]]: ...
    def observation_reconciliation_cases(self) -> list[dict[str, object]]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class RiskLimits:
    """Portable simulation limits; dynamic checks require a fresh context."""

    allowed_accounts: tuple[str, ...] = ("SIM-001",)
    allowed_symbols: tuple[str, ...] = ("2330", "2317", "0050", "11011", "TXFR1")
    max_order_quantity: int = 10_000
    max_order_notional: Decimal = Decimal("5000000")
    max_position_per_symbol: int = 2_000
    max_gross_exposure: Decimal = Decimal("10000000")
    max_daily_loss: Decimal = Decimal("100000")
    max_quote_age_seconds: int = 5
    max_price_deviation: Decimal = Decimal("0.10")
    max_orders_per_minute: int = 30
    cooldown_seconds: float = 0
    # Thinly traded symbols (e.g. convertible bonds) may print only every few
    # seconds; their last-trade time is checked against a longer, explicit age.
    slow_quote_symbols: tuple[str, ...] = ()
    slow_quote_max_age_seconds: int = 60
    # Convertible bonds have no odd-lot market; odd-lot orders are refused.
    board_lot_only_symbols: tuple[str, ...] = ()
    # Futures are limited by contract count and contract value, never by the
    # share-based stock limits above. Only explicitly allowed roots may trade.
    allowed_futures_roots: tuple[str, ...] = ("TXF",)
    futures_multipliers: tuple[tuple[str, Decimal], ...] = (("TXF", Decimal("200")),)
    max_futures_order_contracts: int = 1
    max_futures_position: int = 2
    max_futures_notional: Decimal = Decimal("25000000")

    def document(self) -> dict[str, object]:
        return {
            "allowed_accounts": list(self.allowed_accounts),
            "allowed_symbols": list(self.allowed_symbols),
            "max_order_quantity": self.max_order_quantity,
            "max_order_notional": str(self.max_order_notional),
            "max_position_per_symbol": self.max_position_per_symbol,
            "max_gross_exposure": str(self.max_gross_exposure),
            "max_daily_loss": str(self.max_daily_loss),
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_price_deviation": str(self.max_price_deviation),
            "max_orders_per_minute": self.max_orders_per_minute,
            "cooldown_seconds": self.cooldown_seconds,
            "slow_quote_symbols": list(self.slow_quote_symbols),
            "slow_quote_max_age_seconds": self.slow_quote_max_age_seconds,
            "board_lot_only_symbols": list(self.board_lot_only_symbols),
            "allowed_futures_roots": list(self.allowed_futures_roots),
            "futures_multipliers": {root: str(value) for root, value in self.futures_multipliers},
            "max_futures_order_contracts": self.max_futures_order_contracts,
            "max_futures_position": self.max_futures_position,
            "max_futures_notional": str(self.max_futures_notional),
        }

    def futures_multiplier(self, root: str | None) -> Decimal | None:
        return dict(self.futures_multipliers).get(root or "")


_FUTURES_CODE = re.compile(r"([A-Z]{3})(?:[A-L][0-9]|R[12])")


def futures_root(symbol: str) -> str | None:
    """Return the product root of a monthly (TXFJ6) or rolling (TXFR1) code."""
    match = _FUTURES_CODE.fullmatch(symbol or "")
    return match.group(1) if match else None


@dataclass(frozen=True)
class InstrumentProfile:
    """Risk units for one intent: shares for stocks/CBs, contracts for futures."""

    kind: str
    units: int
    multiplier: Decimal = Decimal("1")
    root: str | None = None
    odd_lot: bool = False

    @property
    def is_futures(self) -> bool:
        return self.kind == "FUTURES"

    def position_key(self, symbol: str) -> str:
        # All months of one futures product share a single position limit.
        return self.root if self.is_futures and self.root else symbol


def instrument_profile(intent: Intent, limits: RiskLimits | None = None) -> InstrumentProfile:
    """Map the immutable broker request to risk units; unknown shapes fail.

    Domain-only intents are already expressed in shares. HTTP stock requests
    carry their lot type (one board lot is 1,000 shares or 1,000 CB units);
    futures requests are counted in contracts with the product multiplier.
    """
    if isinstance(intent.quantity, bool) or not isinstance(intent.quantity, int):
        raise ValueError("quantity must be an integer")
    request = intent.broker_request
    if request is None:
        return InstrumentProfile("STOCK", intent.quantity)
    body = request.get("body")
    if not isinstance(body, dict):
        raise ValueError("missing broker body")
    stock, futures = body.get("stock_order"), body.get("futures_order")
    if isinstance(stock, dict) and futures is None:
        if stock.get("quantity") != intent.quantity:
            raise ValueError("broker quantity differs from intent")
        lot = stock.get("order_lot", "Common")
        if lot in {"Common", "Fixing"}:
            return InstrumentProfile("STOCK", intent.quantity * 1000)
        if lot in {"Odd", "IntradayOdd"}:
            return InstrumentProfile("STOCK", intent.quantity, odd_lot=True)
        raise ValueError("unsupported stock lot")
    if isinstance(futures, dict) and stock is None:
        if futures.get("quantity") != intent.quantity:
            raise ValueError("broker quantity differs from intent")
        contract = body.get("contract")
        if not isinstance(contract, dict) or contract.get("code") != intent.symbol:
            raise ValueError("futures contract differs from intent")
        if contract.get("security_type", "FUT") != "FUT":
            raise ValueError("futures order requires a futures contract")
        root = futures_root(intent.symbol)
        multiplier = (limits or RiskLimits()).futures_multiplier(root)
        if multiplier is None:
            raise ValueError("unsupported futures product")
        return InstrumentProfile("FUTURES", intent.quantity, multiplier, root)
    raise ValueError("instrument risk model unavailable")


def risk_quantity(intent: Intent) -> int:
    """Shares for stock intents and contracts for futures intents."""
    return instrument_profile(intent).units


class RiskEngine:
    """Fail-closed simulation risk policy shared by manual and strategy paths."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        intent: Intent,
        gate: Gate,
        context: RiskContext | None = None,
    ) -> Verdict:
        reasons: list[str] = []
        if gate is not Gate.ARMED:
            reasons.append("trading_gate_not_armed")
        if intent.operation == "PLACE":
            profile: InstrumentProfile | None
            try:
                profile = instrument_profile(intent, self.limits)
                quantity = profile.units
            except ValueError:
                profile = None
                quantity = 0
                reasons.append("instrument_risk_unavailable")
            futures = profile is not None and profile.is_futures
            # Account numbers do not encode their environment.  The caller
            # must populate this allowlist only from a verified simulation
            # sidecar discovery; a cosmetic `SIM-` prefix is not evidence.
            if intent.account_id not in self.limits.allowed_accounts:
                reasons.append("simulation_account_not_allowed")
            if not intent.symbol or not intent.symbol.isalnum():
                reasons.append("invalid_symbol")
            elif futures:
                assert profile is not None
                if profile.root not in self.limits.allowed_futures_roots:
                    reasons.append("symbol_not_allowed")
            elif intent.symbol not in self.limits.allowed_symbols and intent.symbol != "COMBO":
                reasons.append("symbol_not_allowed")
            if intent.side not in {"BUY", "SELL"}:
                reasons.append("invalid_side")
            quantity_limit = (
                self.limits.max_futures_order_contracts if futures else self.limits.max_order_quantity
            )
            notional_limit = self.limits.max_futures_notional if futures else self.limits.max_order_notional
            multiplier = profile.multiplier if profile else Decimal("1")
            if quantity <= 0:
                reasons.append("invalid_quantity")
            elif quantity > quantity_limit:
                reasons.append("quantity_limit_exceeded")
            if profile is not None and profile.odd_lot:
                # Odd lots are 1-999 shares per order; CBs have no odd-lot market.
                if quantity > 999:
                    reasons.append("odd_lot_quantity_exceeded")
                if intent.symbol in self.limits.board_lot_only_symbols:
                    reasons.append("odd_lot_not_supported")
            if intent.order_price_type not in {"LMT", "MKT", "MKP"}:
                reasons.append("invalid_order_price_type")
            elif intent.order_price_type == "LMT":
                try:
                    price = Decimal(intent.limit_price)
                    if not price.is_finite() or price <= 0:
                        reasons.append("invalid_limit_price")
                    elif price * quantity * multiplier > notional_limit:
                        reasons.append("notional_limit_exceeded")
                except (InvalidOperation, ValueError):
                    reasons.append("invalid_limit_price")
            if gate is Gate.ARMED and quantity > 0 and profile is not None:
                reasons.extend(self._dynamic_reasons(intent, context, profile))
        elif intent.operation in {"CANCEL", "UPDATE_PRICE", "UPDATE_QUANTITY"}:
            if not intent.target_order_id:
                reasons.append("target_order_id_required")
            if not intent.target_order_verified:
                reasons.append("target_order_account_unverified")
            changes = intent.operation_payload or {}
            if intent.operation == "UPDATE_PRICE":
                try:
                    price = Decimal(str(changes.get("price")))
                    if not price.is_finite() or price <= 0:
                        reasons.append("invalid_update_price")
                except (InvalidOperation, ValueError):
                    reasons.append("invalid_update_price")
            if intent.operation == "UPDATE_QUANTITY":
                updated_quantity = changes.get("quantity")
                if not isinstance(updated_quantity, int) or isinstance(updated_quantity, bool) or updated_quantity <= 0:
                    reasons.append("invalid_update_quantity")
        else:
            reasons.append("invalid_operation")
        if intent.source is Source.STRATEGY and (not intent.strategy_id or not intent.run_id):
            reasons.append("strategy_identity_required")
        return Verdict(not reasons, tuple(reasons))

    def _dynamic_reasons(
        self, intent: Intent, context: RiskContext | None, profile: InstrumentProfile,
    ) -> list[str]:
        """Evaluate limits that require an authoritative market/account snapshot.

        A future execution gateway must supply this snapshot from its durable
        read models immediately before it creates an outbox record.  Absence
        is deliberately a rejection rather than a permissive fallback: an
        armed process with stale data must never become a bypass around risk.
        """
        if context is None:
            return ["risk_context_unavailable"]
        limit_price: Decimal | None = None
        if intent.order_price_type == "LMT":
            try:
                limit_price = Decimal(intent.limit_price)
            except (InvalidOperation, ValueError):
                # Static validation records the user-visible reason.  Do not
                # let an invalid durable payload crash this second line of
                # defence while the Gate is ARMED.
                return []
            if not limit_price.is_finite() or limit_price <= 0:
                return []
        reasons: list[str] = []
        timestamp = now()
        quote_price = context.quote_price
        quote_at = context.quote_at
        quantity = profile.units
        multiplier = profile.multiplier
        max_quote_age = (
            self.limits.slow_quote_max_age_seconds
            if intent.symbol in self.limits.slow_quote_symbols
            else self.limits.max_quote_age_seconds
        )
        if (
            quote_price is None
            or not quote_price.is_finite()
            or quote_price <= 0
            or quote_at is None
            or quote_at.tzinfo is None
            or (timestamp - quote_at).total_seconds() < -1
            or (timestamp - quote_at).total_seconds() > max_quote_age
        ):
            reasons.append("stale_quote")
        else:
            if intent.order_price_type == "LMT":
                assert limit_price is not None
                try:
                    deviation = abs(limit_price - quote_price) / quote_price
                    if deviation > self.limits.max_price_deviation:
                        reasons.append("price_deviation_exceeded")
                except InvalidOperation:
                    reasons.append("invalid_limit_price")
            if intent.order_price_type == "LMT":
                assert limit_price is not None
                proposed_notional = Decimal(quantity) * limit_price * multiplier
            else:
                proposed_notional = Decimal(quantity) * quote_price * multiplier
            notional_limit = (
                self.limits.max_futures_notional if profile.is_futures else self.limits.max_order_notional
            )
            if proposed_notional > notional_limit:
                reasons.append("notional_limit_exceeded")
            key = profile.position_key(intent.symbol)
            current_quantity = context.positions.get(key, RiskPosition()).quantity
            # Orders that are accepted but not yet filled reserve capacity: the
            # worst case assumes every same-side pending order fills.
            reserved = context.reservations.get(key, Reservation())
            base_quantity = (
                current_quantity + reserved.buy_units if intent.side == "BUY"
                else current_quantity - reserved.sell_units
            )
            projected_quantity = base_quantity + (quantity if intent.side == "BUY" else -quantity)
            position_limit = (
                self.limits.max_futures_position if profile.is_futures else self.limits.max_position_per_symbol
            )
            if abs(projected_quantity) > position_limit:
                reasons.append("position_limit_exceeded")
            gross_exposure = context.gross_exposure if context.gross_exposure is not None else sum(
                abs(position.quantity) * position.market_price * multiplier
                for position in context.positions.values()
            )
            reserved_exposure = sum(
                (item.added_exposure(context.positions.get(name, RiskPosition()).quantity)
                 for name, item in context.reservations.items()),
                Decimal("0"),
            )
            exposure_limit = (
                self.limits.max_futures_notional if profile.is_futures else self.limits.max_gross_exposure
            )
            # Only the part of the order that grows the absolute position adds
            # exposure; a closing order must never be blocked by this limit.
            added_units = max(0, abs(projected_quantity) - abs(base_quantity))
            added_exposure = proposed_notional * Decimal(added_units) / Decimal(quantity)
            if added_units and gross_exposure + reserved_exposure + added_exposure > exposure_limit:
                reasons.append("gross_exposure_limit_exceeded")
        if context.realized_pnl is None or context.unrealized_pnl is None:
            reasons.append("pnl_unavailable")
        elif not context.realized_pnl.is_finite() or not context.unrealized_pnl.is_finite():
            reasons.append("pnl_unavailable")
        elif context.realized_pnl + context.unrealized_pnl <= -self.limits.max_daily_loss:
            reasons.append("daily_loss_limit_exceeded")
        return reasons


@dataclass(frozen=True)
class RiskPosition:
    """Sanitized position fact from the durable account read model."""

    quantity: int = 0
    market_price: Decimal = Decimal("0")


@dataclass(frozen=True)
class Reservation:
    """Unfilled units and value of accepted, still-open orders for one product."""

    buy_units: int = 0
    sell_units: int = 0
    buy_notional: Decimal = Decimal("0")
    sell_notional: Decimal = Decimal("0")

    def added_exposure(self, current_units: int) -> Decimal:
        """Value by which these orders could grow the absolute position."""
        if current_units >= 0:
            closing = min(self.sell_units, current_units)
            flip = self.sell_notional * Decimal(self.sell_units - closing) / Decimal(self.sell_units) \
                if self.sell_units else Decimal("0")
            return self.buy_notional + flip
        closing = min(self.buy_units, -current_units)
        flip = self.buy_notional * Decimal(self.buy_units - closing) / Decimal(self.buy_units) \
            if self.buy_units else Decimal("0")
        return self.sell_notional + flip


@dataclass(frozen=True)
class RiskContext:
    """Fresh, authoritative facts needed for dynamic order risk checks."""

    quote_price: Decimal | None = None
    quote_at: datetime | None = None
    positions: dict[str, RiskPosition] = field(default_factory=dict)
    realized_pnl: Decimal | None = Decimal("0")
    unrealized_pnl: Decimal | None = Decimal("0")
    gross_exposure: Decimal | None = None
    # Best bid/ask from the same snapshot; informational for strategies only.
    quote_bid: Decimal | None = None
    quote_ask: Decimal | None = None
    # Accepted but unfilled orders of the same account, keyed like positions.
    reservations: dict[str, Reservation] = field(default_factory=dict)


class SQLiteIntentStore:
    """SQLite test/dev implementation of the atomic intent and outbox contract."""

    def __init__(self, path: str | Path) -> None:
        self._lock = Lock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_intents (
                intent_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
                source TEXT NOT NULL, strategy_id TEXT, run_id TEXT,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, broker_order_id TEXT
            );
            CREATE TABLE IF NOT EXISTS execution_outbox (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS risk_decisions (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                allowed INTEGER NOT NULL, reasons_json TEXT NOT NULL, decided_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY, intent_id TEXT REFERENCES order_intents(intent_id),
                event_type TEXT NOT NULL, details_json TEXT NOT NULL, occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_events_occurred_idx
                ON audit_events(occurred_at DESC, event_id DESC);
            CREATE TABLE IF NOT EXISTS gateway_controls (
                control_name TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                broker_order_id TEXT NOT NULL, quantity INTEGER NOT NULL, price TEXT NOT NULL,
                filled_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS fills_intent_idx ON fills(intent_id, filled_at DESC, fill_id DESC);
            CREATE TABLE IF NOT EXISTS broker_position_snapshots (
                snapshot_id TEXT PRIMARY KEY, account_type TEXT NOT NULL, account_id TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
                average_price TEXT NOT NULL, market_price TEXT NOT NULL, unrealized_pnl TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(account_type, account_id, symbol, side, observed_at)
            );
            CREATE INDEX IF NOT EXISTS broker_position_snapshots_observed_idx
                ON broker_position_snapshots(observed_at DESC, snapshot_id DESC);
            CREATE TABLE IF NOT EXISTS broker_orders (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                broker_order_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_cases (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, reason TEXT NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_refresh_requests (
                idempotency_key TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                reason TEXT NOT NULL, state TEXT NOT NULL, receipt_json TEXT, applied_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS reconciliation_refresh_pending_intent_idx
                ON reconciliation_refresh_requests(intent_id) WHERE state='PENDING';
            CREATE TABLE IF NOT EXISTS strategy_signals (
                signal_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
                signal_type TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                correlation_id TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, observed_at TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(strategy_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS strategy_signals_observed_idx
                ON strategy_signals(observed_at DESC, signal_id DESC);
            CREATE TABLE IF NOT EXISTS strategy_definitions (
                strategy_id TEXT PRIMARY KEY, state TEXT NOT NULL, enabled INTEGER NOT NULL,
                allowed_symbols_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_control_requests (
                idempotency_key TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                action TEXT NOT NULL, reason TEXT NOT NULL, receipt_json TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_versions (
                strategy_id TEXT NOT NULL REFERENCES strategy_definitions(strategy_id),
                version TEXT NOT NULL, config_checksum TEXT NOT NULL,
                PRIMARY KEY(strategy_id, version)
            );
            CREATE TABLE IF NOT EXISTS strategy_runs (
                run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                version TEXT NOT NULL, config_checksum TEXT NOT NULL,
                state TEXT NOT NULL, last_intent_id TEXT REFERENCES order_intents(intent_id),
                last_status TEXT, intent_count INTEGER NOT NULL,
                started_at TEXT NOT NULL, heartbeat_at TEXT, ended_at TEXT, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS strategy_runs_updated_idx
                ON strategy_runs(updated_at DESC, run_id DESC);
            CREATE TABLE IF NOT EXISTS strategy_heartbeats (
                run_id TEXT NOT NULL REFERENCES strategy_runs(run_id),
                observed_at TEXT NOT NULL,
                PRIMARY KEY(run_id, observed_at)
            );
            """
        )
        # `CREATE TABLE IF NOT EXISTS` does not evolve a persisted development
        # database.  Keep this narrowly-scoped migration idempotent so an old
        # intent journal can expose an already-assigned broker identity without
        # rebuilding or discarding its audit history.
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(order_intents)")
        }
        if "broker_order_id" not in columns:
            self.connection.execute("ALTER TABLE order_intents ADD COLUMN broker_order_id TEXT")
        self.connection.execute(
            """INSERT OR IGNORE INTO broker_orders(intent_id, broker_order_id, created_at, updated_at)
            SELECT intent_id, broker_order_id, updated_at, updated_at FROM order_intents
            WHERE broker_order_id IS NOT NULL AND broker_order_id != ''""",
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO risk_decisions(intent_id, allowed, reasons_json, decided_at)
            SELECT intent_id, CASE WHEN status = 'RISK_REJECTED' THEN 0 ELSE 1 END,
                   risk_reasons_json, updated_at FROM order_intents""",
        )
        self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def persist(self, intent: Intent, verdict: Verdict) -> Status:
        status = Status.APPROVED if verdict.allowed else Status.RISK_REJECTED
        payload = asdict(intent)
        payload["source"] = intent.source.value
        payload["created_at"] = intent.created_at.isoformat()
        timestamp = now().isoformat()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """INSERT INTO order_intents(
                    intent_id, correlation_id, source, strategy_id, run_id, payload_json,
                    status, risk_reasons_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO NOTHING""",
                (
                    intent.intent_id, intent.correlation_id, intent.source.value,
                    intent.strategy_id, intent.run_id, json.dumps(payload, separators=(",", ":")),
                    status.value, json.dumps(verdict.reasons), intent.created_at.isoformat(), timestamp,
                ),
            )
            if cursor.rowcount != 1:
                raise DuplicateIntentError(intent.intent_id)
            self.connection.execute(
                "INSERT INTO risk_decisions(intent_id, allowed, reasons_json, decided_at) VALUES (?, ?, ?, ?)",
                (intent.intent_id, int(verdict.allowed), json.dumps(verdict.reasons), timestamp),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'INTENT_EVALUATED', ?, ?)""",
                (
                    str(uuid4()), intent.intent_id,
                    json.dumps({
                        "allowed": verdict.allowed, "risk_reasons": list(verdict.reasons),
                        "status": status.value,
                    }, separators=(",", ":")),
                    timestamp,
                ),
            )
            if verdict.allowed:
                self.connection.execute(
                    "INSERT INTO execution_outbox(intent_id, state, created_at, updated_at) VALUES (?, 'PENDING', ?, ?)",
                    (intent.intent_id, timestamp, timestamp),
                )
        return status

    def status(self, intent_id: str) -> Status:
        with self._lock:
            row = self.connection.execute("SELECT status FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return Status(row["status"])

    def has_intent(self, intent_id: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM order_intents WHERE intent_id=?", (intent_id,),
            ).fetchone()
        return row is not None

    def request_matches(self, intent: Intent) -> bool:
        """Return true only when an existing durable retry has identical input."""
        with self._lock:
            row = self.connection.execute(
                "SELECT payload_json FROM order_intents WHERE intent_id=?", (intent.intent_id,),
            ).fetchone()
        return row is not None and persisted_request_matches(intent, str(row["payload_json"]))

    def load_intent(self, intent_id: str) -> Intent:
        with self._lock:
            row = self.connection.execute(
                "SELECT payload_json FROM order_intents WHERE intent_id=?", (intent_id,),
            ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return intent_from_persisted_payload(str(row["payload_json"]))

    def verified_target_intent(self, broker_order_id: str) -> Intent | None:
        """Return a still-operable target only when its identity is durable.

        UNKNOWN, reconciling, terminal, and sidecar-only orders are never
        eligible.  A replacement operation must not treat a browser-supplied
        broker id as proof of simulation account ownership.
        """
        with self._lock:
            row = self.connection.execute(
                """SELECT intent.payload_json FROM broker_orders AS broker
                JOIN order_intents AS intent ON intent.intent_id=broker.intent_id
                WHERE broker.broker_order_id=?
                  AND intent.status IN ('PENDING_SUBMIT','SUBMITTED','PART_FILLED')""",
                (broker_order_id,),
            ).fetchone()
        return intent_from_persisted_payload(str(row["payload_json"])) if row else None

    def outbox_state(self, intent_id: str) -> str | None:
        with self._lock:
            row = self.connection.execute("SELECT state FROM execution_outbox WHERE intent_id=?", (intent_id,)).fetchone()
        return str(row["state"]) if row is not None else None

    def transition(self, intent_id: str, target: Status) -> None:
        current = self.status(intent_id)
        if current is Status.UNKNOWN and target is not Status.RECONCILING:
            raise ValueError("UNKNOWN orders require reconciliation; they cannot be resent")
        if current is Status.RECONCILING and target is Status.APPROVED:
            raise ValueError("reconciliation cannot enqueue a fresh order")
        with self._lock, self.connection:
            timestamp = now().isoformat()
            self.connection.execute(
                "UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?",
                (target.value, timestamp, intent_id),
            )
            if target is Status.RECONCILING:
                self.connection.execute(
                    "UPDATE reconciliation_cases SET state='RECONCILING', updated_at=? WHERE intent_id=? AND state='OPEN'",
                    (timestamp, intent_id),
                )

    def recover(self) -> int:
        """Mark every delivery-ambiguous row UNKNOWN without re-enqueueing it."""
        uncertain = tuple(status.value for status in _UNCERTAIN)
        marks = ",".join("?" for _ in uncertain)
        timestamp = now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                f"""INSERT OR IGNORE INTO reconciliation_cases(intent_id,state,reason,opened_at,updated_at)
                SELECT intent_id,'OPEN','restart_recovery',?,? FROM order_intents WHERE status IN ({marks})""",
                (timestamp, timestamp, *uncertain),
            )
            cursor = self.connection.execute(
                f"UPDATE order_intents SET status=?, updated_at=? WHERE status IN ({marks})",
                (Status.UNKNOWN.value, timestamp, *uncertain),
            )
            self.connection.execute(
                "UPDATE execution_outbox SET state='UNKNOWN', updated_at=? WHERE state IN ('PENDING', 'LEASED')",
                (timestamp,),
            )
        return cursor.rowcount

    def pending_outbox_ids(self) -> list[str]:
        """Queued deliveries, oldest first."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT intent_id FROM execution_outbox WHERE state='PENDING' ORDER BY created_at, intent_id",
            ).fetchall()
        return [str(row["intent_id"]) for row in rows]

    def claim_outbox(self, intent_id: str | None = None) -> str | None:
        """Claim once (the oldest, or a given queued one); never available for blind retry."""
        with self._lock, self.connection:
            if intent_id is None:
                row = self.connection.execute("SELECT intent_id FROM execution_outbox WHERE state='PENDING' ORDER BY created_at LIMIT 1").fetchone()
                if row is None:
                    return None
                intent_id = str(row["intent_id"])
            changed = self.connection.execute("UPDATE execution_outbox SET state='LEASED', updated_at=? WHERE intent_id=? AND state='PENDING'", (now().isoformat(), intent_id))
            return intent_id if changed.rowcount == 1 else None

    def mark_delivery_unknown(self, intent_id: str, reason: str) -> None:
        with self._lock, self.connection:
            timestamp = now().isoformat()
            self.connection.execute(
                """INSERT OR IGNORE INTO reconciliation_cases(intent_id,state,reason,opened_at,updated_at)
                VALUES (?,'OPEN',?,?,?)""", (intent_id, reason, timestamp, timestamp),
            )
            self.connection.execute(
                "UPDATE execution_outbox SET state='UNKNOWN', updated_at=? "
                "WHERE intent_id=? AND state IN ('PENDING','LEASED')",
                (timestamp, intent_id),
            )
            self.connection.execute("UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?", (Status.UNKNOWN.value, timestamp, intent_id))
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'DELIVERY_UNKNOWN', ?, ?)""",
                (str(uuid4()), intent_id, json.dumps({"reason": reason}, separators=(",", ":")), timestamp),
            )

    def mark_dispatch_refused(self, intent_id: str, reasons: tuple[str, ...]) -> None:
        """Reject a leased order whose final preflight failed before any broker I/O.

        Only an APPROVED intent with a LEASED outbox entry qualifies; anything
        else may already have reached the broker and is marked UNKNOWN.
        """
        timestamp = now().isoformat()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE execution_outbox SET state='REFUSED', updated_at=? WHERE intent_id=? AND state IN ('PENDING','LEASED') "
                "AND EXISTS (SELECT 1 FROM order_intents WHERE intent_id=? AND status='APPROVED')",
                (timestamp, intent_id, intent_id),
            ).rowcount
            if changed == 1:
                self.connection.execute(
                    "UPDATE order_intents SET status=?, risk_reasons_json=?, updated_at=? WHERE intent_id=?",
                    (Status.RISK_REJECTED.value, json.dumps(list(reasons)), timestamp, intent_id),
                )
                self.connection.execute(
                    """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                    VALUES (?, ?, 'DISPATCH_REFUSED', ?, ?)""",
                    (str(uuid4()), intent_id, json.dumps({"risk_reasons": list(reasons)}, separators=(",", ":")), timestamp),
                )
        if changed != 1:
            self.mark_delivery_unknown(intent_id, "dispatch_refusal_state_mismatch")

    def mark_operation_pending(self, intent_id: str, broker_order_id: str) -> None:
        intent = self.load_intent(intent_id)
        if intent.operation == "PLACE" or intent.target_order_id != broker_order_id:
            raise ValueError("operation receipt identity mismatch")
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE order_intents SET status='PENDING_SUBMIT', broker_order_id=?, updated_at=? WHERE intent_id=?",
                (broker_order_id, now().isoformat(), intent_id),
            )
            self.connection.execute(
                "UPDATE execution_outbox SET state='DISPATCHED', updated_at=? WHERE intent_id=?",
                (now().isoformat(), intent_id),
            )

    def mark_pending_submit(self, intent_id: str, broker_order_id: str) -> None:
        if not broker_order_id:
            self.mark_delivery_unknown(intent_id, "missing_broker_identity")
            return
        with self._lock, self.connection:
            timestamp = now().isoformat()
            self.connection.execute(
                """INSERT INTO broker_orders(intent_id, broker_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)""", (intent_id, broker_order_id, timestamp, timestamp),
            )
            self.connection.execute("UPDATE execution_outbox SET state='DISPATCHED', updated_at=? WHERE intent_id=? AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=?, broker_order_id=?, updated_at=? WHERE intent_id=?", (Status.PENDING_SUBMIT.value, broker_order_id, timestamp, intent_id))
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'BROKER_ORDER_BOUND', ?, ?)""",
                (str(uuid4()), intent_id, json.dumps({"broker_order_id": broker_order_id}, separators=(",", ":")), timestamp),
            )

    def record_terminal_callback(self, intent_id: str, broker_order_id: str, terminal: Status) -> None:
        """Persist an idempotent terminal callback only after identity binding.

        A callback cannot turn an UNKNOWN order into a terminal result: that
        path belongs to explicit reconciliation and must never silently assert
        which broker order the ambiguous delivery referred to.
        """
        if terminal not in {Status.FILLED, Status.CANCELLED, Status.BROKER_REJECTED, Status.COMPLETED}:
            raise ValueError("callback status must be terminal")
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT status, broker_order_id FROM order_intents WHERE intent_id=?", (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            if row["broker_order_id"] != broker_order_id:
                raise ValueError("callback broker identity does not match intent")
            current = Status(row["status"])
            if current is terminal:
                return
            if current not in {Status.PENDING_SUBMIT, Status.SUBMITTED, Status.PART_FILLED}:
                raise ValueError("callback is not valid for current intent status")
            timestamp = now().isoformat()
            self.connection.execute(
                "UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?",
                (terminal.value, timestamp, intent_id),
            )
            self.connection.execute(
                "UPDATE execution_outbox SET state='TERMINAL', updated_at=? WHERE intent_id=?",
                (timestamp, intent_id),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'BROKER_TERMINAL_CALLBACK', ?, ?)""",
                (
                    str(uuid4()), intent_id,
                    json.dumps({"broker_order_id": broker_order_id, "status": terminal.value}, separators=(",", ":")),
                    timestamp,
                ),
            )

    def record_reconciled_terminal(self, intent_id: str, broker_order_id: str, terminal: Status) -> None:
        if terminal not in {Status.FILLED, Status.CANCELLED, Status.BROKER_REJECTED, Status.COMPLETED}:
            raise ValueError("reconciled status must be terminal")
        with self._lock, self.connection:
            row = self.connection.execute("SELECT status, broker_order_id FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is None or row["broker_order_id"] != broker_order_id:
                raise ValueError("reconciliation identity does not match intent")
            if Status(row["status"]) not in {Status.UNKNOWN, Status.RECONCILING}:
                raise ValueError("reconciliation is not valid for current intent status")
            timestamp = now().isoformat()
            self.connection.execute("UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?", (terminal.value, timestamp, intent_id))
            self.connection.execute("UPDATE execution_outbox SET state='TERMINAL', updated_at=? WHERE intent_id=?", (timestamp, intent_id))
            self.connection.execute("UPDATE reconciliation_cases SET state='RESOLVED', updated_at=? WHERE intent_id=?", (timestamp, intent_id))
            self.connection.execute("""INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'BROKER_RECONCILED_TERMINAL', ?, ?)""", (str(uuid4()), intent_id, json.dumps({"broker_order_id": broker_order_id, "status": terminal.value}, separators=(",", ":")), timestamp))

    def reconciliation_refresh_receipt(self, idempotency_key: str) -> dict[str, object] | None:
        """Return one durable, request-bound refresh receipt without a broker read."""
        with self._lock:
            row = self.connection.execute(
                "SELECT intent_id, reason, state, receipt_json FROM reconciliation_refresh_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        receipt: dict[str, object] | None = None
        if row["receipt_json"] is not None:
            try:
                parsed = json.loads(str(row["receipt_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ValueError("stored reconciliation refresh receipt is invalid") from None
            if not isinstance(parsed, dict):
                raise ValueError("stored reconciliation refresh receipt is invalid")
            receipt = parsed
        return {
            "intent_id": str(row["intent_id"]), "reason": str(row["reason"]),
            "state": str(row["state"]), "receipt": receipt,
        }

    def begin_reconciliation_refresh(self, idempotency_key: str, intent_id: str, reason: str) -> bool:
        """Reserve exactly one targeted reconciliation read and mark it reconciling.

        A reservation is intentionally retained across a process failure.  The
        caller must not guess whether a broker read happened and repeat the
        same key; an operator can use a separately audited new request after
        inspecting the durable reconciliation case.
        """
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT intent_id, reason FROM reconciliation_refresh_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["intent_id"] != intent_id or existing["reason"] != reason:
                    raise ValueError("idempotency key is already used by a different reconciliation refresh")
                return False
            pending = self.connection.execute(
                "SELECT 1 FROM reconciliation_refresh_requests WHERE intent_id=? AND state='PENDING'",
                (intent_id,),
            ).fetchone()
            if pending is not None:
                raise ValueError("reconciliation refresh is already in progress")
            order = self.connection.execute(
                "SELECT status FROM order_intents WHERE intent_id=?", (intent_id,),
            ).fetchone()
            case = self.connection.execute(
                "SELECT state FROM reconciliation_cases WHERE intent_id=?", (intent_id,),
            ).fetchone()
            if order is None or case is None or Status(order["status"]) not in {Status.UNKNOWN, Status.RECONCILING}:
                raise ValueError("reconciliation refresh is not valid for current intent status")
            if str(case["state"]) == "RESOLVED":
                raise ValueError("reconciliation refresh is not valid for resolved intent")
            timestamp = now().isoformat()
            self.connection.execute(
                """INSERT INTO reconciliation_refresh_requests(
                    idempotency_key,intent_id,reason,state,receipt_json,applied_at,completed_at
                ) VALUES (?, ?, ?, 'PENDING', NULL, ?, NULL)""",
                (idempotency_key, intent_id, reason, timestamp),
            )
            self.connection.execute(
                "UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?",
                (Status.RECONCILING.value, timestamp, intent_id),
            )
            self.connection.execute(
                "UPDATE reconciliation_cases SET state='RECONCILING', updated_at=? WHERE intent_id=?",
                (timestamp, intent_id),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'RECONCILIATION_REFRESH_STARTED', ?, ?)""",
                (str(uuid4()), intent_id, json.dumps({"reason": reason}, separators=(",", ":")), timestamp),
            )
        return True

    def complete_reconciliation_refresh(
        self, idempotency_key: str, intent_id: str, reason: str, receipt: dict[str, object],
    ) -> None:
        """Commit the result of a reserved broker read; it never sends an order."""
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT intent_id, reason, state FROM reconciliation_refresh_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None or row["intent_id"] != intent_id or row["reason"] != reason or row["state"] != "PENDING":
                raise ValueError("reconciliation refresh reservation is invalid")
            timestamp = now().isoformat()
            self.connection.execute(
                """UPDATE reconciliation_refresh_requests SET state='COMPLETE', receipt_json=?, completed_at=?
                WHERE idempotency_key=?""",
                (json.dumps(receipt, separators=(",", ":")), timestamp, idempotency_key),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'RECONCILIATION_REFRESH_COMPLETED', ?, ?)""",
                (str(uuid4()), intent_id, json.dumps(receipt, separators=(",", ":")), timestamp),
            )

    def observation_orders(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute("SELECT intent_id, correlation_id, source, status, strategy_id, run_id, broker_order_id, payload_json, risk_reasons_json, updated_at FROM order_intents ORDER BY updated_at DESC, intent_id DESC").fetchall()
        observations: list[dict[str, object]] = []
        for row in rows:
            observation = dict(row)
            raw_payload = observation.pop("payload_json", "{}")
            raw_reasons = observation.pop("risk_reasons_json", "[]")
            try:
                parsed_payload = json.loads(str(raw_payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_payload = {}
            try:
                parsed_reasons = json.loads(str(raw_reasons))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_reasons = []
            observation["symbol"] = parsed_payload.get("symbol", "") if isinstance(parsed_payload, dict) else ""
            observation["side"] = parsed_payload.get("side", "") if isinstance(parsed_payload, dict) else ""
            observation["quantity"] = parsed_payload.get("quantity", 0) if isinstance(parsed_payload, dict) else 0
            observation["limit_price"] = parsed_payload.get("limit_price", "") if isinstance(parsed_payload, dict) else ""
            observation["order_price_type"] = parsed_payload.get("order_price_type", "LMT") if isinstance(parsed_payload, dict) else "LMT"
            observation["instrument"] = parsed_payload.get("instrument") if isinstance(parsed_payload, dict) else None
            observation["operation"] = parsed_payload.get("operation", "PLACE") if isinstance(parsed_payload, dict) else "PLACE"
            observation["target_order_id"] = parsed_payload.get("target_order_id") if isinstance(parsed_payload, dict) else None
            observation["operation_payload"] = parsed_payload.get("operation_payload") if isinstance(parsed_payload, dict) else None
            observation["risk_reasons"] = [reason for reason in parsed_reasons if isinstance(reason, str)] if isinstance(parsed_reasons, list) else []
            observations.append(observation)
        return observations

    def observation_risk_decisions(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT intent_id, allowed, reasons_json, decided_at FROM risk_decisions "
                "ORDER BY decided_at DESC, intent_id DESC",
            ).fetchall()
        observations: list[dict[str, object]] = []
        for row in rows:
            observation = dict(row)
            raw_reasons = observation.pop("reasons_json", "[]")
            try:
                parsed_reasons = json.loads(str(raw_reasons))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_reasons = []
            observation["allowed"] = bool(observation["allowed"])
            observation["risk_reasons"] = [
                reason for reason in parsed_reasons if isinstance(reason, str)
            ] if isinstance(parsed_reasons, list) else []
            observations.append(observation)
        return observations

    def observation_audit_events(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT event_id, intent_id, event_type, details_json, occurred_at FROM audit_events "
                "ORDER BY occurred_at DESC, event_id DESC",
            ).fetchall()
        observations: list[dict[str, object]] = []
        for row in rows:
            observation = dict(row)
            raw_details = observation.pop("details_json", "{}")
            try:
                details = json.loads(str(raw_details))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            observation["details"] = details if isinstance(details, dict) else {}
            observations.append(observation)
        return observations

    def record_audit_event(
        self, intent_id: str | None, event_type: str, details: dict[str, object],
    ) -> None:
        """Append a non-broker lifecycle fact without changing delivery state."""
        if not event_type:
            raise ValueError("audit event type is required")
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    str(uuid4()), intent_id, event_type,
                    json.dumps(details, separators=(",", ":")), now().isoformat(),
                ),
            )

    def trading_gate(self) -> Gate:
        with self._lock:
            row = self.connection.execute(
                "SELECT state FROM gateway_controls WHERE control_name='trading_gate'",
            ).fetchone()
        if row is None:
            return Gate.DISARMED
        try:
            return Gate(str(row["state"]))
        except ValueError:
            return Gate.KILLED

    def apply_kill_switch(self, reason: str, idempotency_key: str) -> tuple[Gate, str, bool]:
        """Persist the one-way local kill control; no unkill path exists here."""
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT state, reason FROM gateway_controls WHERE control_name='trading_gate'",
            ).fetchone()
            if row is not None:
                return Gate.KILLED, str(row["reason"]), False
            timestamp = now().isoformat()
            self.connection.execute(
                """INSERT INTO gateway_controls(control_name, state, reason, idempotency_key, applied_at)
                VALUES ('trading_gate', 'KILLED', ?, ?, ?)""",
                (reason, idempotency_key, timestamp),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, NULL, 'KILL_SWITCH_APPLIED', ?, ?)""",
                (str(uuid4()), json.dumps({"reason": reason}, separators=(",", ":")), timestamp),
            )
            return Gate.KILLED, reason, True

    def record_fill(self, fill: Fill) -> None:
        """Record a broker-confirmed fill only for its already-bound identity."""
        if not fill.fill_id or type(fill.quantity) is not int or fill.quantity <= 0 or fill.filled_at.tzinfo is None:
            raise ValueError("fill identity and quantity are required")
        try:
            if not Decimal(fill.price).is_finite() or Decimal(fill.price) <= 0:
                raise ValueError("fill price must be positive")
        except (InvalidOperation, ValueError) as error:
            raise ValueError("fill price must be positive") from error
        with self._lock, self.connection:
            order = self.connection.execute(
                "SELECT status, broker_order_id FROM order_intents WHERE intent_id=?", (fill.intent_id,),
            ).fetchone()
            if order is None or order["broker_order_id"] != fill.broker_order_id:
                raise ValueError("fill broker identity does not match intent")
            existing = self.connection.execute(
                "SELECT intent_id, broker_order_id, quantity, price, filled_at FROM fills WHERE fill_id=?",
                (fill.fill_id,),
            ).fetchone()
            if existing is not None:
                if dict(existing) == {
                    "intent_id": fill.intent_id, "broker_order_id": fill.broker_order_id,
                    "quantity": fill.quantity, "price": fill.price, "filled_at": fill.filled_at.isoformat(),
                }:
                    return
                raise ValueError("fill identity conflicts with an existing fill")
            if Status(order["status"]) not in {Status.PENDING_SUBMIT, Status.SUBMITTED, Status.PART_FILLED, Status.FILLED, Status.CANCELLED, Status.UNKNOWN, Status.RECONCILING}:
                raise ValueError("fill is not valid for current intent status")
            timestamp = now().isoformat()
            self.connection.execute(
                "INSERT INTO fills(fill_id,intent_id,broker_order_id,quantity,price,filled_at) VALUES (?,?,?,?,?,?)",
                (fill.fill_id, fill.intent_id, fill.broker_order_id, fill.quantity, fill.price, fill.filled_at.isoformat()),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'FILL_RECORDED', ?, ?)""",
                (
                    str(uuid4()), fill.intent_id,
                    json.dumps({"fill_id": fill.fill_id, "broker_order_id": fill.broker_order_id}, separators=(",", ":")),
                    timestamp,
                ),
            )

    def observation_fills(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT fill_id,intent_id,broker_order_id,quantity,price,filled_at FROM fills "
                "ORDER BY filled_at DESC, fill_id DESC",
            ).fetchall()
        return [dict(row) for row in rows]

    def record_position_snapshot(self, snapshot: PositionSnapshot) -> None:
        self.record_position_snapshots((snapshot,))

    def record_position_snapshots(self, snapshots: tuple[PositionSnapshot, ...]) -> None:
        """Atomically retain a complete broker-read position observation."""
        prepared = tuple((snapshot, position_snapshot_values(snapshot)) for snapshot in snapshots)
        if len({values[0] for _, values in prepared}) != len(prepared):
            raise ValueError("position snapshot batch has duplicate identities")
        with self._lock, self.connection:
            new: list[tuple[PositionSnapshot, tuple[str, ...]]] = []
            for snapshot, values in prepared:
                existing = self.connection.execute(
                    """SELECT account_type, account_id, symbol, side, quantity, average_price,
                        market_price, unrealized_pnl, observed_at
                        FROM broker_position_snapshots WHERE snapshot_id=?""",
                    (snapshot.snapshot_id,),
                ).fetchone()
                expected = dict(zip(
                    ("account_type", "account_id", "symbol", "side", "quantity", "average_price",
                     "market_price", "unrealized_pnl", "observed_at"),
                    values[1:], strict=True,
                ))
                if existing is not None:
                    if dict(existing) != expected:
                        raise ValueError("position snapshot identity conflicts with an existing snapshot")
                else:
                    new.append((snapshot, values))
            for snapshot, values in new:
                self.connection.execute(
                    """INSERT INTO broker_position_snapshots(
                        snapshot_id,account_type,account_id,symbol,side,quantity,average_price,
                        market_price,unrealized_pnl,observed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                self.connection.execute(
                    """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                    VALUES (?, NULL, 'POSITION_SNAPSHOT_RECORDED', ?, ?)""",
                    (
                        str(uuid4()), json.dumps({
                            "snapshot_id": snapshot.snapshot_id,
                            "account_type": snapshot.account_type,
                            "symbol": snapshot.symbol,
                        }, separators=(",", ":")), now().isoformat(),
                    ),
                )

    def observation_position_snapshots(self) -> list[dict[str, object]]:
        """Return internal facts; an observer projection must mask account_id."""
        with self._lock:
            rows = self.connection.execute(
                """SELECT snapshot_id,account_type,account_id,symbol,side,quantity,average_price,
                    market_price,unrealized_pnl,observed_at FROM broker_position_snapshots
                    ORDER BY observed_at DESC, snapshot_id DESC""",
            ).fetchall()
        return [dict(row) for row in rows]

    def record_signal(self, signal: StrategySignal) -> None:
        """Persist a signal once; it never changes a broker delivery state."""
        recorded_at = now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_signals(
                    signal_id, strategy_id, run_id, event_id, signal_type, symbol, side,
                    intent_id, correlation_id, status, risk_reasons_json, observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING""",
                (
                    signal.signal_id, signal.strategy_id, signal.run_id, signal.event_id,
                    signal.signal_type, signal.symbol, signal.side, signal.intent_id,
                    signal.correlation_id, signal.status.value, json.dumps(signal.risk_reasons),
                    signal.observed_at.isoformat(), recorded_at,
                ),
            )

    def observation_signals(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT signal_id, strategy_id, run_id, event_id, signal_type, symbol, side,
                    intent_id, correlation_id, status, risk_reasons_json, observed_at
                    FROM strategy_signals ORDER BY observed_at DESC, signal_id DESC""",
            ).fetchall()
        observations: list[dict[str, object]] = []
        for row in rows:
            observation = dict(row)
            raw_reasons = observation.pop("risk_reasons_json", "[]")
            try:
                parsed_reasons = json.loads(str(raw_reasons))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_reasons = []
            observation["risk_reasons"] = [
                reason for reason in parsed_reasons if isinstance(reason, str)
            ] if isinstance(parsed_reasons, list) else []
            observations.append(observation)
        return observations

    def record_strategy_definition(self, definition: StrategyDefinition) -> None:
        """Persist a descriptor without starting, pausing, or dispatching it."""
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_definitions(strategy_id, state, enabled, allowed_symbols_json)
                VALUES (?, ?, ?, ?) ON CONFLICT(strategy_id) DO NOTHING""",
                (definition.strategy_id, definition.state, int(definition.enabled),
                 json.dumps(definition.allowed_symbols)),
            )
            self.connection.execute(
                """INSERT INTO strategy_versions(strategy_id, version, config_checksum)
                VALUES (?, ?, ?) ON CONFLICT(strategy_id, version) DO NOTHING""",
                (definition.strategy_id, definition.version, definition.config_checksum),
            )

    def set_strategy_definition_state(self, strategy_id: str, state: str, enabled: bool) -> None:
        """Persist an operator lifecycle state without touching any intent."""
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE strategy_definitions SET state=?, enabled=? WHERE strategy_id=?",
                (state, int(enabled), strategy_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(strategy_id)

    def strategy_control_receipt(self, idempotency_key: str) -> dict[str, object] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT strategy_id, action, reason, receipt_json FROM strategy_control_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(str(row["receipt_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("stored strategy control receipt is invalid") from None
        if not isinstance(receipt, dict):
            raise ValueError("stored strategy control receipt is invalid")
        return {"strategy_id": str(row["strategy_id"]), "action": str(row["action"]),
                "reason": str(row["reason"]), "receipt": receipt}

    def record_strategy_control(
        self, idempotency_key: str, strategy_id: str, action: str, reason: str, receipt: dict[str, object],
    ) -> bool:
        existing = self.strategy_control_receipt(idempotency_key)
        expected = {"strategy_id": strategy_id, "action": action, "reason": reason, "receipt": receipt}
        if existing is not None:
            if existing != expected:
                raise ValueError("idempotency key is already used by a different strategy control")
            return False
        with self._lock, self.connection:
            cursor = self.connection.execute(
                    """INSERT OR IGNORE INTO strategy_control_requests(
                        idempotency_key,strategy_id,action,reason,receipt_json,applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (idempotency_key, strategy_id, action, reason,
                     json.dumps(receipt, separators=(",", ":")), now().isoformat()),
                )
        if cursor.rowcount == 1:
            return True
        existing = self.strategy_control_receipt(idempotency_key)
        if existing != expected:
            raise ValueError("idempotency key is already used by a different strategy control")
        return False

    def observation_strategy_definitions(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT definition.strategy_id, version.version, version.config_checksum,
                    definition.state, definition.enabled, definition.allowed_symbols_json
                    FROM strategy_definitions AS definition
                    JOIN strategy_versions AS version ON version.strategy_id = definition.strategy_id
                    ORDER BY definition.strategy_id, version.version""",
            ).fetchall()
        observations: list[dict[str, object]] = []
        for row in rows:
            observation = dict(row)
            raw_symbols = observation.pop("allowed_symbols_json", "[]")
            try:
                symbols = json.loads(str(raw_symbols))
            except (TypeError, ValueError, json.JSONDecodeError):
                symbols = []
            observation["enabled"] = bool(observation["enabled"])
            observation["allowed_symbols"] = [
                symbol for symbol in symbols if isinstance(symbol, str)
            ] if isinstance(symbols, list) else []
            observations.append(observation)
        return observations

    def record_strategy_run(self, run: StrategyRun) -> None:
        """Persist one fixture lifecycle without changing any delivery state."""
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_runs(
                    run_id, strategy_id, version, config_checksum, state, last_intent_id,
                    last_status, intent_count, started_at, heartbeat_at, ended_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING""",
                (
                    run.run_id, run.strategy_id, run.version, run.config_checksum, run.state,
                    run.last_intent_id, run.last_status.value if run.last_status else None,
                    run.intent_count, run.started_at.isoformat(),
                    run.heartbeat_at.isoformat() if run.heartbeat_at else None,
                    run.ended_at.isoformat() if run.ended_at else None, run.updated_at.isoformat(),
                ),
            )
            if run.heartbeat_at is not None:
                self.connection.execute(
                    """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (?, ?)
                    ON CONFLICT DO NOTHING""",
                    (run.run_id, run.heartbeat_at.isoformat()),
                )

    def start_strategy_lifecycle(self, run: StrategyRun) -> None:
        """Atomically create an active run and expose its definition state."""
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE strategy_definitions SET state='RUNNING', enabled=1 WHERE strategy_id=?",
                (run.strategy_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(run.strategy_id)
            self.connection.execute(
                """INSERT INTO strategy_runs(
                    run_id, strategy_id, version, config_checksum, state, last_intent_id,
                    last_status, intent_count, started_at, heartbeat_at, ended_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.run_id, run.strategy_id, run.version, run.config_checksum, run.state,
                 run.last_intent_id, run.last_status.value if run.last_status else None,
                 run.intent_count, run.started_at.isoformat(),
                 run.heartbeat_at.isoformat() if run.heartbeat_at else None,
                 run.ended_at.isoformat() if run.ended_at else None, run.updated_at.isoformat()),
            )
            if run.heartbeat_at is not None:
                self.connection.execute(
                    "INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (?, ?)",
                    (run.run_id, run.heartbeat_at.isoformat()),
                )

    def record_strategy_heartbeat(self, run_id: str, observed_at: datetime) -> None:
        """Record a monotonic liveness fact for an existing durable run."""
        timestamp = observed_at.isoformat()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_runs SET heartbeat_at=CASE
                    WHEN heartbeat_at IS NULL OR heartbeat_at < ? THEN ? ELSE heartbeat_at END,
                    updated_at=CASE WHEN updated_at < ? THEN ? ELSE updated_at END
                    WHERE run_id=?""",
                (timestamp, timestamp, timestamp, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self.connection.execute(
                """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (?, ?)
                ON CONFLICT DO NOTHING""", (run_id, timestamp),
            )

    def record_strategy_intent_result(
        self, run_id: str, intent_id: str, status: Status, observed_at: datetime,
    ) -> None:
        """Attach one accepted strategy intent to its active durable run.

        The same event may be replayed after a process restart.  Replaying its
        already-recorded intent updates liveness but cannot inflate the run's
        intent count or create a second run.
        """
        timestamp = observed_at.isoformat()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_runs SET last_intent_id=?, last_status=?,
                    intent_count=CASE WHEN last_intent_id IS ? THEN intent_count
                        ELSE intent_count + 1 END,
                    heartbeat_at=CASE WHEN heartbeat_at IS NULL OR heartbeat_at < ?
                        THEN ? ELSE heartbeat_at END,
                    updated_at=CASE WHEN updated_at < ? THEN ? ELSE updated_at END
                    WHERE run_id=?""",
                (intent_id, status.value, intent_id, timestamp, timestamp, timestamp, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self.connection.execute(
                """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (?, ?)
                ON CONFLICT DO NOTHING""", (run_id, timestamp),
            )

    def transition_strategy_run(self, run_id: str, state: str, observed_at: datetime) -> None:
        """Move a durable run without creating, changing, or dispatching orders."""
        timestamp = observed_at.isoformat()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_runs SET state=?, heartbeat_at=?,
                    ended_at=CASE WHEN ?='STOPPED' THEN ? ELSE ended_at END,
                    updated_at=? WHERE run_id=?""",
                (state, timestamp, state, timestamp, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self.connection.execute(
                """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (?, ?)
                ON CONFLICT DO NOTHING""", (run_id, timestamp),
            )

    def transition_strategy_lifecycle(
        self, run_id: str, strategy_id: str, state: str, enabled: bool, observed_at: datetime,
    ) -> None:
        """Atomically move the run and the user-visible definition state."""
        timestamp = observed_at.isoformat()
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_runs SET state=?, heartbeat_at=?,
                    ended_at=CASE WHEN ?='STOPPED' THEN ? ELSE ended_at END,
                    updated_at=? WHERE run_id=?""",
                (state, timestamp, state, timestamp, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            cursor = self.connection.execute(
                "UPDATE strategy_definitions SET state=?, enabled=? WHERE strategy_id=?",
                (state, int(enabled), strategy_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(strategy_id)
            self.connection.execute(
                """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (?, ?)
                ON CONFLICT DO NOTHING""", (run_id, timestamp),
            )

    def observation_strategy_runs(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT run_id, strategy_id, version, config_checksum, state, last_intent_id,
                    last_status, intent_count, started_at, heartbeat_at, ended_at, updated_at
                    FROM strategy_runs ORDER BY updated_at DESC, run_id DESC""",
            ).fetchall()
        return [dict(row) for row in rows]

    def observation_reconciliation_cases(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT intent_id,state,reason,opened_at,updated_at FROM reconciliation_cases ORDER BY opened_at DESC, intent_id DESC",
            ).fetchall()
        return [dict(row) for row in rows]


class OrderCoordinator:
    """One ingress point for manual UI and strategy intents; it owns no broker."""

    def __init__(
        self,
        store: IntentStore,
        gate: Gate = Gate.DISARMED,
        risk: RiskEngine | None = None,
        risk_context_provider: Callable[[Intent], RiskContext | None] | None = None,
    ) -> None:
        self.store = store
        self.gate = gate
        self.risk = risk or RiskEngine()
        self.risk_context_provider = risk_context_provider
        # The Gateway owns one coordinator shared by HTTP and the runner.
        # Serialize evaluation + durable reservation so concurrent requests
        # observe each other's rate limits and outstanding exposure.
        self.lock = RLock()

    def evaluated_intent(self, intent: Intent) -> Intent:
        if intent.operation not in {"UPDATE_PRICE", "UPDATE_QUANTITY"}:
            return intent
        target = self.store.verified_target_intent(intent.target_order_id or "")
        if target is None or not target.broker_request:
            return intent
        request = deepcopy(target.broker_request)
        body = request.get("body")
        stock = body.get("stock_order") if isinstance(body, dict) else None
        if not isinstance(stock, dict):
            return intent
        changes: dict = {"quantity": target.quantity, "price": target.limit_price}
        for row in reversed(self.store.observation_orders()):
            if row["status"] == Status.COMPLETED.value:
                command = self.store.load_intent(str(row["intent_id"]))
                if command.target_order_id == intent.target_order_id and command.operation in {"UPDATE_PRICE", "UPDATE_QUANTITY"}:
                    changes.update(command.operation_payload or {})
        changes.update(intent.operation_payload or {})
        quantity = changes["quantity"] if type(changes["quantity"]) is int else 0
        price = str(changes["price"])
        stock.update(quantity=quantity, price=price)
        return replace(target, quantity=quantity, limit_price=price, broker_request=request)

    def accept(self, intent: Intent) -> tuple[Status, Verdict]:
        with self.lock:
            if self.store.trading_gate() is Gate.KILLED:
                self.gate = Gate.KILLED
            evaluated = self.evaluated_intent(intent)
            context = self.risk_context_provider(evaluated) if self.risk_context_provider and intent.operation != "CANCEL" else None
            context = self.with_reservations(evaluated, context)
            verdict = self.risk.evaluate(intent, self.gate, context)
            reasons = list(verdict.reasons)
            if intent.operation in {"UPDATE_PRICE", "UPDATE_QUANTITY"}:
                original = self.store.verified_target_intent(intent.target_order_id or "")
                if original:
                    body = (original.broker_request or {}).get("body", {})
                    stock = body.get("stock_order", {}) if isinstance(body, dict) else {}
                    if intent.operation == "UPDATE_PRICE" and stock.get("order_lot") == "IntradayOdd":
                        reasons.append("intraday_odd_price_update_unsupported")
                    if intent.operation == "UPDATE_QUANTITY":
                        previous = self.evaluated_intent(replace(intent, operation_payload={}))
                        if evaluated.quantity >= previous.quantity:
                            reasons.append("quantity_update_must_reduce")
                        filled = sum(int(str(fill["quantity"])) for fill in self.store.observation_fills()
                                     if fill["intent_id"] == original.intent_id)
                        if evaluated.quantity > 0 and risk_quantity(evaluated) < filled:
                            reasons.append("quantity_below_filled")
            if intent.operation in {"UPDATE_PRICE", "UPDATE_QUANTITY"} and self.gate is Gate.ARMED:
                if evaluated is intent:
                    reasons.append("instrument_risk_unavailable")
                else:
                    reasons.extend(self.risk.evaluate(evaluated, self.gate, context).reasons)
            if self.gate is Gate.ARMED and intent.operation != "CANCEL":
                accepted = [row for row in self.store.observation_orders()
                            if row.get("status") != Status.RISK_REJECTED.value
                            and row.get("intent_id") != intent.intent_id]
                if intent.operation in {"UPDATE_PRICE", "UPDATE_QUANTITY"} and any(
                    row["status"] in {s.value for s in _UNCERTAIN} | {"UNKNOWN", "RECONCILING", "MANUAL_REVIEW"}
                    and self.store.load_intent(str(row["intent_id"])).target_order_id == intent.target_order_id
                    for row in accepted
                ):
                    reasons.append("outstanding_amendment_requires_reconciliation")
                recent = []
                for row in accepted:
                    old = self.store.load_intent(str(row["intent_id"]))
                    if old.account_id == intent.account_id:
                        age = (now() - old.created_at).total_seconds()
                        if age < 60:
                            recent.append(old)
                        # A time cooldown paces strategies only. Manual multi-order
                        # flows (laddering, brackets) are paced by broker
                        # acknowledgement at dispatch instead; see dispatch_ready.
                        if intent.source is not Source.MANUAL_UI and age < self.risk.limits.cooldown_seconds:
                            reasons.append("order_cooldown")
                if len(recent) >= self.risk.limits.max_orders_per_minute:
                    reasons.append("order_rate_exceeded")
                # Several open orders of one product are allowed (they reserve
                # capacity above), but never on top of an uncertain delivery of
                # the same product: that one must be reconciled first.
                key = self.product_key(intent) if intent.operation == "PLACE" else None
                if key is not None and any(
                    row.get("status") in {Status.UNKNOWN.value, Status.RECONCILING.value, Status.MANUAL_REVIEW.value}
                    and self.product_key(self.store.load_intent(str(row["intent_id"]))) == key
                    for row in accepted
                ):
                    reasons.append("outstanding_order_requires_reconciliation")
            verdict = Verdict(not reasons, tuple(dict.fromkeys(reasons)))
            return self.store.persist(intent, verdict), verdict

    def product_key(self, intent: Intent) -> str | None:
        """Account + product: a stock/CB symbol, or a futures root for every month."""
        try:
            profile = instrument_profile(intent, self.risk.limits)
        except ValueError:
            return None
        return f"{intent.account_id}:{profile.position_key(intent.symbol)}"

    def reservations_for(self, intent: Intent) -> dict[str, Reservation]:
        """Unfilled remainder of every other open order of the same account."""
        open_rows = [
            row for row in self.store.observation_orders()
            if row.get("status") in _OPEN_ORDER_STATUSES and row.get("intent_id") != intent.intent_id
        ]
        if not open_rows:
            return {}
        filled: dict[str, int] = {}
        for fill in self.store.observation_fills():
            key = str(fill["intent_id"])
            filled[key] = filled.get(key, 0) + int(str(fill["quantity"]))
        reservations: dict[str, Reservation] = {}
        for row in open_rows:
            other = self.store.load_intent(str(row["intent_id"]))
            if other.operation != "PLACE" or other.account_id != intent.account_id:
                continue
            try:
                profile = instrument_profile(other, self.risk.limits)
                price = Decimal(other.limit_price)
            except (ValueError, InvalidOperation):
                continue
            remaining = max(0, profile.units - filled.get(other.intent_id, 0))
            if not remaining:
                continue
            notional = Decimal(remaining) * (price if price.is_finite() and price > 0 else Decimal("0")) * profile.multiplier
            position_key = profile.position_key(other.symbol)
            current = reservations.get(position_key, Reservation())
            reservations[position_key] = (
                replace(current, buy_units=current.buy_units + remaining, buy_notional=current.buy_notional + notional)
                if other.side == "BUY"
                else replace(current, sell_units=current.sell_units + remaining, sell_notional=current.sell_notional + notional)
            )
        return reservations

    def with_reservations(self, intent: Intent, context: RiskContext | None) -> RiskContext | None:
        if context is None:
            return None
        return replace(context, reservations=self.reservations_for(intent))

    def dispatch_ready(self, intent: Intent) -> bool:
        """Send a new order only after the broker acknowledged earlier ones of its product.

        "Acknowledged" means the broker reported the order as submitted (or it
        reached a fill or terminal state). Until then the next order for the
        same product stays queued; other products are not held back.
        """
        if intent.operation != "PLACE":
            return True
        key = self.product_key(intent)
        if key is None:
            return True  # the dispatch preflight rejects it with a reason
        for row in self.store.observation_orders():
            status = row.get("status")
            if row.get("intent_id") == intent.intent_id:
                continue
            in_flight = status in _AWAITING_BROKER_ACK or (
                status == Status.APPROVED.value and self.store.outbox_state(str(row["intent_id"])) == "LEASED"
            )
            if in_flight and self.product_key(self.store.load_intent(str(row["intent_id"]))) == key:
                return False
        return True
