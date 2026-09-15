from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.autotrade_gateway.order_core import Gate, OrderCoordinator, SQLiteIntentStore, Status
from services.autotrade_gateway.strategy_runtime import StrategyRuntime
from services.autotrade_gateway.worker import ExecutionWorker


class StrategyRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.store = SQLiteIntentStore(Path(self.temporary_directory.name) / "strategy-runtime.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_production_runtime_is_disabled_and_does_not_accept_fixture_events(self) -> None:
        runtime = StrategyRuntime(OrderCoordinator(self.store, Gate.DISARMED))
        self.assertEqual(runtime.observations()[0]["state"], "DISABLED")
        self.assertFalse(runtime.observations()[0]["enabled"])
        self.assertIsNone(runtime.process_fixture_event({"reference_signal": True, "event_id": "event-1"}))
        self.assertEqual(self.store.observation_orders(), [])

    def test_isolated_fixture_uses_common_intent_risk_and_never_creates_outbox_when_disarmed(self) -> None:
        runtime = StrategyRuntime(OrderCoordinator(self.store, Gate.DISARMED), allow_reference_fixture=True)
        result = runtime.process_fixture_event({"reference_signal": True, "event_id": "event-1"})
        self.assertIsNotNone(result)
        status, verdict = result or (None, None)
        self.assertEqual(status, Status.RISK_REJECTED)
        self.assertFalse(verdict.allowed)
        self.assertIn("trading_gate_not_armed", verdict.reasons)
        observation = self.store.observation_orders()[0]
        self.assertEqual(observation["source"], "STRATEGY")
        self.assertEqual(observation["strategy_id"], "reference-fixture")
        self.assertIsNone(self.store.outbox_state(str(observation["intent_id"])))

    def test_fixture_event_is_deduplicated_deterministically(self) -> None:
        runtime = StrategyRuntime(OrderCoordinator(self.store, Gate.DISARMED), allow_reference_fixture=True)
        first = runtime.process_fixture_event({"reference_signal": True, "event_id": "event-1"})
        duplicate = runtime.process_fixture_event({"reference_signal": True, "event_id": "event-1"})
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(len(self.store.observation_orders()), 1)

    def test_dry_run_exposes_disarmed_verdict_without_persistence_or_outbox(self) -> None:
        runtime = StrategyRuntime(OrderCoordinator(self.store, Gate.DISARMED))
        document = runtime.dry_run_document()
        self.assertEqual(document["mode"], "DRY_RUN")
        self.assertFalse(document["would_persist"])
        self.assertFalse(document["would_create_outbox"])
        self.assertFalse(document["would_dispatch_broker"])
        self.assertEqual(document["risk"], {"allowed": False, "reasons": ["trading_gate_not_armed"]})
        self.assertEqual(self.store.observation_orders(), [])

    def test_fixture_reaches_a_mock_terminal_callback_with_the_same_identity(self) -> None:
        runtime = StrategyRuntime(OrderCoordinator(self.store, Gate.ARMED), allow_reference_fixture=True)
        result = runtime.process_fixture_event({"reference_signal": True, "event_id": "event-terminal"})
        self.assertIsNotNone(result)
        status, verdict = result or (None, None)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        observation = self.store.observation_orders()[0]

        class MockDispatcher:
            def dispatch(self, intent_id: str) -> str:
                if intent_id != observation["intent_id"]:
                    raise AssertionError("worker claimed a different strategy intent")
                return "MOCK-STRATEGY-ORDER-1"

        self.assertTrue(ExecutionWorker(self.store, MockDispatcher()).dispatch_once())
        self.store.record_terminal_callback(
            str(observation["intent_id"]), "MOCK-STRATEGY-ORDER-1", Status.FILLED,
        )
        self.assertEqual(self.store.status(str(observation["intent_id"])), Status.FILLED)
        self.assertEqual(self.store.outbox_state(str(observation["intent_id"])), "TERMINAL")


if __name__ == "__main__":
    unittest.main()
