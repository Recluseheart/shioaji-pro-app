"""PostgreSQL implementation of the Gateway intent/outbox safety contract."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from threading import Lock
from uuid import uuid4

from .order_core import (
    _UNCERTAIN,
    DuplicateIntentError,
    Intent,
    StrategyRun,
    StrategyDefinition,
    StrategySignal,
    Status,
    Verdict,
    intent_from_persisted_payload,
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
            CREATE TABLE IF NOT EXISTS risk_decisions (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                allowed BOOLEAN NOT NULL, reasons_json TEXT NOT NULL, decided_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY, intent_id TEXT REFERENCES order_intents(intent_id),
                event_type TEXT NOT NULL, details_json TEXT NOT NULL, occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_events_occurred_idx
                ON audit_events(occurred_at DESC, event_id DESC);
            CREATE TABLE IF NOT EXISTS broker_orders (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                broker_order_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_cases (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, reason TEXT NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS execution_outbox_state_idx ON execution_outbox(state, created_at);
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
                strategy_id TEXT PRIMARY KEY, state TEXT NOT NULL, enabled BOOLEAN NOT NULL,
                allowed_symbols_json TEXT NOT NULL
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
        self.connection.execute("ALTER TABLE order_intents ADD COLUMN IF NOT EXISTS broker_order_id TEXT")
        self.connection.execute(
            """INSERT INTO broker_orders(intent_id, broker_order_id, created_at, updated_at)
            SELECT intent_id, broker_order_id, updated_at, updated_at FROM order_intents
            WHERE broker_order_id IS NOT NULL AND broker_order_id != ''
            ON CONFLICT DO NOTHING""",
        )
        self.connection.execute(
            """INSERT INTO risk_decisions(intent_id, allowed, reasons_json, decided_at)
            SELECT intent_id, status <> 'RISK_REJECTED', risk_reasons_json, updated_at
            FROM order_intents ON CONFLICT DO NOTHING""",
        )

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
            self.connection.execute(
                "INSERT INTO risk_decisions(intent_id, allowed, reasons_json, decided_at) VALUES (%s,%s,%s,%s)",
                (intent.intent_id, verdict.allowed, json.dumps(verdict.reasons), timestamp),
            )
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (%s, %s, 'INTENT_EVALUATED', %s, %s)""",
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

    def load_intent(self, intent_id: str) -> Intent:
        with self._lock:
            row = self.connection.execute(
                "SELECT payload_json FROM order_intents WHERE intent_id=%s", (intent_id,),
            ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return intent_from_persisted_payload(str(row["payload_json"]))

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
            timestamp = now().isoformat()
            self.connection.execute("UPDATE order_intents SET status=%s, updated_at=%s WHERE intent_id=%s", (target.value, timestamp, intent_id))
            if target is Status.RECONCILING:
                self.connection.execute(
                    "UPDATE reconciliation_cases SET state='RECONCILING', updated_at=%s WHERE intent_id=%s AND state='OPEN'",
                    (timestamp, intent_id),
                )

    def recover(self) -> int:
        uncertain = tuple(status.value for status in _UNCERTAIN)
        timestamp = now().isoformat()
        with self._lock, self.connection.transaction():
            self.connection.execute(
                """INSERT INTO reconciliation_cases(intent_id,state,reason,opened_at,updated_at)
                SELECT intent_id,'OPEN','restart_recovery',%s,%s FROM order_intents WHERE status = ANY(%s)
                ON CONFLICT DO NOTHING""", (timestamp, timestamp, list(uncertain)),
            )
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
            self.connection.execute(
                """INSERT INTO reconciliation_cases(intent_id,state,reason,opened_at,updated_at)
                VALUES (%s,'OPEN',%s,%s,%s) ON CONFLICT DO NOTHING""",
                (intent_id, reason, timestamp, timestamp),
            )
            self.connection.execute("UPDATE execution_outbox SET state='UNKNOWN', updated_at=%s WHERE intent_id=%s AND state='LEASED'", (timestamp, intent_id))
            self.connection.execute("UPDATE order_intents SET status=%s, updated_at=%s WHERE intent_id=%s", (Status.UNKNOWN.value, timestamp, intent_id))

    def mark_pending_submit(self, intent_id: str, broker_order_id: str) -> None:
        if not broker_order_id:
            self.mark_delivery_unknown(intent_id, "missing_broker_identity")
            return
        timestamp = now().isoformat()
        with self._lock, self.connection.transaction():
            self.connection.execute(
                """INSERT INTO broker_orders(intent_id, broker_order_id, created_at, updated_at)
                VALUES (%s,%s,%s,%s)""", (intent_id, broker_order_id, timestamp, timestamp),
            )
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
            self.connection.execute(
                """INSERT INTO audit_events(event_id, intent_id, event_type, details_json, occurred_at)
                VALUES (%s, %s, 'BROKER_TERMINAL_CALLBACK', %s, %s)""",
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

    def record_signal(self, signal: StrategySignal) -> None:
        """Persist a signal once; it never changes a broker delivery state."""
        recorded_at = now().isoformat()
        with self._lock, self.connection.transaction():
            self.connection.execute(
                """INSERT INTO strategy_signals(
                    signal_id, strategy_id, run_id, event_id, signal_type, symbol, side,
                    intent_id, correlation_id, status, risk_reasons_json, observed_at, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
        with self._lock, self.connection.transaction():
            self.connection.execute(
                """INSERT INTO strategy_definitions(strategy_id, state, enabled, allowed_symbols_json)
                VALUES (%s, %s, %s, %s) ON CONFLICT(strategy_id) DO NOTHING""",
                (definition.strategy_id, definition.state, definition.enabled,
                 json.dumps(definition.allowed_symbols)),
            )
            self.connection.execute(
                """INSERT INTO strategy_versions(strategy_id, version, config_checksum)
                VALUES (%s, %s, %s) ON CONFLICT(strategy_id, version) DO NOTHING""",
                (definition.strategy_id, definition.version, definition.config_checksum),
            )

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
        with self._lock, self.connection.transaction():
            self.connection.execute(
                """INSERT INTO strategy_runs(
                    run_id, strategy_id, version, config_checksum, state, last_intent_id,
                    last_status, intent_count, started_at, heartbeat_at, ended_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (%s, %s)
                    ON CONFLICT DO NOTHING""", (run.run_id, run.heartbeat_at.isoformat()),
                )

    def record_strategy_heartbeat(self, run_id: str, observed_at: datetime) -> None:
        """Record a monotonic liveness fact for an existing durable run."""
        timestamp = observed_at.isoformat()
        with self._lock, self.connection.transaction():
            cursor = self.connection.execute(
                """UPDATE strategy_runs SET heartbeat_at=CASE
                    WHEN heartbeat_at IS NULL OR heartbeat_at < %s THEN %s ELSE heartbeat_at END,
                    updated_at=CASE WHEN updated_at < %s THEN %s ELSE updated_at END
                    WHERE run_id=%s""",
                (timestamp, timestamp, timestamp, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            self.connection.execute(
                """INSERT INTO strategy_heartbeats(run_id, observed_at) VALUES (%s, %s)
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
