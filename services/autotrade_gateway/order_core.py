"""Durable, broker-free order spine for the Autotrade Gateway.

The HTTP handler intentionally does not expose this as a mutation endpoint
yet.  This module makes the invariant testable before an Execution Gateway
worker and Shioaji HTTP adapter are connected: every request is attributed,
risk-evaluated and persisted before it could ever reach a broker.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
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
}


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
    def outbox_state(self, intent_id: str) -> str | None: ...
    def transition(self, intent_id: str, target: Status) -> None: ...
    def recover(self) -> int: ...
    def claim_outbox(self) -> str | None: ...
    def mark_delivery_unknown(self, intent_id: str, reason: str) -> None: ...
    def mark_pending_submit(self, intent_id: str, broker_order_id: str) -> None: ...
    def record_terminal_callback(
        self, intent_id: str, broker_order_id: str, terminal: Status,
    ) -> None: ...
    def observation_orders(self) -> list[dict[str, object]]: ...
    def observation_risk_decisions(self) -> list[dict[str, object]]: ...
    def observation_audit_events(self) -> list[dict[str, object]]: ...
    def trading_gate(self) -> Gate: ...
    def apply_kill_switch(self, reason: str, idempotency_key: str) -> tuple[Gate, str, bool]: ...
    def record_fill(self, fill: Fill) -> None: ...
    def observation_fills(self) -> list[dict[str, object]]: ...
    def record_signal(self, signal: StrategySignal) -> None: ...
    def observation_signals(self) -> list[dict[str, object]]: ...
    def record_strategy_definition(self, definition: StrategyDefinition) -> None: ...
    def set_strategy_definition_state(self, strategy_id: str, state: str, enabled: bool) -> None: ...
    def strategy_control_receipt(self, idempotency_key: str) -> dict[str, object] | None: ...
    def record_strategy_control(self, idempotency_key: str, strategy_id: str, action: str, reason: str, receipt: dict[str, object]) -> bool: ...
    def observation_strategy_definitions(self) -> list[dict[str, object]]: ...
    def record_strategy_run(self, run: StrategyRun) -> None: ...
    def transition_strategy_run(self, run_id: str, state: str, observed_at: datetime) -> None: ...
    def record_strategy_heartbeat(self, run_id: str, observed_at: datetime) -> None: ...
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
        }


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
            # Account numbers do not encode their environment.  The caller
            # must populate this allowlist only from a verified simulation
            # sidecar discovery; a cosmetic `SIM-` prefix is not evidence.
            if intent.account_id not in self.limits.allowed_accounts:
                reasons.append("simulation_account_not_allowed")
            if not intent.symbol or not intent.symbol.isalnum():
                reasons.append("invalid_symbol")
            elif intent.symbol not in self.limits.allowed_symbols and intent.symbol != "COMBO":
                reasons.append("symbol_not_allowed")
            if intent.side not in {"BUY", "SELL"}:
                reasons.append("invalid_side")
            if intent.quantity <= 0:
                reasons.append("invalid_quantity")
            elif intent.quantity > self.limits.max_order_quantity:
                reasons.append("quantity_limit_exceeded")
            if intent.order_price_type not in {"LMT", "MKT", "MKP"}:
                reasons.append("invalid_order_price_type")
            elif intent.order_price_type == "LMT":
                try:
                    price = Decimal(intent.limit_price)
                    if price <= 0:
                        reasons.append("invalid_limit_price")
                    elif price * intent.quantity > self.limits.max_order_notional:
                        reasons.append("notional_limit_exceeded")
                except (InvalidOperation, ValueError):
                    reasons.append("invalid_limit_price")
            if gate is Gate.ARMED:
                reasons.extend(self._dynamic_reasons(intent, context))
        elif intent.operation in {"CANCEL", "UPDATE_PRICE", "UPDATE_QUANTITY"}:
            if not intent.target_order_id:
                reasons.append("target_order_id_required")
            reasons.append("target_order_account_unverified")
        else:
            reasons.append("invalid_operation")
        if intent.source is Source.STRATEGY and (not intent.strategy_id or not intent.run_id):
            reasons.append("strategy_identity_required")
        return Verdict(not reasons, tuple(reasons))

    def _dynamic_reasons(self, intent: Intent, context: RiskContext | None) -> list[str]:
        """Evaluate limits that require an authoritative market/account snapshot.

        A future execution gateway must supply this snapshot from its durable
        read models immediately before it creates an outbox record.  Absence
        is deliberately a rejection rather than a permissive fallback: an
        armed process with stale data must never become a bypass around risk.
        """
        if context is None:
            return ["risk_context_unavailable"]
        reasons: list[str] = []
        timestamp = now()
        quote_price = context.quote_price
        quote_at = context.quote_at
        if (
            quote_price is None
            or quote_price <= 0
            or quote_at is None
            or (timestamp - quote_at).total_seconds() > self.limits.max_quote_age_seconds
        ):
            reasons.append("stale_quote")
        else:
            if intent.order_price_type == "LMT":
                try:
                    deviation = abs(Decimal(intent.limit_price) - quote_price) / quote_price
                    if deviation > self.limits.max_price_deviation:
                        reasons.append("price_deviation_exceeded")
                except InvalidOperation:
                    reasons.append("invalid_limit_price")
            proposed_notional = Decimal(intent.quantity) * (
                quote_price if intent.order_price_type != "LMT" else Decimal(intent.limit_price)
            )
            current_quantity = context.positions.get(intent.symbol, RiskPosition()).quantity
            projected_quantity = current_quantity + (intent.quantity if intent.side == "BUY" else -intent.quantity)
            if abs(projected_quantity) > self.limits.max_position_per_symbol:
                reasons.append("position_limit_exceeded")
            gross_exposure = sum(
                abs(position.quantity) * position.market_price
                for position in context.positions.values()
            )
            if gross_exposure + proposed_notional > self.limits.max_gross_exposure:
                reasons.append("gross_exposure_limit_exceeded")
        if context.realized_pnl + context.unrealized_pnl <= -self.limits.max_daily_loss:
            reasons.append("daily_loss_limit_exceeded")
        return reasons


@dataclass(frozen=True)
class RiskPosition:
    """Sanitized position fact from the durable account read model."""

    quantity: int = 0
    market_price: Decimal = Decimal("0")


@dataclass(frozen=True)
class RiskContext:
    """Fresh, authoritative facts needed for dynamic order risk checks."""

    quote_price: Decimal | None = None
    quote_at: datetime | None = None
    positions: dict[str, RiskPosition] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")


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
            CREATE TABLE IF NOT EXISTS broker_orders (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                broker_order_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_cases (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, reason TEXT NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
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

    def claim_outbox(self) -> str | None:
        """Claim once; a leased record is never available for blind retry."""
        with self._lock, self.connection:
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
            self.connection.execute("UPDATE execution_outbox SET state='UNKNOWN', updated_at=? WHERE intent_id=? AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?", (Status.UNKNOWN.value, timestamp, intent_id))
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (?, ?, 'DELIVERY_UNKNOWN', ?, ?)""",
                (str(uuid4()), intent_id, json.dumps({"reason": reason}, separators=(",", ":")), timestamp),
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
        if terminal not in {Status.FILLED, Status.CANCELLED, Status.BROKER_REJECTED}:
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
            if current not in {Status.PENDING_SUBMIT, Status.SUBMITTED}:
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
        if not fill.fill_id or fill.quantity <= 0:
            raise ValueError("fill identity and quantity are required")
        try:
            if Decimal(fill.price) <= 0:
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
            if Status(order["status"]) not in {Status.PENDING_SUBMIT, Status.SUBMITTED, Status.FILLED}:
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

    def accept(self, intent: Intent) -> tuple[Status, Verdict]:
        context = self.risk_context_provider(intent) if self.risk_context_provider else None
        verdict = self.risk.evaluate(intent, self.gate, context)
        return self.store.persist(intent, verdict), verdict
