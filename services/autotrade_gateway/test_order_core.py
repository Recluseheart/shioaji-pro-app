from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from services.autotrade_gateway.order_core import (
    Gate,
    DuplicateIntentError,
    Fill,
    Intent,
    OrderCoordinator,
    RiskContext,
    RiskEngine,
    RiskLimits,
    RiskPosition,
    Source,
    SQLiteIntentStore,
    Status,
)
from services.autotrade_gateway.worker import ExecutionWorker


def armed_coordinator(store: SQLiteIntentStore) -> OrderCoordinator:
    return OrderCoordinator(
        store,
        Gate.ARMED,
        risk_context_provider=lambda _intent: RiskContext(
            quote_price=Decimal("1000"),
            quote_at=datetime.now(timezone.utc),
        ),
    )


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

    def test_kill_switch_is_persistent_and_never_rearms_the_store(self) -> None:
        self.assertEqual(self.store.apply_kill_switch("operator test", "kill-test-1"), (
            Gate.KILLED, "operator test", True,
        ))
        self.assertEqual(self.store.apply_kill_switch("later reason", "kill-test-2"), (
            Gate.KILLED, "operator test", False,
        ))
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "KILL_SWITCH_APPLIED")
        journal = Path(self.temp_dir.name) / "gateway-test.db"
        self.store.close()
        self.store = SQLiteIntentStore(journal)
        self.assertEqual(self.store.trading_gate(), Gate.KILLED)

    def test_strategy_control_receipt_is_idempotent_and_request_bound(self) -> None:
        receipt = {"state": "RUNNING", "enabled": True, "run_id": "run-1"}
        self.assertTrue(self.store.record_strategy_control(
            "strategy-start-1", "reference-fixture", "start", "operator test", receipt,
        ))
        self.assertEqual(self.store.strategy_control_receipt("strategy-start-1"), {
            "strategy_id": "reference-fixture", "action": "start", "reason": "operator test",
            "receipt": receipt,
        })
        self.assertFalse(self.store.record_strategy_control(
            "strategy-start-1", "reference-fixture", "start", "operator test", receipt,
        ))
        with self.assertRaises(ValueError):
            self.store.record_strategy_control(
                "strategy-start-1", "reference-fixture", "stop", "operator test", receipt,
            )

    def test_reconciliation_refresh_reservation_is_request_bound_and_audited(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        self.store.recover()
        self.assertTrue(self.store.begin_reconciliation_refresh("refresh-1", intent.intent_id, "operator check"))
        self.assertEqual(self.store.status(intent.intent_id), Status.RECONCILING)
        self.assertFalse(self.store.begin_reconciliation_refresh("refresh-1", intent.intent_id, "operator check"))
        with self.assertRaisesRegex(ValueError, "different reconciliation refresh"):
            self.store.begin_reconciliation_refresh("refresh-1", intent.intent_id, "different reason")
        self.store.complete_reconciliation_refresh("refresh-1", intent.intent_id, "operator check", {
            "observed_terminal_facts": 0, "resolved_intents": 0, "status": "RECONCILING",
        })
        self.assertEqual(self.store.reconciliation_refresh_receipt("refresh-1"), {
            "intent_id": intent.intent_id, "reason": "operator check", "state": "COMPLETE",
            "receipt": {"observed_terminal_facts": 0, "resolved_intents": 0, "status": "RECONCILING"},
        })
        event_types = {event["event_type"] for event in self.store.observation_audit_events()}
        self.assertTrue({"RECONCILIATION_REFRESH_STARTED", "RECONCILIATION_REFRESH_COMPLETED"} <= event_types)

    def test_armed_intent_requires_authoritative_risk_context(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        status, verdict = OrderCoordinator(self.store, Gate.ARMED).accept(intent)
        self.assertEqual(status, Status.RISK_REJECTED)
        self.assertIn("risk_context_unavailable", verdict.reasons)
        self.assertIsNone(self.store.outbox_state(intent.intent_id))

    def test_risk_context_provider_receives_the_immutable_intent(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        seen: list[str] = []
        coordinator = OrderCoordinator(
            self.store,
            Gate.ARMED,
            risk_context_provider=lambda received: (
                seen.append(received.intent_id) or RiskContext(
                    quote_price=Decimal("1000"), quote_at=datetime.now(timezone.utc),
                )
            ),
        )
        self.assertEqual(coordinator.accept(intent)[0], Status.APPROVED)
        self.assertEqual(seen, [intent.intent_id])

    def test_verified_simulation_allowlist_does_not_depend_on_account_id_prefix(self) -> None:
        intent = Intent(Source.MANUAL_UI, "paper-account-42", "2330", "BUY", 1, "1000")
        verdict = RiskEngine(RiskLimits(allowed_accounts=("paper-account-42",))).evaluate(
            intent,
            Gate.ARMED,
            RiskContext(quote_price=Decimal("1000"), quote_at=datetime.now(timezone.utc)),
        )
        self.assertTrue(verdict.allowed)

    def test_armed_intent_rejects_a_stale_market_snapshot_without_outbox(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        coordinator = OrderCoordinator(
            self.store,
            Gate.ARMED,
            risk_context_provider=lambda _intent: RiskContext(
                quote_price=Decimal("1000"),
                quote_at=datetime.now(timezone.utc) - timedelta(seconds=6),
            ),
        )
        status, verdict = coordinator.accept(intent)
        self.assertEqual(status, Status.RISK_REJECTED)
        self.assertIn("stale_quote", verdict.reasons)
        self.assertIsNone(self.store.outbox_state(intent.intent_id))

    def test_dynamic_risk_context_enforces_position_loss_and_exposure_limits(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 10, "1000")
        verdict = RiskEngine(RiskLimits(
            max_position_per_symbol=10,
            max_gross_exposure=Decimal("1000"),
            max_daily_loss=Decimal("100"),
        )).evaluate(
            intent,
            Gate.ARMED,
            RiskContext(
                quote_price=Decimal("1000"),
                quote_at=datetime.now(timezone.utc),
                positions={"2330": RiskPosition(quantity=10, market_price=Decimal("1000"))},
                realized_pnl=Decimal("-100"),
            ),
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(
            set(verdict.reasons),
            {"position_limit_exceeded", "gross_exposure_limit_exceeded", "daily_loss_limit_exceeded"},
        )

    def test_request_match_requires_every_broker_relevant_field_to_match(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        OrderCoordinator(self.store).accept(intent)
        self.assertTrue(self.store.request_matches(replace(
            intent,
            correlation_id="retry-correlation",
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
            "SELECT allowed, reasons_json FROM risk_decisions WHERE intent_id=?", (intent.intent_id,),
        ).fetchone()
        self.assertEqual(dict(decision), {
            "allowed": 0, "reasons_json": '["trading_gate_not_armed"]',
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
        self.store.connection.commit()
        self.assertEqual(
            [item["intent_id"] for item in self.store.observation_orders()],
            ["intent-z", "intent-a"],
        )

    def test_strategy_identity_and_source_are_required_for_common_ingress(self) -> None:
        incomplete = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000")
        status, verdict = armed_coordinator(self.store).accept(incomplete)
        self.assertEqual(status, Status.RISK_REJECTED)
        self.assertIn("strategy_identity_required", verdict.reasons)

    def test_verified_bound_order_can_create_a_follow_up_cancel_intent(self) -> None:
        original = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        coordinator = armed_coordinator(self.store)
        coordinator.accept(original)
        self.store.mark_pending_submit(original.intent_id, "BOUND-CANCEL-ORDER-1")
        cancel = Intent(
            Source.MANUAL_UI,
            "SIM-001",
            "2330",
            "CANCEL",
            0,
            "0",
            operation="CANCEL",
            target_order_id="BOUND-CANCEL-ORDER-1",
            target_order_verified=True,
        )

        status, verdict = coordinator.accept(cancel)

        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        self.assertEqual(self.store.outbox_state(cancel.intent_id), "PENDING")

    def test_unverified_cancel_target_remains_fail_closed(self) -> None:
        cancel = Intent(
            Source.MANUAL_UI,
            "SIM-001",
            "2330",
            "CANCEL",
            0,
            "0",
            operation="CANCEL",
            target_order_id="external-order",
        )

        status, verdict = armed_coordinator(self.store).accept(cancel)

        self.assertEqual((status, verdict.allowed), (Status.RISK_REJECTED, False))
        self.assertIn("target_order_account_unverified", verdict.reasons)

        strategy = Intent(Source.STRATEGY, "SIM-001", "2330", "BUY", 1, "1000", "reference", "run-1")
        status, verdict = armed_coordinator(self.store).accept(strategy)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        self.assertEqual(self.store.outbox_state(strategy.intent_id), "PENDING")

    def test_market_price_intent_does_not_require_a_positive_limit_price(self) -> None:
        intent = Intent(
            Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "0",
            order_price_type="MKT",
        )
        status, verdict = armed_coordinator(self.store).accept(intent)
        self.assertEqual((status, verdict.allowed), (Status.APPROVED, True))
        self.assertEqual(self.store.outbox_state(intent.intent_id), "PENDING")

    def test_static_simulation_limits_apply_even_when_the_gate_is_armed(self) -> None:
        cases = (
            (Intent(Source.MANUAL_UI, "SIM-999", "2330", "BUY", 1, "1000"), "simulation_account_not_allowed"),
            (Intent(Source.MANUAL_UI, "SIM-001", "9999", "BUY", 1, "1000"), "symbol_not_allowed"),
            (Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 10_001, "1"), "quantity_limit_exceeded"),
            (Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 6_000, "1000"), "notional_limit_exceeded"),
        )
        for intent, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                status, verdict = armed_coordinator(self.store).accept(intent)
                self.assertEqual(status, Status.RISK_REJECTED)
                self.assertIn(expected_reason, verdict.reasons)
                self.assertIsNone(self.store.outbox_state(intent.intent_id))

    def test_restart_marks_pending_delivery_unknown_and_forbids_blind_resend(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        status, _ = armed_coordinator(self.store).accept(intent)
        self.assertEqual(status, Status.APPROVED)
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")
        self.assertEqual(self.store.observation_reconciliation_cases()[0]["reason"], "restart_recovery")
        with self.assertRaisesRegex(ValueError, "require reconciliation"):
            self.store.transition(intent.intent_id, Status.SUBMITTED)
        self.store.transition(intent.intent_id, Status.RECONCILING)
        self.assertEqual(self.store.observation_reconciliation_cases()[0]["state"], "RECONCILING")

    def test_timeout_claim_becomes_unknown_and_is_not_retried(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)

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
        self.assertEqual(self.store.observation_reconciliation_cases()[0]["reason"], "dispatch_uncertain:TimeoutError")
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "DELIVERY_UNKNOWN")
        self.assertEqual(self.store.observation_audit_events()[0]["details"], {
            "reason": "dispatch_uncertain:TimeoutError",
        })

    def test_corrupt_durable_payload_becomes_unknown_without_dispatch(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE order_intents SET payload_json=? WHERE intent_id=?", ("{}", intent.intent_id),
            )

        class NeverDispatcher:
            calls = 0

            def dispatch(self, _intent: Intent) -> str:
                self.calls += 1
                return "must-not-dispatch"

        dispatcher = NeverDispatcher()
        self.assertTrue(ExecutionWorker(self.store, dispatcher).dispatch_once())
        self.assertEqual(dispatcher.calls, 0)
        self.assertEqual(self.store.status(intent.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "UNKNOWN")

    def test_broker_identity_advances_claim_to_pending_submit(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        class AcceptedDispatcher:
            received: Intent | None = None

            def dispatch(self, persisted: Intent) -> str:
                self.received = persisted
                return "MOCK-ORDER-1"

        dispatcher = AcceptedDispatcher()
        self.assertTrue(ExecutionWorker(self.store, dispatcher).dispatch_once())
        self.assertEqual(dispatcher.received, intent)
        self.assertEqual(self.store.status(intent.intent_id), Status.PENDING_SUBMIT)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "DISPATCHED")
        self.assertEqual(self.store.observation_orders()[0]["broker_order_id"], "MOCK-ORDER-1")
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "BROKER_ORDER_BOUND")
        self.assertEqual(self.store.observation_audit_events()[0]["details"], {
            "broker_order_id": "MOCK-ORDER-1",
        })

    def test_bound_broker_order_accepts_a_valid_immutable_fill(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)

        class AcceptedDispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "MOCK-FILL-ORDER-1"

        self.assertTrue(ExecutionWorker(self.store, AcceptedDispatcher()).dispatch_once())
        self.store.record_fill(Fill(
            fill_id="mock-fill-1", intent_id=intent.intent_id,
            broker_order_id="MOCK-FILL-ORDER-1", quantity=1, price="1001",
            filled_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        ))
        self.assertEqual(self.store.observation_fills(), [{
            "fill_id": "mock-fill-1", "intent_id": intent.intent_id,
            "broker_order_id": "MOCK-FILL-ORDER-1", "quantity": 1, "price": "1001",
            "filled_at": "2026-09-15T00:00:00+00:00",
        }])
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "FILL_RECORDED")

    def test_reused_broker_identity_is_unknown_not_bound_to_two_intents(self) -> None:
        first = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        second = Intent(Source.MANUAL_UI, "SIM-001", "2317", "BUY", 1, "1000")
        coordinator = armed_coordinator(self.store)
        coordinator.accept(first)
        coordinator.accept(second)

        class ReusedIdentityDispatcher:
            def dispatch(self, _intent: Intent) -> str:
                return "MOCK-REUSED-ORDER-1"

        worker = ExecutionWorker(self.store, ReusedIdentityDispatcher())
        self.assertTrue(worker.dispatch_once())
        self.assertTrue(worker.dispatch_once())
        self.assertEqual(self.store.status(first.intent_id), Status.PENDING_SUBMIT)
        self.assertEqual(self.store.status(second.intent_id), Status.UNKNOWN)
        self.assertEqual(self.store.outbox_state(second.intent_id), "UNKNOWN")

    def test_terminal_callback_is_identity_bound_idempotent_and_never_reopens_delivery(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)

        class AcceptedDispatcher:
            def dispatch(self, _intent_id: str) -> str: return "MOCK-ORDER-1"

        self.assertTrue(ExecutionWorker(self.store, AcceptedDispatcher()).dispatch_once())
        with self.assertRaisesRegex(ValueError, "identity"):
            self.store.record_terminal_callback(intent.intent_id, "OTHER-ORDER", Status.FILLED)
        self.store.record_terminal_callback(intent.intent_id, "MOCK-ORDER-1", Status.FILLED)
        self.store.record_terminal_callback(intent.intent_id, "MOCK-ORDER-1", Status.FILLED)
        self.assertEqual(self.store.status(intent.intent_id), Status.FILLED)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "TERMINAL")
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "BROKER_TERMINAL_CALLBACK")
        self.assertEqual(self.store.observation_audit_events()[0]["details"], {
            "broker_order_id": "MOCK-ORDER-1", "status": "FILLED",
        })
        with self.assertRaisesRegex(ValueError, "current intent status"):
            self.store.record_terminal_callback(intent.intent_id, "MOCK-ORDER-1", Status.CANCELLED)

    def test_reconciled_terminal_resolves_unknown_with_bound_identity(self) -> None:
        intent = Intent(Source.MANUAL_UI, "SIM-001", "2330", "BUY", 1, "1000")
        armed_coordinator(self.store).accept(intent)
        self.store.mark_pending_submit(intent.intent_id, "RECONCILE-1")
        self.store.mark_delivery_unknown(intent.intent_id, "timeout")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.store.record_reconciled_terminal(intent.intent_id, "OTHER", Status.FILLED)
        self.store.record_reconciled_terminal(intent.intent_id, "RECONCILE-1", Status.FILLED)
        self.assertEqual(self.store.status(intent.intent_id), Status.FILLED)
        self.assertEqual(self.store.outbox_state(intent.intent_id), "TERMINAL")
        self.assertEqual(self.store.observation_audit_events()[0]["event_type"], "BROKER_RECONCILED_TERMINAL")

    def test_existing_sqlite_journal_gains_broker_identity_without_rebuild(self) -> None:
        self.store.close()
        journal = Path(self.temp_dir.name) / "gateway-test.db"
        legacy = sqlite3.connect(journal)
        legacy.execute("DROP TABLE execution_outbox")
        legacy.execute("DROP TABLE order_intents")
        legacy.execute(
            """CREATE TABLE order_intents (
                intent_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
                source TEXT NOT NULL, strategy_id TEXT, run_id TEXT,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                risk_reasons_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        legacy.execute(
            """INSERT INTO order_intents VALUES
            ('historic-intent', 'historic-correlation', 'MANUAL_UI', NULL, NULL,
             '{}', 'UNKNOWN', '[]', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"""
        )
        legacy.commit()
        legacy.close()

        self.store = SQLiteIntentStore(journal)
        observation = self.store.observation_orders()
        self.assertEqual(observation[0]["intent_id"], "historic-intent")
        self.assertIsNone(observation[0]["broker_order_id"])
        self.assertEqual(self.store.observation_risk_decisions(), [{
            "intent_id": "historic-intent", "allowed": True, "risk_reasons": [],
            "decided_at": "2026-01-01T00:00:00+00:00",
        }])


if __name__ == "__main__":
    unittest.main()
