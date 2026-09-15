from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.autotrade_gateway.order_core import Gate, OrderCoordinator, SQLiteIntentStore, Status
from services.autotrade_gateway.strategy_runtime import StrategyRuntime


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


if __name__ == "__main__":
    unittest.main()
