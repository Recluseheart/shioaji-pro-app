from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from services.autotrade_gateway.execution_service import (
    ExecutionService,
    PaperTransport,
)
from services.autotrade_gateway.order_core import (
    Gate,
    Intent,
    OrderCoordinator,
    RiskContext,
    RiskEngine,
    RiskLimits,
    RiskPosition,
    SQLiteIntentStore,
    Source,
    Status,
)
from services.autotrade_gateway.server import GatewayHandler, performance_document
from services.autotrade_gateway.simulation_snapshot import (
    SimulationSnapshotProvider,
    SimulationStockAccount,
)
from services.autotrade_gateway.test_simulation_snapshot import Response


def manual(
    quantity=1, price=1000, lot="IntradayOdd", price_type="LMT", account="SIM-001", code="2330"
) -> Intent:
    body = {
        "contract": {"code": code, "security_type": "STK"},
        "stock_order": {
            "account": {"account_id": account},
            "action": "Buy",
            "quantity": quantity,
            "price": price,
            "price_type": price_type,
            "order_lot": lot,
        },
    }
    return _manual_body(body)


def manual_futures(
    quantity=1, price=48700, code="TXFJ6", account="SIM-FUT", price_type="LMT", action="Buy"
) -> Intent:
    return _manual_body({
        "contract": {"code": code, "security_type": "FUT", "exchange": "TAIFEX"},
        "futures_order": {
            "account": {"account_id": account},
            "action": action,
            "quantity": quantity,
            "price": price,
            "price_type": price_type,
            "order_type": "ROD",
            "octype": "Auto",
        },
    })


def _manual_body(body) -> Intent:
    raw = json.dumps(body).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
        _with_idempotency_key=lambda value: value,
    )
    return GatewayHandler._manual_place_order_intent(handler)


class RiskRegressionTest(unittest.TestCase):
    def context(self, price="1000") -> RiskContext:
        return RiskContext(Decimal(price), datetime.now(timezone.utc))

    def test_board_lots_are_shares_for_risk_but_original_wire_quantity_is_retained(
        self,
    ):
        for lot in ("Common", "Fixing"):
            intent = manual(6, lot=lot)
            verdict = RiskEngine().evaluate(intent, Gate.ARMED, self.context())
            self.assertFalse(verdict.allowed)
            self.assertIn("notional_limit_exceeded", verdict.reasons)
            self.assertIn("position_limit_exceeded", verdict.reasons)
            self.assertEqual(
                intent.broker_request["body"]["stock_order"]["quantity"], 6
            )
        for lot in ("Odd", "IntradayOdd"):
            self.assertTrue(
                RiskEngine()
                .evaluate(manual(6, lot=lot), Gate.ARMED, self.context())
                .allowed
            )

    def test_market_single_order_notional_limit_and_boundary(self):
        for price_type in ("MKT", "MKP"):
            engine = RiskEngine(RiskLimits(max_position_per_symbol=10000))
            self.assertIn(
                "notional_limit_exceeded",
                engine.evaluate(
                    manual(2, 0, lot="Common", price_type=price_type),
                    Gate.ARMED,
                    self.context("3000"),
                ).reasons,
            )
            self.assertTrue(
                engine.evaluate(
                    manual(2, 0, lot="Common", price_type=price_type),
                    Gate.ARMED,
                    self.context("2500"),
                ).allowed
            )

    def test_reject_fractional_boolean_and_unknown_units(self):
        for quantity in (True, 1.5, "2"):
            with self.assertRaises(ValueError):
                manual(quantity)
        self.assertFalse(
            RiskEngine()
            .evaluate(manual(lot="unknown"), Gate.ARMED, self.context())
            .allowed
        )

    def test_pending_unknown_reserves_capacity_and_cancel_is_not_rate_limited(self):
        with_store = SQLiteIntentStore(":memory:")
        self.addCleanup(with_store.close)
        core = OrderCoordinator(
            with_store,
            Gate.ARMED,
            RiskEngine(RiskLimits(max_orders_per_minute=1, cooldown_seconds=5)),
            lambda _: self.context(),
        )
        first = manual()
        self.assertEqual(core.accept(first)[0], Status.APPROVED)
        with_store.mark_pending_submit(first.intent_id, "broker-1")
        self.assertFalse(core.accept(manual())[1].allowed)
        cancel = Intent(
            Source.MANUAL_UI,
            "SIM-001",
            "2330",
            "BUY",
            0,
            "0",
            operation="CANCEL",
            target_order_id="broker-1",
            target_order_verified=True,
        )
        self.assertTrue(core.accept(cancel)[1].allowed)
        with_store.mark_delivery_unknown(first.intent_id, "test")
        self.assertIn(
            "outstanding_order_requires_reconciliation",
            core.accept(manual())[1].reasons,
        )

    def test_amendment_cannot_bypass_notional_or_price_deviation(self):
        store = SQLiteIntentStore(":memory:")
        self.addCleanup(store.close)
        core = OrderCoordinator(
            store, Gate.ARMED, risk_context_provider=lambda _: self.context()
        )
        first = manual()
        core.accept(first)
        store.mark_pending_submit(first.intent_id, "broker-1")
        amendment = Intent(
            Source.MANUAL_UI,
            "SIM-001",
            "2330",
            "BUY",
            0,
            "0",
            operation="UPDATE_PRICE",
            target_order_id="broker-1",
            target_order_verified=True,
            operation_payload={"price": 999999999},
        )
        reasons = core.accept(amendment)[1].reasons
        self.assertIn("notional_limit_exceeded", reasons)
        self.assertIn("price_deviation_exceeded", reasons)

    def test_signed_pnl_and_gross_exposure_are_broker_facts(self):
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")

        def opener(request, **_):
            if request.full_url.endswith("profit_loss"):
                return Response([{"id": 1, "date": today, "pnl": -100000}])
            if request.full_url.endswith("position_unit"):
                return Response(
                    [
                        {
                            "code": "2330",
                            "quantity": 1000,
                            "last_price": 1000,
                            "direction": direction,
                            "pnl": -10,
                        }
                        for direction in ("Buy", "Sell")
                    ]
                )
            return Response(
                [
                    {
                        "code": "2330",
                        "close": 1000,
                        "datetime": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )

        context = SimulationSnapshotProvider(
            "http://127.0.0.1:9", opener=opener
        ).stock_risk_context(SimulationStockAccount("B", "A"), "2330")
        self.assertIsNotNone(context)
        self.assertEqual(context.realized_pnl, Decimal("-100000"))
        self.assertEqual(context.gross_exposure, Decimal("2000000"))
        reasons = (
            RiskEngine(RiskLimits(max_gross_exposure=Decimal("1500000")))
            .evaluate(manual(), Gate.ARMED, context)
            .reasons
        )
        self.assertIn("daily_loss_limit_exceeded", reasons)
        self.assertIn("gross_exposure_limit_exceeded", reasons)

    def test_futures_risk_counts_contracts_and_contract_value(self):
        engine = RiskEngine(RiskLimits(allowed_accounts=("SIM-FUT",)))
        flat = self.context("48700")
        self.assertTrue(engine.evaluate(manual_futures(), Gate.ARMED, flat).allowed)
        self.assertTrue(engine.evaluate(manual_futures(code="TXFR1"), Gate.ARMED, flat).allowed)
        self.assertIn(
            "quantity_limit_exceeded",
            engine.evaluate(manual_futures(2), Gate.ARMED, flat).reasons,
        )
        long_two = replace(flat, positions={"TXF": RiskPosition(2, Decimal("48700"))})
        self.assertIn(
            "position_limit_exceeded",
            engine.evaluate(manual_futures(), Gate.ARMED, long_two).reasons,
        )
        self.assertTrue(
            engine.evaluate(manual_futures(action="Sell"), Gate.ARMED, long_two).allowed
        )
        # One TXF contract is ~9.7M of contract value; the stock 5M order
        # notional must not apply, but the futures contract-value cap must.
        tight = RiskEngine(RiskLimits(
            allowed_accounts=("SIM-FUT",), max_futures_notional=Decimal("9000000"),
        ))
        self.assertIn(
            "notional_limit_exceeded",
            tight.evaluate(manual_futures(), Gate.ARMED, flat).reasons,
        )

    def test_unknown_futures_product_or_contract_mismatch_fails_closed(self):
        engine = RiskEngine(RiskLimits(allowed_accounts=("SIM-FUT",)))
        self.assertIn(
            "instrument_risk_unavailable",
            engine.evaluate(manual_futures(code="MXFJ6"), Gate.ARMED, self.context()).reasons,
        )
        mismatched = replace(manual_futures(), symbol="TXFK6")
        self.assertIn(
            "instrument_risk_unavailable",
            engine.evaluate(mismatched, Gate.ARMED, self.context()).reasons,
        )
        disallowed = RiskEngine(RiskLimits(allowed_accounts=("SIM-FUT",), allowed_futures_roots=()))
        self.assertIn(
            "symbol_not_allowed",
            disallowed.evaluate(manual_futures(), Gate.ARMED, self.context("48700")).reasons,
        )

    def test_convertible_bond_board_lot_and_slow_quote_age(self):
        cb = manual(1, 101.5, lot="Common", code="11011")
        aged = RiskContext(Decimal("101.5"), datetime.now(timezone.utc) - timedelta(seconds=30))
        self.assertIn("stale_quote", RiskEngine().evaluate(cb, Gate.ARMED, aged).reasons)
        slow = RiskEngine(RiskLimits(slow_quote_symbols=("11011",), slow_quote_max_age_seconds=60))
        self.assertTrue(slow.evaluate(cb, Gate.ARMED, aged).allowed)
        # One CB board lot is 1,000 units: 2 lots exceed a 2,000-unit symbol cap
        # once one lot is already held.
        holding = replace(aged, positions={"11011": RiskPosition(1000, Decimal("101.5"))})
        self.assertIn(
            "position_limit_exceeded",
            slow.evaluate(manual(2, 101.5, lot="Common", code="11011"), Gate.ARMED, holding).reasons,
        )

    def test_odd_lots_are_1_to_999_shares_and_not_for_cbs(self):
        engine = RiskEngine(RiskLimits(board_lot_only_symbols=("11011",)))
        self.assertTrue(engine.evaluate(manual(999), Gate.ARMED, self.context()).allowed)
        self.assertIn(
            "odd_lot_quantity_exceeded",
            engine.evaluate(manual(1000), Gate.ARMED, self.context()).reasons,
        )
        self.assertIn(
            "odd_lot_not_supported",
            engine.evaluate(manual(5, 101.5, code="11011"), Gate.ARMED, self.context("101.5")).reasons,
        )

    def test_nonfinite_and_unavailable_pnl_fail_closed(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            self.assertIsNone(SimulationSnapshotProvider._decimal(value))
        self.assertIn(
            "pnl_unavailable",
            RiskEngine()
            .evaluate(manual(), Gate.ARMED, replace(self.context(), realized_pnl=None))
            .reasons,
        )


class ExecutionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteIntentStore(":memory:")
        self.broker = PaperTransport(self.store)
        self.events = []
        self.service = ExecutionService(
            self.store, self.broker, self.events.append, allow_execution=True
        )
        self.service.coordinator.risk = RiskEngine(
            RiskLimits(allowed_accounts=(self.broker.account_id,), cooldown_seconds=0)
        )

    def tearDown(self):
        self.service.stop()
        self.store.close()

    def test_manual_http_mapping_to_fill_and_ui_projection_without_broker(self):
        self.service.control("arm", "paper test", "arm-1")
        order = manual(account=self.broker.account_id)
        self.assertEqual(self.service.coordinator.accept(order)[0], Status.APPROVED)
        self.service.tick()
        self.assertEqual(self.store.status(order.intent_id), Status.PENDING_SUBMIT)
        self.service.tick()
        self.assertEqual(self.store.status(order.intent_id), Status.FILLED)
        self.assertEqual(len(self.store.observation_fills()), 1)
        self.service.tick()
        self.assertEqual(len(self.store.observation_fills()), 1)
        self.assertEqual(self.events[-1]["status"], "FILLED")
        self.assertEqual(performance_document(self.store)["source"], "PAPER_ESTIMATE")

    def test_paper_position_projection_selects_latest_complete_snapshot(self):
        from services.autotrade_gateway.server import position_snapshots_document

        self.service.control("arm", "paper test", "arm-positions")
        order = manual(account=self.broker.account_id)
        self.service.coordinator.accept(order)
        self.service.tick()
        self.service.tick()
        for _ in range(2):
            self.service._last_account_snapshot = 0
            self.service.tick()
        self.assertGreater(len(self.store.observation_position_snapshots()), 1)
        self.assertEqual(len(position_snapshots_document(self.store)), 1)
        self.store.record_audit_event(
            None,
            "PAPER_POSITION_VIEW",
            {"snapshot_ids": [], "observed_at": datetime.now(timezone.utc).isoformat()},
        )
        self.assertEqual(position_snapshots_document(self.store), [])

    def test_market_strategy_configuration_preview_lifecycle_and_fill(self):
        strategy = self.service.strategy
        strategy.configure(
            {
                "symbol": "2330",
                "side": "BUY",
                "quantity": 2,
                "threshold": "1000",
                "condition": "AT_OR_BELOW",
                "max_signals": 1,
            }
        )
        strategy.dry_run_document()
        self.assertEqual(self.store.observation_orders(), [])
        self.service.control("arm", "paper test", "arm-1")
        strategy.control("start")
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.store.observation_orders()[0]["status"], "FILLED")
        self.assertEqual(len(self.store.observation_signals()), 1)
        strategy.control("pause")
        self.service.tick()
        self.assertEqual(len(self.store.observation_signals()), 1)
        strategy.control("resume")
        self.service.tick()
        self.assertEqual(len(self.store.observation_orders()), 1)
        strategy.control("stop")
        self.assertEqual(self.store.observation_strategy_runs()[0]["state"], "STOPPED")

    def test_cancel_and_amend_use_one_target_identity_without_duplicate_binding(self):
        self.service.control("arm", "paper test", "arm-1")
        order = manual(price=950, account=self.broker.account_id)
        self.service.coordinator.accept(order)
        self.service.tick()
        target = self.store.observation_orders()[0]["broker_order_id"]
        command = Intent(
            Source.MANUAL_UI,
            self.broker.account_id,
            "2330",
            "BUY",
            0,
            "0",
            operation="CANCEL",
            target_order_id=target,
            target_order_verified=True,
        )
        self.service.coordinator.accept(command)
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.store.status(command.intent_id), Status.COMPLETED)
        self.assertEqual(self.store.status(order.intent_id), Status.CANCELLED)

    def test_real_http_target_mapping_cancel_completes(self):
        self.service.control("arm", "paper test", "arm-http")
        order = manual(price=950, account=self.broker.account_id)
        self.service.coordinator.accept(order)
        self.service.tick()
        target = self.store.observation_orders()[0]["broker_order_id"]
        body = json.dumps({"trade_id": target}).encode()
        handler = SimpleNamespace(
            headers={"Content-Length": str(len(body))},
            rfile=BytesIO(body),
            server=SimpleNamespace(order_store=self.store),
            _with_idempotency_key=lambda value: value,
            _matching_target_for_operation=GatewayHandler._matching_target_for_operation,
        )
        command = GatewayHandler._manual_target_order_intent(
            handler, "CANCEL", (), broker_path="/api/v1/order/cancel_order"
        )
        self.assertEqual(command.side, "CANCEL")
        self.assertEqual(self.service.coordinator.accept(command)[0], Status.APPROVED)
        self.service.tick()
        self.assertIsNone(self.service.trade_document(command.intent_id)["trade"])
        self.service.tick()
        self.assertEqual(self.store.status(command.intent_id), Status.COMPLETED)
        self.assertIsNotNone(self.service.trade_document(command.intent_id)["trade"])

    def test_http_price_and_quantity_amendments_use_modified_status_fields(self):
        self.service.coordinator.risk = RiskEngine(
            RiskLimits(
                allowed_accounts=(self.broker.account_id,),
                cooldown_seconds=0,
                max_position_per_symbol=10000,
            )
        )
        self.service.control("arm", "paper test", "arm-amend")
        order = manual(
            quantity=2, price=950, lot="Common", account=self.broker.account_id
        )
        self.assertEqual(self.service.coordinator.accept(order)[0], Status.APPROVED)
        self.service.tick()
        target = self.store.observation_orders()[0]["broker_order_id"]
        for operation, field, value in (
            ("UPDATE_PRICE", "price", 960),
            ("UPDATE_QUANTITY", "quantity", 1),
        ):
            raw = json.dumps({"trade_id": target, field: value}).encode()
            handler = SimpleNamespace(
                headers={"Content-Length": str(len(raw))},
                rfile=BytesIO(raw),
                server=SimpleNamespace(order_store=self.store),
                _with_idempotency_key=lambda intent: intent,
                _matching_target_for_operation=GatewayHandler._matching_target_for_operation,
            )
            command = GatewayHandler._manual_target_order_intent(
                handler, operation, (field,)
            )
            self.assertEqual(
                self.service.coordinator.accept(command)[0], Status.APPROVED
            )
            self.service.tick()
            self.service.tick()
            self.assertEqual(self.store.status(command.intent_id), Status.COMPLETED)
        trade = self.service.trade_document(order.intent_id)["trade"]
        self.assertEqual(trade["order"]["price"], "950")
        self.assertEqual(trade["status"]["modified_price"], 960)
        self.assertEqual(trade["order"]["quantity"], 2)
        self.assertEqual(trade["status"]["order_quantity"], 1)
        self.assertEqual(trade["status"]["cancel_quantity"], 1)

    def test_timeout_never_retries_and_kill_blocks_worker(self):
        self.service.control("arm", "paper test", "arm-1")
        order = manual(account=self.broker.account_id)
        self.service.coordinator.accept(order)
        calls = []

        def uncertain(intent):
            calls.append(intent.intent_id)
            raise TimeoutError()

        self.broker.dispatch = uncertain
        self.service.tick()
        self.service.tick()
        self.assertEqual(calls, [order.intent_id])
        self.assertEqual(self.store.status(order.intent_id), Status.UNKNOWN)
        self.store.apply_kill_switch("stop", "kill-1")
        self.service.tick()
        self.assertEqual(self.service.coordinator.gate, Gate.KILLED)

    def test_restart_does_not_rearm_or_auto_resume_and_unknown_pnl_is_not_zero(self):
        self.assertIsNone(performance_document(self.store)["realized_pnl"])
        self.service.control("arm", "paper test", "arm-1")
        self.service.strategy.control("start")
        restarted = ExecutionService(
            self.store, self.broker, self.events.append, allow_execution=True
        )
        self.assertEqual(restarted.coordinator.gate, Gate.DISARMED)
        self.assertEqual(restarted.strategy.observations()[0]["state"], "PAUSED")
        receipt = restarted.control("arm", "paper test", "arm-1")
        self.assertFalse(receipt["applied"])
        self.assertEqual(restarted.coordinator.gate, Gate.DISARMED)


if __name__ == "__main__":
    unittest.main()


class RuntimeBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteIntentStore(":memory:")
        self.broker = PaperTransport(self.store)
        self.service = ExecutionService(
            self.store, self.broker, lambda _: None, allow_execution=True
        )
        self.service.control("arm", "test", "arm")
        self.order = manual(quantity=10, price=950, account=self.broker.account_id)
        self.service.coordinator.accept(self.order)
        self.service.tick()
        self.identity = self.store.observation_orders()[0]["broker_order_id"]
        self.trade = self.broker.rows[self.identity]
        self.addCleanup(self.store.close)

    def test_bad_account_symbol_or_incomplete_executions_cannot_finalize(self):
        from copy import deepcopy

        for change in ("account", "symbol", "quantity", "duplicate", "nan"):
            with self.subTest(change=change):
                trade = deepcopy(self.trade)
                trade["status"].update(
                    status="Filled",
                    deals=[
                        {
                            "seq": "1",
                            "quantity": 10,
                            "price": 950,
                            "ts": datetime.now(timezone.utc).timestamp(),
                        }
                    ],
                )
                if change == "account":
                    trade["order"]["account"]["account_id"] = "OTHER"
                if change == "symbol":
                    trade["contract"]["code"] = "OTHER"
                if change == "quantity":
                    trade["status"]["deals"][0]["quantity"] = 1
                if change == "duplicate":
                    trade["status"]["deals"] *= 2
                if change == "nan":
                    trade["status"]["deals"][0]["price"] = "NaN"
                with self.assertRaises(ValueError):
                    self.service._apply_trades([trade])
                self.assertEqual(
                    self.store.status(self.order.intent_id), Status.PENDING_SUBMIT
                )
                self.assertEqual(self.store.observation_fills(), [])

    def test_partial_is_monotonic_and_duplicate_fills_are_idempotent(self):
        self.trade["status"].update(
            status="PartFilled",
            deals=[
                {
                    "seq": "1",
                    "quantity": 1,
                    "price": 950,
                    "ts": datetime.now(timezone.utc).timestamp(),
                }
            ],
        )
        self.service._apply_trades([self.trade])
        self.service._apply_trades([self.trade])
        self.assertEqual(len(self.store.observation_fills()), 1)
        self.trade["status"].update(status="Submitted", deals=[])
        self.service._apply_trades([self.trade])
        self.assertEqual(self.store.status(self.order.intent_id), Status.PART_FILLED)

    def test_cancel_readback_waits_for_completion_and_unavailable_read_disarms(self):
        command = Intent(
            Source.MANUAL_UI,
            self.broker.account_id,
            "2330",
            "BUY",
            0,
            "0",
            operation="CANCEL",
            target_order_id=self.identity,
            target_order_verified=True,
        )
        self.service.coordinator.accept(command)
        self.store.mark_operation_pending(command.intent_id, self.identity)
        self.service._apply_trades([self.trade])
        self.assertIsNone(self.service.trade_document(command.intent_id)["trade"])

        def unavailable():
            raise TimeoutError()

        self.broker.trades = unavailable
        with self.assertRaises(TimeoutError):
            self.service.tick()
        self.assertEqual(self.service.coordinator.gate, Gate.DISARMED)
        self.assertEqual(self.store.status(self.order.intent_id), Status.UNKNOWN)

    def test_slow_context_cannot_dispatch_after_arm_expiry(self):
        calls = []
        original = self.broker.context

        def context(intent):
            result = original(intent)
            self.service.arm_expires_at = 0
            return result

        self.broker.context = context
        self.broker.dispatch = lambda intent: calls.append(intent)
        with self.assertRaises(ValueError):
            self.service.dispatch(self.order)
        self.assertEqual(calls, [])

    def test_configurations_have_one_current_strategy(self):
        self.assertEqual(len(self.service.strategy.observations()), 1)
        config = self.service.strategy.observations()[0]["config"]
        self.service.strategy.configure({**config, "threshold": "999"})
        self.assertEqual(len(self.service.strategy.observations()), 1)


class InstrumentExecutionTest(unittest.TestCase):
    """Board lots, convertible bonds and TXF futures through the one owner."""

    def setUp(self):
        self.store = SQLiteIntentStore(":memory:")
        self.broker = PaperTransport(self.store)
        self.service = ExecutionService(
            self.store, self.broker, lambda _: None, allow_execution=True,
            limits=RiskLimits(cooldown_seconds=0),
        )
        self.service.control("arm", "paper instruments", "arm-instruments")

    def tearDown(self):
        self.service.stop()
        self.store.close()

    def test_paper_futures_fill_counts_contracts_and_values_with_multiplier(self):
        order = manual_futures(code="TXFR1", account=self.broker.futures_account_id)
        status, verdict = self.service.coordinator.accept(order)
        self.assertEqual(status, Status.APPROVED, verdict.reasons)
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.store.status(order.intent_id), Status.FILLED)
        self.assertEqual(int(str(self.store.observation_fills()[0]["quantity"])), 1)
        self.broker.set_price("TXFR1", Decimal("48800"))
        futures = [item for item in self.broker.position_snapshots if item.account_type == "F"]
        self.assertEqual([(item.symbol, item.quantity) for item in futures], [("TXF", "1")])
        self.assertEqual(Decimal(futures[0].unrealized_pnl), Decimal("20000"))

    def test_instruments_must_use_their_own_account(self):
        stock_on_futures = manual(account=self.broker.futures_account_id)
        futures_on_stock = manual_futures(code="TXFR1", account=self.broker.account_id)
        for order in (stock_on_futures, futures_on_stock):
            status, verdict = self.service.coordinator.accept(order)
            self.assertEqual(status, Status.RISK_REJECTED)
            self.assertIn("risk_context_unavailable", verdict.reasons)

    def test_convertible_bond_board_lot_fills_in_units(self):
        order = manual(1, 100, lot="Common", code="11011", account=self.broker.account_id)
        status, verdict = self.service.coordinator.accept(order)
        self.assertEqual(status, Status.APPROVED, verdict.reasons)
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.store.status(order.intent_id), Status.FILLED)
        self.assertEqual(int(str(self.store.observation_fills()[0]["quantity"])), 1000)
        stock = [item for item in self.broker.position_snapshots if item.account_type == "S"]
        self.assertEqual([(item.symbol, item.quantity) for item in stock], [("11011", "1000")])

    def test_dispatch_preflight_refusal_is_rejected_and_never_sent(self):
        order = manual(account=self.broker.account_id)
        self.assertEqual(self.service.coordinator.accept(order)[0], Status.APPROVED)
        sent = []
        original_context, original_dispatch = self.broker.context, self.broker.dispatch
        self.broker.context = lambda intent: None
        self.broker.dispatch = lambda intent: sent.append(intent) or "never"
        self.assertTrue(self.service.worker.dispatch_once())
        self.assertEqual(sent, [])
        self.assertEqual(self.store.status(order.intent_id), Status.RISK_REJECTED)
        self.assertEqual(self.store.outbox_state(order.intent_id), "REFUSED")
        row = next(item for item in self.store.observation_orders() if item["intent_id"] == order.intent_id)
        self.assertIn("risk_context_unavailable", row["risk_reasons"])
        # A refusal is not an uncertain delivery, so re-arming is allowed.
        self.broker.context, self.broker.dispatch = original_context, original_dispatch
        self.service.control("disarm", "paper instruments", "disarm-1")
        self.assertTrue(self.service.control("arm", "paper instruments", "arm-2")["applied"])

    def test_sidecar_routes_futures_to_the_verified_futures_account(self):
        from unittest import mock

        from services.autotrade_gateway.execution_service import SidecarTransport
        from services.autotrade_gateway.simulation_snapshot import SimulationSnapshotProvider

        seen: list[tuple[str, object]] = []
        now = datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None).isoformat()

        def fake(self, path, body=None):
            del self
            seen.append((path, body))
            if path == "/api/v1/info":
                return {"simulation": True}
            if path == "/api/v1/auth/accounts":
                return [
                    {"account_type": "S", "broker_id": "B", "account_id": "stock-1"},
                    {"account_type": "F", "broker_id": "F", "account_id": "future-1"},
                ]
            if path.startswith("/api/v1/data/contracts/"):
                return {"security_type": "FUT", "exchange": "TAIFEX", "code": "TXFR1", "target_code": "TXFJ6"}
            if path == "/api/v1/data/snapshots":
                return [{"code": "TXFJ6", "close": 48700, "datetime": now}]
            if path in {"/api/v1/portfolio/position_unit", "/api/v1/portfolio/profit_loss", "/api/v1/order/trades"}:
                return []
            raise AssertionError(path)

        with mock.patch.object(SimulationSnapshotProvider, "_request", fake):
            transport = SidecarTransport("http://127.0.0.1:21322")
            self.assertEqual(transport.account_ids, ("stock-1", "future-1"))
            self.assertEqual(transport.broker_id_for("future-1"), "F")
            context = transport.context(manual_futures(code="TXFR1", account="future-1"))
            self.assertIsNotNone(context)
            self.assertIsNone(transport.context(manual_futures(code="TXFR1", account="stock-1")))
            transport.trades()
        position_reads = [body for path, body in seen if path == "/api/v1/portfolio/position_unit"]
        self.assertEqual(position_reads[0]["account_type"], "F")
        self.assertEqual(position_reads[0]["unit"], "Common")
        trade_reads = [body["account_type"] for path, body in seen if path == "/api/v1/order/trades"]
        self.assertEqual(trade_reads, ["S", "F"])

    def test_environment_limits_enable_listed_cbs_and_refuse_bad_values(self):
        from services.autotrade_gateway.execution_service import risk_limits_from_environment

        limits = risk_limits_from_environment({
            "AUTOTRADE_CB_SYMBOLS": "11011, 23301",
            "AUTOTRADE_MAX_FUTURES_POSITION": "3",
        })
        self.assertIn("23301", limits.allowed_symbols)
        self.assertEqual(limits.slow_quote_symbols, ("11011", "23301"))
        self.assertEqual(limits.max_futures_position, 3)
        self.assertEqual(limits.cooldown_seconds, 2)
        for bad in (
            {"AUTOTRADE_CB_SYMBOLS": "11011;DROP"},
            {"AUTOTRADE_MAX_FUTURES_ORDER_CONTRACTS": "0"},
            {"AUTOTRADE_SLOW_QUOTE_MAX_AGE_SECONDS": "abc"},
        ):
            with self.assertRaises(ValueError):
                risk_limits_from_environment(bad)

    def test_stock_only_sidecar_keeps_futures_unavailable(self):
        from unittest import mock

        from services.autotrade_gateway.execution_service import SidecarTransport
        from services.autotrade_gateway.simulation_snapshot import SimulationSnapshotProvider

        def fake(self, path, body=None):
            del self, body
            if path == "/api/v1/info":
                return {"simulation": True}
            return [{"account_type": "S", "broker_id": "B", "account_id": "stock-1"}]

        with mock.patch.object(SimulationSnapshotProvider, "_request", fake):
            transport = SidecarTransport("http://127.0.0.1:21322")
            self.assertEqual(transport.account_ids, ("stock-1",))
            self.assertIsNone(transport.futures_account_id)
            with self.assertRaises(ValueError):
                SidecarTransport("http://127.0.0.1:21322", futures_account_id="future-1")


class GatewayRuntimeIsolationTest(unittest.TestCase):
    def test_owner_lock_precedes_recovery_and_is_released(self):
        import os
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from services.autotrade_gateway.server import create_server

        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"AUTOTRADE_GATEWAY_DB": f"{directory}/paper.db"}),
        ):
            first = create_server(port=0, runtime_mode="PAPER")
            try:
                with self.assertRaisesRegex(RuntimeError, "operator process"):
                    create_server(port=0, runtime_mode="PAPER")
            finally:
                first.server_close()
                first.order_store.close()
            second = create_server(port=0, runtime_mode="PAPER")
            second.server_close()
            second.order_store.close()

    def test_paper_routes_and_boot_never_contact_sidecar(self):
        import os
        from threading import Thread
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        from unittest.mock import patch
        from services.autotrade_gateway.server import create_server

        with (
            patch.dict(os.environ, {"AUTOTRADE_ENABLE_SIMULATION_RISK_CONTEXT": "1"}),
            patch(
                "services.autotrade_gateway.server.urlopen",
                side_effect=AssertionError("sidecar call"),
            ),
            patch(
                "services.autotrade_gateway.simulation_snapshot.urlopen",
                side_effect=AssertionError("sidecar call"),
            ),
            patch(
                "services.autotrade_gateway.simulation_connection.urlopen",
                side_effect=AssertionError("sidecar call"),
            ),
        ):
            server = create_server(port=0, runtime_mode="PAPER")
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                server.execution_service.tick()
                base = f"http://127.0.0.1:{server.server_port}"
                for path in (
                    "/autotrade/connection",
                    "/auth/accounts",
                    "/info",
                    "/order/trades",
                ):
                    with urlopen(base + "/api/v1" + path) as response:
                        self.assertEqual(response.status, 200)
                for path, body in (
                    ("/data/snapshots", b"{}"),
                    ("/autotrade/positions/refresh", b"{}"),
                    ("/stream/data", None),
                ):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(Request(base + "/api/v1" + path, data=body))
                    self.assertIn(raised.exception.code, {400, 409})
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
                server.order_store.close()
