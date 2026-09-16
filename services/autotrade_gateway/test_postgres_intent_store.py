from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from services.autotrade_gateway.order_core import DuplicateIntentError, Fill, Gate, Intent, IntentStore, OrderCoordinator, RiskContext, Source, Status, StrategyDefinition, StrategyRun, StrategySignal
from services.autotrade_gateway.simulation_execution_window import SimulationExecutionWindow
from services.autotrade_gateway.worker import ExecutionWorker


def armed_coordinator(store: IntentStore) -> OrderCoordinator:
    return OrderCoordinator(
        store,
        Gate.ARMED,
        risk_context_provider=lambda _intent: RiskContext(
            quote_price=Decimal("1000"),
            quote_at=datetime.now(timezone.utc),
        ),
    )


class PostgresIntentStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.getenv("AUTOTRADE_GATEWAY_TEST_POSTGRES_DSN")
        if not cls.dsn:
            raise unittest.SkipTest("AUTOTRADE_GATEWAY_TEST_POSTGRES_DSN is required")
        import psycopg

        cls.psycopg = psycopg
        cls.schema = f"gateway_it_{uuid4().hex}"
        from services.autotrade_gateway.postgres_intent_store import PostgresIntentStore

        cls.store = PostgresIntentStore(cls.dsn, cls.schema)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()
        with cls.psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')

    def setUp(self) -> None:
        # Each test shares the per-class disposable schema but must not share
        # durable observations. CASCADE is constrained to this UUID schema's
        # order and strategy graphs, including outbox/signal dependents.
        self.store.connection.execute(
            "TRUNCATE strategy_definitions, order_intents, gateway_controls CASCADE"
        )

    def test_atomic_outbox_and_unknown_recovery(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        status, verdict = armed_coordinator(self.store).accept(intent)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        self.assertEqual(self.store.outbox_state(intent.intent_id), "PENDING")
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "require reconciliation"):
            self.store.transition(intent.intent_id, Status.SUBMITTED)
        self.store.transition(intent.intent_id, Status.RECONCILING)
        self.assertEqual(self.store.observation_reconciliation_cases()[0]["state"], "RECONCILING")

    def test_schema_migrations_are_recorded_and_safe_to_rerun(self) -> None:
        """A restart applies no hidden DDL and preserves the durable journal."""
        initial = self.store.connection.execute(
            "SELECT version, name FROM gateway_schema_migrations ORDER BY version",
        ).fetchall()
        self.assertEqual(
            [dict(row) for row in initial],
            [
                {"version": 1, "name": "durable_intent_and_strategy_schema"},
                {"version": 2, "name": "legacy_broker_and_risk_backfill"},
            ],
        )
        columns = self.store.connection.execute(
            """SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema=%s
              AND (table_name, column_name) IN (
                  ('order_intents', 'created_at'),
                  ('order_intents', 'updated_at'),
                  ('fills', 'price')
              )
            ORDER BY table_name, column_name""",
            (self.schema,),
        ).fetchall()
        self.assertEqual(
            [dict(row) for row in columns],
            [
                {"table_name": "fills", "column_name": "price", "data_type": "numeric", "numeric_precision": 20, "numeric_scale": 8},
                {"table_name": "order_intents", "column_name": "created_at", "data_type": "timestamp with time zone", "numeric_precision": None, "numeric_scale": None},
                {"table_name": "order_intents", "column_name": "updated_at", "data_type": "timestamp with time zone", "numeric_precision": None, "numeric_scale": None},
            ],
        )

        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store).accept(intent)
        from services.autotrade_gateway.postgres_intent_store import PostgresIntentStore

        restarted = PostgresIntentStore(self.dsn, self.schema)
        try:
            self.assertEqual(restarted.status(intent.intent_id), Status.RISK_REJECTED)
            rerun = restarted.connection.execute(
                "SELECT version, name FROM gateway_schema_migrations ORDER BY version",
            ).fetchall()
            self.assertEqual([dict(row) for row in rerun], [dict(row) for row in initial])
        finally:
            restarted.close()

    def test_kill_switch_is_persistent_and_never_rearms_the_store(self) -> None:
        self.assertEqual(self.store.apply_kill_switch("operator test", "kill-test-1"), (
            Gate.KILLED, "operator test", True,
        ))
        self.assertEqual(self.store.apply_kill_switch("later reason", "kill-test-2"), (
            Gate.KILLED, "operator test", False,
        ))
        self.assertEqual(self.store.trading_gate(), Gate.KILLED)
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "KILL_SWITCH_APPLIED")

    def test_request_match_requires_every_broker_relevant_field_to_match(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store).accept(intent)
        self.assertTrue(self.store.request_matches(replace(
            intent,
            correlation_id="postgres-retry-correlation",
        )))
        self.assertFalse(self.store.request_matches(replace(intent, quantity=2)))
        self.assertFalse(self.store.request_matches(replace(intent, operation="CANCEL")))

    def test_duplicate_persist_is_an_explicit_atomic_retry_signal(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        coordinator = OrderCoordinator(self.store)
        coordinator.accept(intent)
        with self.assertRaises(DuplicateIntentError):
            coordinator.accept(intent)
        self.assertEqual(len(self.store.observation_orders()), 1)

    def test_risk_verdict_is_durable_separately_from_its_intent(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        status, verdict = OrderCoordinator(self.store, Gate.DISARMED).accept(intent)
        self.assertEqual((status, verdict.allowed), (Status.RISK_REJECTED, False))
        decision = self.store.connection.execute(
            "SELECT allowed, reasons_json FROM risk_decisions WHERE intent_id=%s", (intent.intent_id,),
        ).fetchone()
        self.assertEqual(dict(decision), {
            "allowed": False, "reasons_json": '["trading_gate_not_armed"]',
        })
        event = self.store.observation_audit_events()[0]
        self.assertEqual(event["intent_id"], intent.intent_id)
        self.assertEqual(event["event_type"], "INTENT_EVALUATED")
        self.assertEqual(event["details"], {
            "allowed": False, "risk_reasons": ["trading_gate_not_armed"],
            "status": "RISK_REJECTED",
        })

    def test_observations_have_a_stable_secondary_order_when_timestamps_tie(self) -> None:
        first = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000", intent_id="intent-a")
        second = Intent(Source.MANUAL_UI, "SIM-001", "2317", "BUY", 1, "1000", intent_id="intent-z")
        coordinator = OrderCoordinator(self.store)
        coordinator.accept(first)
        coordinator.accept(second)
        self.store.connection.execute(
            "UPDATE order_intents SET updated_at='2026-01-01T00:00:00+00:00'",
        )
        self.assertEqual(
            [item["intent_id"] for item in self.store.observation_orders()],
            ["intent-z", "intent-a"],
        )

    def test_worker_timeout_is_unknown_without_retry(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        class TimeoutDispatcher:
            def dispatch(self, _intent_id: str) -> None: raise TimeoutError()
        worker = ExecutionWorker(self.store, TimeoutDispatcher())
        self.assertTrue(worker.dispatch_once())
        self.assertFalse(worker.dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "DELIVERY_UNKNOWN")
        self.assertEqual(self.store.observation_audit_events()[0]["details"], {
            "reason": "dispatch_uncertain:TimeoutError",
        })

    def test_controlled_window_keeps_postgres_gate_disarmed_after_one_dispatch(self) -> None:
        class Dispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "POSTGRES-CONTROLLED-WINDOW-1"

        window = SimulationExecutionWindow(
            self.store,
            ExecutionWorker(self.store, Dispatcher()),
            authorization="postgres-window-authorization",
            risk_context_provider=lambda _intent: RiskContext(
                Decimal("1000"), datetime.now(timezone.utc)
            ),
        )
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        result = window.execute(
            intent,
            authorization="postgres-window-authorization",
            reason="postgres controlled-window contract",
        )

        self.assertTrue(result.dispatched)
        self.assertEqual(result.status, Status.PENDING_SUBMIT)
        self.assertEqual(self.store.trading_gate(), Gate.DISARMED)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "DISPATCHED")
        self.assertEqual(
            self.store.observation_audit_events()[0]["event_type"],
            "SIMULATION_WINDOW_FINISHED",
        )

    def test_broker_identity_is_persisted_with_pending_submit(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)

        class AcceptedDispatcher:
            def dispatch(self, _intent_id: str) -> str:
                return "POSTGRES-MOCK-ORDER-1"

        self.assertTrue(ExecutionWorker(self.store, AcceptedDispatcher()).dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.PENDING_SUBMIT)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "DISPATCHED")
        self.assertEqual(
            self.store.observation_orders()[0]["broker_order_id"],
            "POSTGRES-MOCK-ORDER-1",
        )
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "BROKER_ORDER_BOUND")
        self.assertEqual(self.store.observation_audit_events()[0]["details"], {
            "broker_order_id": "POSTGRES-MOCK-ORDER-1",
        })

    def test_verified_target_lookup_excludes_unknown_and_terminal_orders(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        self.store.mark_pending_submit(intent.intent_id, "POSTGRES-TARGET-1")
        target = self.store.verified_target_intent("POSTGRES-TARGET-1")
        self.assertIsNotNone(target)
        self.assertEqual(target.account_id, "SIM-001")
        self.store.record_terminal_callback(intent.intent_id, "POSTGRES-TARGET-1", Status.CANCELLED)
        self.assertIsNone(self.store.verified_target_intent("POSTGRES-TARGET-1"))

    def test_bound_broker_order_accepts_a_valid_immutable_fill(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)

        class AcceptedDispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "POSTGRES-FILL-ORDER-1"

        self.assertTrue(ExecutionWorker(self.store, AcceptedDispatcher()).dispatch_once())
        self.store.record_fill(Fill(
            fill_id="postgres-fill-1", intent_id=intent.intent_id,
            broker_order_id="POSTGRES-FILL-ORDER-1", quantity=1, price="1001",
            filled_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        ))
        self.assertEqual(self.store.observation_fills(), [{
            "fill_id": "postgres-fill-1", "intent_id": intent.intent_id,
            "broker_order_id": "POSTGRES-FILL-ORDER-1", "quantity": 1, "price": "1001",
            "filled_at": "2026-09-15T00:00:00+00:00",
        }])
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "FILL_RECORDED")

    def test_reused_broker_identity_becomes_unknown_without_a_second_binding(self) -> None:
        first = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        second = Intent(Source.MANUAL_UI, "SIM-001", "2317", "BUY", 1, "1000")
        coordinator = armed_coordinator(self.store)
        coordinator.accept(first)
        coordinator.accept(second)

        class ReusedIdentityDispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "POSTGRES-REUSED-ORDER-1"

        worker = ExecutionWorker(self.store, ReusedIdentityDispatcher())
        self.assertTrue(worker.dispatch_once())
        self.assertTrue(worker.dispatch_once())
        self.assertEqual(self.store.status(first.intent_id), Status.PENDING_SUBMIT)
        self.assertEqual(self.store.status(second.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(second.intent_id), "UNKNOWN")

    def test_terminal_callback_requires_the_persisted_broker_identity(self) -> None:
        intent = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "reference", "run-1")
        armed_coordinator(self.store).accept(intent)

        class AcceptedDispatcher:
            def dispatch(self, _intent_id: str) -> str:
                return "POSTGRES-MOCK-TERMINAL-1"

        self.assertTrue(ExecutionWorker(self.store, AcceptedDispatcher()).dispatch_once())
        with self.assertRaisesRegex(ValueError, "identity"):
            self.store.record_terminal_callback(intent.intent_id, "OTHER", Status.FILLED)
        self.store.record_terminal_callback(intent.intent_id, "POSTGRES-MOCK-TERMINAL-1", Status.FILLED)
        self.store.record_terminal_callback(intent.intent_id, "POSTGRES-MOCK-TERMINAL-1", Status.FILLED)
        self.assertEqual(self.store.status(intent.intent_id), Status.FILLED)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "TERMINAL")
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "BROKER_TERMINAL_CALLBACK")
        self.assertEqual(self.store.observation_audit_events()[0]["details"], {
            "broker_order_id": "POSTGRES-MOCK-TERMINAL-1", "status": "FILLED",
        })

    def test_reconciled_terminal_resolves_unknown_with_the_bound_identity(self) -> None:
        """A broker read may finish UNKNOWN, but cannot create another dispatch."""
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        self.store.mark_pending_submit(intent.intent_id, "POSTGRES-RECONCILED-1")
        self.store.mark_delivery_unknown(intent.intent_id, "dispatch_uncertain:TimeoutError")

        self.store.record_reconciled_terminal(
            intent.intent_id, "POSTGRES-RECONCILED-1", Status.FILLED
        )

        self.assertEqual(self.store.status(intent.intent_id), Status.FILLED)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "TERMINAL")
        self.assertEqual(self.store.observation_reconciliation_cases()[0]["state"], "RESOLVED")
        self.assertEqual(
            self.store.observation_audit_events()[0]["event_type"],
            "BROKER_RECONCILED_TERMINAL",
        )

    def test_reconciliation_refresh_reservation_is_request_bound_and_durable(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        self.store.recover()
        self.assertTrue(self.store.begin_reconciliation_refresh("pg-refresh-1", intent.intent_id, "operator check"))
        self.assertEqual(self.store.status(intent.intent_id), Status.RECONCILING)
        self.assertFalse(self.store.begin_reconciliation_refresh("pg-refresh-1", intent.intent_id, "operator check"))
        self.store.complete_reconciliation_refresh("pg-refresh-1", intent.intent_id, "operator check", {
            "observed_terminal_facts": 0, "resolved_intents": 0, "status": "RECONCILING",
        })
        self.assertEqual(self.store.reconciliation_refresh_receipt("pg-refresh-1"), {
            "intent_id": intent.intent_id, "reason": "operator check", "state": "COMPLETE",
            "receipt": {"observed_terminal_facts": 0, "resolved_intents": 0, "status": "RECONCILING"},
        })
        with self.assertRaisesRegex(ValueError, "different reconciliation refresh"):
            self.store.begin_reconciliation_refresh("pg-refresh-1", intent.intent_id, "different reason")

    def test_signal_projection_is_durable_and_idempotent(self) -> None:
        intent = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "reference", "run-1")
        status, _ = OrderCoordinator(self.store).accept(intent)
        signal = StrategySignal(
            signal_id="signal-1", strategy_id="reference", run_id="run-1", event_id="event-1",
            signal_type="REFERENCE_SIGNAL", symbol="2330", side="BUY", intent_id=intent.intent_id,
            correlation_id=intent.correlation_id, status=status, risk_reasons=("trading_gate_not_armed",),
            observed_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
        self.store.record_signal(signal)
        self.store.record_signal(signal)
        self.assertEqual(self.store.observation_signals(), [{
            "signal_id": "signal-1", "strategy_id": "reference", "run_id": "run-1", "event_id": "event-1",
            "signal_type": "REFERENCE_SIGNAL", "symbol": "2330", "side": "BUY", "intent_id": intent.intent_id,
            "correlation_id": intent.correlation_id, "status": "RISK_REJECTED",
            "risk_reasons": ["trading_gate_not_armed"], "observed_at": "2026-09-15T00:00:00+00:00",
        }])

    def test_strategy_run_and_heartbeat_are_durable_without_dispatching(self) -> None:
        started_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.store.record_strategy_run(StrategyRun(
            run_id="fixture-run-1", strategy_id="reference-fixture", version="v1",
            config_checksum="fixture-checksum", state="FIXTURE_OBSERVED",
            last_intent_id=None, last_status=None, intent_count=0,
            started_at=started_at, heartbeat_at=started_at, ended_at=None,
            updated_at=started_at,
        ))
        heartbeat_at = datetime(2026, 9, 15, 0, 1, tzinfo=timezone.utc)
        self.store.record_strategy_heartbeat("fixture-run-1", heartbeat_at)
        self.assertEqual(self.store.observation_strategy_runs(), [{
            "run_id": "fixture-run-1", "strategy_id": "reference-fixture", "version": "v1",
            "config_checksum": "fixture-checksum", "state": "FIXTURE_OBSERVED",
            "last_intent_id": None, "last_status": None, "intent_count": 0,
            "started_at": started_at.isoformat(), "heartbeat_at": heartbeat_at.isoformat(),
            "ended_at": None, "updated_at": heartbeat_at.isoformat(),
        }])
        self.assertEqual(self.store.observation_orders(), [])

    def test_lifecycle_transition_updates_run_and_definition_together(self) -> None:
        started_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.store.record_strategy_definition(StrategyDefinition(
            strategy_id="reference-fixture", version="v1", config_checksum="fixture-checksum",
            state="DISABLED", enabled=False, allowed_symbols=("2330",),
        ))
        self.store.start_strategy_lifecycle(StrategyRun(
            run_id="atomic-run", strategy_id="reference-fixture", version="v1",
            config_checksum="fixture-checksum", state="RUNNING", last_intent_id=None,
            last_status=None, intent_count=0, started_at=started_at, heartbeat_at=started_at,
            ended_at=None, updated_at=started_at,
        ))
        self.store.transition_strategy_lifecycle(
            "atomic-run", "reference-fixture", "STOPPED", False, started_at,
        )
        self.assertEqual(self.store.observation_strategy_definitions()[0]["state"], "STOPPED")
        self.assertFalse(self.store.observation_strategy_definitions()[0]["enabled"])
        self.assertEqual(self.store.observation_strategy_runs()[0]["state"], "STOPPED")

    def test_strategy_definition_and_version_are_durable_without_enabling_a_runner(self) -> None:
        self.store.record_strategy_definition(StrategyDefinition(
            strategy_id="reference-fixture", version="v1", config_checksum="fixture-checksum",
            state="DISABLED", enabled=False, allowed_symbols=("2330",),
        ))
        self.assertEqual(self.store.observation_strategy_definitions(), [{
            "strategy_id": "reference-fixture", "version": "v1",
            "config_checksum": "fixture-checksum", "state": "DISABLED",
            "enabled": False, "allowed_symbols": ["2330"],
        }])
