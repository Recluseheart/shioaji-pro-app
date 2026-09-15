"""PostgreSQL implementation of the Gateway intent/outbox safety contract."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from threading import Lock

from .order_core import (
    _UNCERTAIN,
    DuplicateIntentError,
    Intent,
    Status,
    Verdict,
    now,
    persisted_request_matches,
)


class PostgresIntentStore:
    """Schema-scoped store; import psycopg only when PostgreSQL is configured."""

    def __init__(self, dsn: str, schema: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("PostgreSQL schema name is invalid")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError("install psycopg[binary] to use AUTOTRADE_GATEWAY_DATABASE_URL") from error
        self._lock = Lock()
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        self.connection.execute(f'SET search_path TO "{schema}"')
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_intents (
                intent_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
                source TEXT NOT NULL, strategy_id TEXT, run_id TEXT,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                broker_order_id TEXT
            );
            CREATE TABLE IF NOT EXISTS execution_outbox (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS execution_outbox_state_idx ON execution_outbox(state, created_at);
            """
        )
        self.connection.execute("ALTER TABLE order_intents ADD COLUMN IF NOT EXISTS broker_order_id TEXT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def persist(self, intent: Intent, verdict: Verdict) -> Status:
        status = Status.APPROVED if verdict.allowed else Status.RISK_REJECTED
        payload = asdict(intent)
        payload["source"] = intent.source.value
        payload["created_at"] = intent.created_at.isoformat()
        timestamp = now().isoformat()
        with self._lock, self.connection.transaction():
            cursor = self.connection.execute(
                """INSERT INTO order_intents(
                    intent_id, correlation_id, source, strategy_id, run_id, payload_json,
                    status, risk_reasons_json, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(intent_id) DO NOTHING""",
                (intent.intent_id, intent.correlation_id, intent.source.value, intent.strategy_id,
                 intent.run_id, json.dumps(payload, separators=(",", ":")), status.value,
                 json.dumps(verdict.reasons), intent.created_at.isoformat(), timestamp),
            )
            if cursor.rowcount != 1:
                raise DuplicateIntentError(intent.intent_id)
            if verdict.allowed:
                self.connection.execute(
                    "INSERT INTO execution_outbox(intent_id,state,created_at,updated_at) VALUES (%s,'PENDING',%s,%s)",
                    (intent.intent_id, timestamp, timestamp),
                )
        return status

    def status(self, intent_id: str) -> Status:
        with self._lock:
            row = self.connection.execute("SELECT status FROM order_intents WHERE intent_id=%s", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return Status(row["status"])

    def has_intent(self, intent_id: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM order_intents WHERE intent_id=%s", (intent_id,),
            ).fetchone()
        return row is not None

    def request_matches(self, intent: Intent) -> bool:
        """Return true only when an existing durable retry has identical input."""
        with self._lock:
            row = self.connection.execute(
                "SELECT payload_json FROM order_intents WHERE intent_id=%s", (intent.intent_id,),
            ).fetchone()
        return row is not None and persisted_request_matches(intent, str(row["payload_json"]))

    def outbox_state(self, intent_id: str) -> str | None:
        with self._lock:
            row = self.connection.execute("SELECT state FROM execution_outbox WHERE intent_id=%s", (intent_id,)).fetchone()
        return str(row["state"]) if row else None

    def transition(self, intent_id: str, target: Status) -> None:
        current = self.status(intent_id)
        if current is Status.UNKNOWN and target is not Status.RECONCILING:
            raise ValueError("UNKNOWN orders require reconciliation; they cannot be resent")
        if current is Status.RECONCILING and target is Status.APPROVED:
            raise ValueError("reconciliation cannot enqueue a fresh order")
        with self._lock, self.connection.transaction():
            self.connection.execute("UPDATE order_intents SET status=%s, updated_at=%s WHERE intent_id=%s", (target.value, now().isoformat(), intent_id))

    def recover(self) -> int:
        uncertain = tuple(status.value for status in _UNCERTAIN)
        timestamp = now().isoformat()
        with self._lock, self.connection.transaction():
            cursor = self.connection.execute(
                "UPDATE order_intents SET status=%s, updated_at=%s WHERE status = ANY(%s)",
                (Status.UNKNOWN.value, timestamp, list(uncertain)),
            )
            self.connection.execute("UPDATE execution_outbox SET state='UNKNOWN', updated_at=%s WHERE state IN ('PENDING','LEASED')", (timestamp,))
        return cursor.rowcount

    def claim_outbox(self) -> str | None:
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """WITH candidate AS (
                    SELECT intent_id FROM execution_outbox WHERE state='PENDING'
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                ) UPDATE execution_outbox AS outbox SET state='LEASED', updated_at=%s
                FROM candidate WHERE outbox.intent_id=candidate.intent_id
                RETURNING outbox.intent_id""",
                (now().isoformat(),),
            ).fetchone()
        return str(row["intent_id"]) if row else None

    def mark_delivery_unknown(self, intent_id: str, reason: str) -> None:
        timestamp = now().isoformat()
        with self._lock, self.connection.transaction():
            self.connection.execute("UPDATE execution_outbox SET state='UNKNOWN', updated_at=%s WHERE intent_id=%s AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=%s, updated_at=%s WHERE intent_id=%s", (Status.UNKNOWN.value, timestamp, intent_id))

    def mark_pending_submit(self, intent_id: str, broker_order_id: str) -> None:
        if not broker_order_id:
            self.mark_delivery_unknown(intent_id, "missing_broker_identity")
            return
        timestamp = now().isoformat()
        with self._lock, self.connection.transaction():
            self.connection.execute("UPDATE execution_outbox SET state='DISPATCHED', updated_at=%s WHERE intent_id=%s AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=%s, broker_order_id=%s, updated_at=%s WHERE intent_id=%s", (Status.PENDING_SUBMIT.value, broker_order_id, timestamp, intent_id))

    def record_terminal_callback(self, intent_id: str, broker_order_id: str, terminal: Status) -> None:
        if terminal not in {Status.FILLED, Status.CANCELLED, Status.BROKER_REJECTED}:
            raise ValueError("callback status must be terminal")
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                "SELECT status, broker_order_id FROM order_intents WHERE intent_id=%s FOR UPDATE", (intent_id,),
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
                "UPDATE order_intents SET status=%s, updated_at=%s WHERE intent_id=%s", (terminal.value, timestamp, intent_id))
            self.connection.execute(
                "UPDATE execution_outbox SET state='TERMINAL', updated_at=%s WHERE intent_id=%s", (timestamp, intent_id))

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
