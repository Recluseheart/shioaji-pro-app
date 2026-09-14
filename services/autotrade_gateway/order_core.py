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
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
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
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    MANUAL_REVIEW = "MANUAL_REVIEW"


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
    intent_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=now)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reasons: tuple[str, ...] = ()


class RiskEngine:
    """Small fail-closed policy seam; richer legacy limits plug in here later."""

    def evaluate(self, intent: Intent, gate: Gate) -> Verdict:
        reasons: list[str] = []
        if gate is not Gate.ARMED:
            reasons.append("trading_gate_not_armed")
        if not intent.account_id.startswith("SIM-"):
            reasons.append("simulation_account_required")
        if not intent.symbol or not intent.symbol.isalnum():
            reasons.append("invalid_symbol")
        if intent.side not in {"BUY", "SELL"}:
            reasons.append("invalid_side")
        if intent.quantity <= 0:
            reasons.append("invalid_quantity")
        try:
            if float(intent.limit_price) <= 0:
                reasons.append("invalid_limit_price")
        except ValueError:
            reasons.append("invalid_limit_price")
        if intent.source is Source.STRATEGY and (not intent.strategy_id or not intent.run_id):
            reasons.append("strategy_identity_required")
        return Verdict(not reasons, tuple(reasons))


class SQLiteIntentStore:
    """SQLite test/dev implementation of the atomic intent and outbox contract."""

    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_intents (
                intent_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
                source TEXT NOT NULL, strategy_id TEXT, run_id TEXT,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_outbox (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def persist(self, intent: Intent, verdict: Verdict) -> Status:
        status = Status.APPROVED if verdict.allowed else Status.RISK_REJECTED
        payload = asdict(intent)
        payload["source"] = intent.source.value
        payload["created_at"] = intent.created_at.isoformat()
        timestamp = now().isoformat()
        with self.connection:
            self.connection.execute(
                """INSERT INTO order_intents(
                    intent_id, correlation_id, source, strategy_id, run_id, payload_json,
                    status, risk_reasons_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    intent.intent_id, intent.correlation_id, intent.source.value,
                    intent.strategy_id, intent.run_id, json.dumps(payload, separators=(",", ":")),
                    status.value, json.dumps(verdict.reasons), intent.created_at.isoformat(), timestamp,
                ),
            )
            if verdict.allowed:
                self.connection.execute(
                    "INSERT INTO execution_outbox(intent_id, state, created_at, updated_at) VALUES (?, 'PENDING', ?, ?)",
                    (intent.intent_id, timestamp, timestamp),
                )
        return status

    def status(self, intent_id: str) -> Status:
        row = self.connection.execute("SELECT status FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return Status(row["status"])

    def outbox_state(self, intent_id: str) -> str | None:
        row = self.connection.execute("SELECT state FROM execution_outbox WHERE intent_id=?", (intent_id,)).fetchone()
        return str(row["state"]) if row is not None else None

    def transition(self, intent_id: str, target: Status) -> None:
        current = self.status(intent_id)
        if current is Status.UNKNOWN and target is not Status.RECONCILING:
            raise ValueError("UNKNOWN orders require reconciliation; they cannot be resent")
        if current is Status.RECONCILING and target is Status.APPROVED:
            raise ValueError("reconciliation cannot enqueue a fresh order")
        with self.connection:
            self.connection.execute(
                "UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?",
                (target.value, now().isoformat(), intent_id),
            )

    def recover(self) -> int:
        """Mark every delivery-ambiguous row UNKNOWN without re-enqueueing it."""
        uncertain = tuple(status.value for status in _UNCERTAIN)
        marks = ",".join("?" for _ in uncertain)
        timestamp = now().isoformat()
        with self.connection:
            cursor = self.connection.execute(
                f"UPDATE order_intents SET status=?, updated_at=? WHERE status IN ({marks})",
                (Status.UNKNOWN.value, timestamp, *uncertain),
            )
            self.connection.execute(
                "UPDATE execution_outbox SET state='UNKNOWN', updated_at=? WHERE state IN ('PENDING', 'LEASED')",
                (timestamp,),
            )
        return cursor.rowcount


class OrderCoordinator:
    """One ingress point for manual UI and strategy intents; it owns no broker."""

    def __init__(self, store: SQLiteIntentStore, gate: Gate = Gate.DISARMED, risk: RiskEngine | None = None) -> None:
        self.store = store
        self.gate = gate
        self.risk = risk or RiskEngine()

    def accept(self, intent: Intent) -> tuple[Status, Verdict]:
        verdict = self.risk.evaluate(intent, self.gate)
        return self.store.persist(intent, verdict), verdict
