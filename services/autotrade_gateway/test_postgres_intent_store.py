from __future__ import annotations

import os
import unittest
from uuid import uuid4

from services.autotrade_gateway.order_core import Gate, Intent, OrderCoordinator, Source, Status
from services.autotrade_gateway.worker import ExecutionWorker


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
        status, verdict = OrderCoordinator(self.store, Gate.ARMED).accept(intent)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        self.assertEqual(self.store.outbox_state(intent.intent_id), "PENDING")
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "require reconciliation"):
            self.store.transition(intent.intent_id, Status.SUBMITTED)

    def test_worker_timeout_is_unknown_without_retry(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store, Gate.ARMED).accept(intent)
        class TimeoutDispatcher:
            def dispatch(self, _intent_id: str) -> None: raise TimeoutError()
        worker = ExecutionWorker(self.store, TimeoutDispatcher())
        self.assertTrue(worker.dispatch_once())
        self.assertFalse(worker.dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)

    def test_broker_identity_is_persisted_with_pending_submit(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store, Gate.ARMED).accept(intent)

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
        OrderCoordinator(self.store, Gate.ARMED).accept(intent)

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
