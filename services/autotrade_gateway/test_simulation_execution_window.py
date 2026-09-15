from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from services.autotrade_gateway.order_core import (
    Gate,
    Intent,
    OrderCoordinator,
    RiskContext,
    SQLiteIntentStore,
    Source,
    Status,
)
from services.autotrade_gateway.simulation_execution_window import (
    SimulationExecutionPreflightError,
    SimulationExecutionWindow,
)
from services.autotrade_gateway.worker import ExecutionWorker


def fresh_context(_intent: Intent) -> RiskContext:
    return RiskContext(Decimal("1000"), datetime.now(timezone.utc))


class SimulationExecutionWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteIntentStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def window(self, dispatcher: object) -> SimulationExecutionWindow:
        return SimulationExecutionWindow(
            self.store,
            ExecutionWorker(self.store, dispatcher),  # type: ignore[arg-type]
            authorization="local-test-authorization",
            risk_context_provider=fresh_context,
        )

    def test_dispatches_exactly_one_approved_intent_and_leaves_gate_disarmed(self) -> None:
        class Dispatcher:
            received: list[str] = []

            def dispatch(self, intent: Intent) -> str:
                self.received.append(intent.intent_id)
                return "SIM-WINDOW-ORDER-1"

        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        result = self.window(Dispatcher()).execute(
            intent, authorization="local-test-authorization", reason="controlled mock drill"
        )

        self.assertTrue(result.dispatched)
        self.assertEqual(result.status, Status.PENDING_SUBMIT)
        self.assertEqual(Dispatcher.received, [intent.intent_id])
        self.assertEqual(self.store.trading_gate(), Gate.DISARMED)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "DISPATCHED")
        self.assertEqual(
            [event["event_type"] for event in self.store.observation_audit_events()[:3]],
            ["SIMULATION_WINDOW_FINISHED", "BROKER_ORDER_BOUND", "SIMULATION_WINDOW_ACCEPTED"],
        )

    def test_wrong_authorization_cannot_create_an_intent_or_dispatch(self) -> None:
        class Dispatcher:
            def dispatch(self, _intent: Intent) -> str:
                raise AssertionError("must not dispatch")

        with self.assertRaises(PermissionError):
            self.window(Dispatcher()).execute(
                Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000"),
                authorization="wrong", reason="controlled mock drill",
            )
        self.assertEqual(self.store.observation_orders(), [])
        self.assertEqual(self.store.trading_gate(), Gate.DISARMED)

    def test_open_reconciliation_or_nonterminal_outbox_blocks_the_window(self) -> None:
        existing = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store, Gate.ARMED, risk_context_provider=fresh_context).accept(existing)
        self.assertEqual(self.store.outbox_state(existing.intent_id), "PENDING")

        class Dispatcher:
            def dispatch(self, _intent: Intent) -> str:
                raise AssertionError("must not dispatch")

        with self.assertRaisesRegex(SimulationExecutionPreflightError, "unresolved intent"):
            self.window(Dispatcher()).execute(
                Intent(Source.MANUAL_UI, "SIM-001", "2317", "BUY", 1, "1000"),
                authorization="local-test-authorization", reason="controlled mock drill",
            )
        self.assertTrue(self.store.has_intent(existing.intent_id))
        self.assertEqual(self.store.trading_gate(), Gate.DISARMED)

    def test_timeout_is_unknown_and_cannot_be_dispatched_again(self) -> None:
        class TimeoutDispatcher:
            calls = 0

            def dispatch(self, _intent: Intent) -> str:
                self.calls += 1
                raise TimeoutError("mock timeout")

        dispatcher = TimeoutDispatcher()
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        result = self.window(dispatcher).execute(
            intent, authorization="local-test-authorization", reason="timeout fault drill"
        )

        self.assertTrue(result.dispatched)
        self.assertEqual(result.status, Status.UNKNOWN)
        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        self.assertIsNone(self.store.claim_outbox())
        self.assertEqual(self.store.trading_gate(), Gate.DISARMED)

    def test_killed_gate_blocks_before_persisting_another_intent(self) -> None:
        self.store.apply_kill_switch("test", "kill-window-test")

        class Dispatcher:
            def dispatch(self, _intent: Intent) -> str:
                raise AssertionError("must not dispatch")

        with self.assertRaisesRegex(SimulationExecutionPreflightError, "kill switch"):
            self.window(Dispatcher()).execute(
                Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000"),
                authorization="local-test-authorization", reason="controlled mock drill",
            )
        self.assertEqual(self.store.observation_orders(), [])
        self.assertEqual(self.store.trading_gate(), Gate.KILLED)


if __name__ == "__main__":
    unittest.main()
