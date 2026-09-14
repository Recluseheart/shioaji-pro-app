from __future__ import annotations

import os
import unittest
from uuid import uuid4

from services.autotrade_gateway.order_core import Gate, Intent, OrderCoordinator, Source, Status


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
