from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from services.autotrade_gateway.order_core import DuplicateIntentError, Gate, Intent, IntentStore, OrderCoordinator, RiskContext, Source, Status
from services.autotrade_gateway.worker import ExecutionWorker


def armed_coordinator(store: IntentStore) -> OrderCoordinator:
    return OrderCoordinator(
        store,
        Gate.ARMED,
        risk_context_provider=lambda: RiskContext(
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
