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
from typing import Protocol
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
    intent_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=now)


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
    def close(self) -> None: ...


@dataclass(frozen=True)
class RiskLimits:
    """Portable first slice of the legacy simulation risk policy.

    Position, quote freshness, daily loss, and frequency budgets require their
    own durable read models and are intentionally not implied by this static
    policy.  These baseline limits still apply regardless of the trading gate.
    """

    allowed_accounts: tuple[str, ...] = ("SIM-001",)
    allowed_symbols: tuple[str, ...] = ("2330", "2317", "0050", "11011", "TXFR1")
    max_order_quantity: int = 10_000
    max_order_notional: Decimal = Decimal("5000000")

    def document(self) -> dict[str, object]:
        return {
            "allowed_accounts": list(self.allowed_accounts),
            "allowed_symbols": list(self.allowed_symbols),
            "max_order_quantity": self.max_order_quantity,
            "max_order_notional": str(self.max_order_notional),
        }


class RiskEngine:
    """Fail-closed simulation risk policy shared by manual and strategy paths."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(self, intent: Intent, gate: Gate) -> Verdict:
        reasons: list[str] = []
        if gate is not Gate.ARMED:
            reasons.append("trading_gate_not_armed")
        if intent.operation == "PLACE":
            if not intent.account_id.startswith("SIM-"):
                reasons.append("simulation_account_required")
            elif intent.account_id not in self.limits.allowed_accounts:
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
        elif intent.operation in {"CANCEL", "UPDATE_PRICE", "UPDATE_QUANTITY"}:
            if not intent.target_order_id:
                reasons.append("target_order_id_required")
            reasons.append("target_order_account_unverified")
        else:
            reasons.append("invalid_operation")
        if intent.source is Source.STRATEGY and (not intent.strategy_id or not intent.run_id):
            reasons.append("strategy_identity_required")
        return Verdict(not reasons, tuple(reasons))


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
            self.connection.execute(
                "UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?",
                (target.value, now().isoformat(), intent_id),
            )

    def recover(self) -> int:
        """Mark every delivery-ambiguous row UNKNOWN without re-enqueueing it."""
        uncertain = tuple(status.value for status in _UNCERTAIN)
        marks = ",".join("?" for _ in uncertain)
        timestamp = now().isoformat()
        with self._lock, self.connection:
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
            self.connection.execute("UPDATE execution_outbox SET state='UNKNOWN', updated_at=? WHERE intent_id=? AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?", (Status.UNKNOWN.value, timestamp, intent_id))

    def mark_pending_submit(self, intent_id: str, broker_order_id: str) -> None:
        if not broker_order_id:
            self.mark_delivery_unknown(intent_id, "missing_broker_identity")
            return
        with self._lock, self.connection:
            timestamp = now().isoformat()
            self.connection.execute("UPDATE execution_outbox SET state='DISPATCHED', updated_at=? WHERE intent_id=? AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=?, broker_order_id=?, updated_at=? WHERE intent_id=?", (Status.PENDING_SUBMIT.value, broker_order_id, timestamp, intent_id))

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
            observation["instrument"] = parsed_payload.get("instrument") if isinstance(parsed_payload, dict) else None
            observation["operation"] = parsed_payload.get("operation", "PLACE") if isinstance(parsed_payload, dict) else "PLACE"
            observation["target_order_id"] = parsed_payload.get("target_order_id") if isinstance(parsed_payload, dict) else None
            observation["operation_payload"] = parsed_payload.get("operation_payload") if isinstance(parsed_payload, dict) else None
            observation["risk_reasons"] = [reason for reason in parsed_reasons if isinstance(reason, str)] if isinstance(parsed_reasons, list) else []
            observations.append(observation)
        return observations


class OrderCoordinator:
    """One ingress point for manual UI and strategy intents; it owns no broker."""

    def __init__(self, store: IntentStore, gate: Gate = Gate.DISARMED, risk: RiskEngine | None = None) -> None:
        self.store = store
        self.gate = gate
        self.risk = risk or RiskEngine()

    def accept(self, intent: Intent) -> tuple[Status, Verdict]:
        verdict = self.risk.evaluate(intent, self.gate)
        return self.store.persist(intent, verdict), verdict
