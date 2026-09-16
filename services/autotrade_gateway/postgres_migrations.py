"""Forward-only PostgreSQL migrations for the Autotrade durable journal.

The Gateway owns a schema, not a database.  A migration ledger makes a
restart deterministic and auditable: migrations are applied inside the target
schema only, checksummed, and never rolled back automatically.  If a deployed
migration needs correction, add a new forward migration; do not mutate a
recorded migration or rewrite broker/audit history.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        source = "\n".join((str(self.version), self.name, *self.statements))
        return sha256(source.encode()).hexdigest()


MIGRATIONS = (
    Migration(
        1,
        "durable_intent_and_strategy_schema",
        (
            """CREATE TABLE IF NOT EXISTS order_intents (
                intent_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
                source TEXT NOT NULL, strategy_id TEXT, run_id TEXT,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL, broker_order_id TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS execution_outbox (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS risk_decisions (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                allowed BOOLEAN NOT NULL, reasons_json TEXT NOT NULL, decided_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY, intent_id TEXT REFERENCES order_intents(intent_id),
                event_type TEXT NOT NULL, details_json TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS audit_events_occurred_idx ON audit_events(occurred_at DESC, event_id DESC)",
            """CREATE TABLE IF NOT EXISTS gateway_controls (
                control_name TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, applied_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                broker_order_id TEXT NOT NULL, quantity INTEGER NOT NULL CHECK (quantity > 0),
                price NUMERIC(20,8) NOT NULL CHECK (price > 0), filled_at TIMESTAMPTZ NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS fills_intent_idx ON fills(intent_id, filled_at DESC, fill_id DESC)",
            """CREATE TABLE IF NOT EXISTS broker_orders (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                broker_order_id TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS reconciliation_cases (
                intent_id TEXT PRIMARY KEY REFERENCES order_intents(intent_id),
                state TEXT NOT NULL, reason TEXT NOT NULL, opened_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS reconciliation_refresh_requests (
                idempotency_key TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                reason TEXT NOT NULL, state TEXT NOT NULL, receipt_json TEXT, applied_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ
            )""",
            "CREATE INDEX IF NOT EXISTS reconciliation_refresh_pending_intent_idx ON reconciliation_refresh_requests(intent_id) WHERE state='PENDING'",
            "CREATE INDEX IF NOT EXISTS execution_outbox_state_idx ON execution_outbox(state, created_at)",
            """CREATE TABLE IF NOT EXISTS strategy_signals (
                signal_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
                signal_type TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
                correlation_id TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(strategy_id, event_id)
            )""",
            "CREATE INDEX IF NOT EXISTS strategy_signals_observed_idx ON strategy_signals(observed_at DESC, signal_id DESC)",
            """CREATE TABLE IF NOT EXISTS strategy_definitions (
                strategy_id TEXT PRIMARY KEY, state TEXT NOT NULL, enabled BOOLEAN NOT NULL,
                allowed_symbols_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_control_requests (
                idempotency_key TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                action TEXT NOT NULL, reason TEXT NOT NULL, receipt_json TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_versions (
                strategy_id TEXT NOT NULL REFERENCES strategy_definitions(strategy_id),
                version TEXT NOT NULL, config_checksum TEXT NOT NULL,
                PRIMARY KEY(strategy_id, version)
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_runs (
                run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                version TEXT NOT NULL, config_checksum TEXT NOT NULL,
                state TEXT NOT NULL, last_intent_id TEXT REFERENCES order_intents(intent_id),
                last_status TEXT, intent_count INTEGER NOT NULL CHECK (intent_count >= 0),
                started_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS strategy_runs_updated_idx ON strategy_runs(updated_at DESC, run_id DESC)",
            """CREATE TABLE IF NOT EXISTS strategy_heartbeats (
                run_id TEXT NOT NULL REFERENCES strategy_runs(run_id),
                observed_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(run_id, observed_at)
            )""",
        ),
    ),
    Migration(
        2,
        "legacy_broker_and_risk_backfill",
        (
            "ALTER TABLE order_intents ADD COLUMN IF NOT EXISTS broker_order_id TEXT",
            """INSERT INTO broker_orders(intent_id, broker_order_id, created_at, updated_at)
            SELECT intent_id, broker_order_id, updated_at, updated_at FROM order_intents
            WHERE broker_order_id IS NOT NULL AND broker_order_id != ''
            ON CONFLICT DO NOTHING""",
            """INSERT INTO risk_decisions(intent_id, allowed, reasons_json, decided_at)
            SELECT intent_id, status <> 'RISK_REJECTED', risk_reasons_json, updated_at
            FROM order_intents ON CONFLICT DO NOTHING""",
        ),
    ),
)


def apply_postgres_migrations(connection: Any, schema: str) -> None:
    """Apply the immutable migration sequence once for a validated schema."""
    connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    connection.execute(f'SET search_path TO "{schema}"')
    connection.execute(
        """CREATE TABLE IF NOT EXISTS gateway_schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )
    applied = {
        int(row["version"]): (str(row["name"]), str(row["checksum"]))
        for row in connection.execute(
            "SELECT version, name, checksum FROM gateway_schema_migrations",
        ).fetchall()
    }
    for migration in MIGRATIONS:
        recorded = applied.get(migration.version)
        expected = (migration.name, migration.checksum)
        if recorded is not None:
            if recorded != expected:
                raise RuntimeError(
                    f"PostgreSQL migration {migration.version} does not match its recorded checksum",
                )
            continue
        with connection.transaction():
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO gateway_schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
