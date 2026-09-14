from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.autotrade_gateway.order_core import (
    Gate,
    Intent,
    OrderCoordinator,
    Source,
    SQLiteIntentStore,
    Status,
)
from services.autotrade_gateway.worker import ExecutionWorker


class OrderCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = SQLiteIntentStore(Path(self.temp_dir.name) / "gateway-test.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_disarmed_manual_intent_is_audited_and_rejected_without_outbox(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        status, verdict = OrderCoordinator(self.store).accept(intent)
        self.assertEqual(status, Status.RISK_REJECTED)
        self.assertIn("trading_gate_not_armed", verdict.reasons)
        self.assertEqual(self.store.status(intent.intent_id), Status.RISK_REJECTED)
        self.assertIsNone(self.store.outbox_state(intent.intent_id))

    def test_strategy_identity_and_source_are_required_for_common_ingress(self) -> None:
        incomplete = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000")
        status, verdict = OrderCoordinator(self.store, Gate.ARMED).accept(incomplete)
        self.assertEqual(status, Status.RISK_REJECTED)
        self.assertIn("strategy_identity_required", verdict.reasons)

        strategy = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "reference", "run-1")
        status, verdict = OrderCoordinator(self.store, Gate.ARMED).accept(strategy)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        self.assertEqual(self.store.outbox_state(strategy.intent_id), "PENDING")

    def test_restart_marks_pending_delivery_unknown_and_forbids_blind_resend(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        status, _ = OrderCoordinator(self.store, Gate.ARMED).accept(intent)
        self.assertEqual(status, Status.APPROVED)
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "require reconciliation"):
            self.store.transition(intent.intent_id, Status.SUBMITTED)

    def test_timeout_claim_becomes_unknown_and_is_not_retried(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store, Gate.ARMED).accept(intent)

        class TimeoutDispatcher:
            calls = 0
            def dispatch(self, _intent_id: str) -> None:
                self.calls += 1
                raise TimeoutError()

        dispatcher = TimeoutDispatcher()
        worker = ExecutionWorker(self.store, dispatcher)
        self.assertTrue(worker.dispatch_once())
        self.assertFalse(worker.dispatch_once())
        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")

    def test_broker_identity_advances_claim_to_pending_submit(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store, Gate.ARMED).accept(intent)
        class AcceptedDispatcher:
            def dispatch(self, _intent_id: str) -> str: return "MOCK-ORDER-1"
        self.assertTrue(ExecutionWorker(self.store, AcceptedDispatcher()).dispatch_once())
        self.assertEqual(self.store.status(intent.intent_id), Status.PENDING_SUBMIT)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "DISPATCHED")


if __name__ == "__main__":
    unittest.main()
