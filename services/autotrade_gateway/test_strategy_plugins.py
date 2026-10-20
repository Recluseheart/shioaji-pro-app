from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from services.autotrade_gateway.execution_service import ExecutionService, PaperTransport
from services.autotrade_gateway.order_core import (
    OrderCoordinator,
    RiskLimits,
    SQLiteIntentStore,
    Status,
)
from services.autotrade_gateway.simulation_snapshot import ContractRef
from services.autotrade_gateway.strategy_plugins import (
    REPOSITORY_ROOT,
    PluginStrategyRuntime,
    load_private_strategy,
    stock_order_parts,
)

# A scripted fixture, not a trading strategy: it proposes exactly what its
# configuration says so the Gateway mapping can be tested without private code.
SCRIPTED_PLUGIN = '''
from services.autotrade_gateway.strategy_plugins import OrderProposal


class Scripted:
    strategy_id = "scripted-test"
    display_name = "Scripted test plugin"
    version = "t1"
    config_fields = ({"name": "symbol", "label": "symbol", "type": "text"},)

    def default_config(self):
        return {"symbol": "99991", "side": "BUY", "quantity": 1, "lot": "Common",
                "max_signals": 1, "fail": False}

    def parse_config(self, raw):
        if not isinstance(raw, dict) or set(raw) != set(self.default_config()):
            raise ValueError("bad config")
        return dict(raw)

    def symbols(self, config):
        return (config["symbol"],)

    def max_signals(self, config):
        return config["max_signals"]

    def decide(self, config, markets, positions, activity):
        if config["fail"]:
            raise RuntimeError("scripted failure")
        market = markets.get(config["symbol"])
        if market is None:
            return []
        return [OrderProposal(config["symbol"], config["side"], config["quantity"], market.last_price,
                              lot=config["lot"])]


STRATEGY = Scripted()
'''


def write_plugin(directory: Path) -> None:
    (directory / "scripted_test.py").write_text(SCRIPTED_PLUGIN)


class PrivateStrategyLoaderTest(unittest.TestCase):
    def test_loads_only_from_outside_git_work_trees(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_plugin(directory)
            self.assertEqual(load_private_strategy(directory, "scripted-test").strategy_id, "scripted-test")
            with self.assertRaises(ValueError):
                load_private_strategy(REPOSITORY_ROOT / "services", "scripted-test")
            (directory / ".git").mkdir()
            with self.assertRaises(ValueError):
                load_private_strategy(directory, "scripted-test")

    def test_missing_or_mismatched_module_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(ValueError):
                load_private_strategy(directory, "scripted-test")
            (directory / "scripted_test.py").write_text("STRATEGY = object()\n")
            with self.assertRaises(ValueError):
                load_private_strategy(directory, "scripted-test")
            with self.assertRaises(ValueError):
                load_private_strategy(directory, "../escape")


class PluginExecutionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        write_plugin(Path(self.directory.name))
        self.plugin = load_private_strategy(self.directory.name, "scripted-test")
        self.store = SQLiteIntentStore(":memory:")
        self.broker = PaperTransport(self.store)
        self.service = ExecutionService(
            self.store, self.broker, lambda _: None, allow_execution=True,
            limits=RiskLimits(
                cooldown_seconds=0, slow_quote_symbols=("99991",), board_lot_only_symbols=("99991",),
            ),
            strategy_plugin=self.plugin,
        )
        self.strategy = self.service.strategy

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.directory.cleanup()

    def configure(self, **changes):
        self.strategy.configure({**self.plugin.default_config(), **changes})

    def test_dry_run_never_persists_and_reports_proposals(self):
        self.configure()
        document = self.strategy.dry_run_document()
        self.assertFalse(document["would_persist"])
        self.assertEqual(document["proposals"][0]["proposal"]["side"], "BUY")
        self.assertEqual(self.store.observation_orders(), [])
        self.assertEqual(self.strategy.observations()[0]["config_schema"][0]["name"], "symbol")

    def test_sim_cb_prices_go_on_the_half_point_step_in_the_safe_direction(self):
        self.assertIsNone(self.strategy.cb_price_step)  # PAPER keeps the real price
        self.strategy.cb_price_step = Decimal("0.5")    # what the SIM service sets
        self.broker.set_price("99991", Decimal("102.05"))
        self.configure()
        proposal = self.strategy.dry_run_document()["proposals"][0]["proposal"]
        self.assertEqual((proposal["price"], proposal["original_price"]), ("102.0", "102.05"))
        self.configure(side="SELL")
        self.assertEqual(self.strategy.dry_run_document()["proposals"][0]["proposal"]["price"], "102.5")
        self.broker.set_price("99991", Decimal("103.5"))
        self.assertEqual(self.strategy.dry_run_document()["proposals"][0]["proposal"]["price"], "103.5")
        self.configure(symbol="2330", quantity=1)
        self.broker.set_price("2330", Decimal("1002.5"))
        self.assertEqual(self.strategy.dry_run_document()["proposals"][0]["proposal"]["price"], "1002.5")

    def test_sim_cb_order_is_sent_at_the_stepped_price(self):
        self.strategy.cb_price_step = Decimal("0.5")
        self.broker.set_price("99991", Decimal("102.05"))
        self.configure()
        self.service.control("arm", "plugin test", "arm-step")
        self.strategy.control("start")
        self.service.tick()
        order = self.store.observation_orders()[0]
        self.assertEqual(order["limit_price"], "102.0")
        intent = self.store.load_intent(str(order["intent_id"]))
        self.assertEqual(intent.broker_request["body"]["stock_order"]["price"], 102.0)

    def test_convertible_bond_signal_reaches_a_board_lot_fill_once(self):
        self.configure()
        self.service.control("arm", "plugin test", "arm-1")
        self.strategy.control("start")
        for _ in range(4):
            self.service.tick()
        orders = self.store.observation_orders()
        self.assertEqual(len(orders), 1)
        intent = self.store.load_intent(str(orders[0]["intent_id"]))
        body = intent.broker_request["body"]
        self.assertEqual(body["contract"]["exchange"], "OTC")
        self.assertEqual(body["stock_order"]["order_lot"], "Common")
        self.assertEqual(body["stock_order"]["account"]["account_type"], "S")
        self.assertEqual(self.store.status(intent.intent_id), Status.FILLED)
        self.assertEqual(int(str(self.store.observation_fills()[0]["quantity"])), 1000)
        self.assertEqual(len(self.store.observation_signals()), 1)

    def test_futures_proposal_uses_the_futures_account(self):
        self.configure(symbol="TXFR1")
        self.service.control("arm", "plugin test", "arm-1")
        self.strategy.control("start")
        self.service.tick()
        self.service.tick()
        self.service.tick()
        intent = self.store.load_intent(str(self.store.observation_orders()[0]["intent_id"]))
        order = intent.broker_request["body"]["futures_order"]
        self.assertEqual((intent.account_id, order["octype"], order["account"]["account_type"]),
                         (self.broker.futures_account_id, "Auto", "F"))
        self.assertEqual(self.store.status(intent.intent_id), Status.FILLED)

    def test_plugin_failure_pauses_strategy_without_stopping_the_gateway(self):
        self.configure(fail=True)
        self.service.control("arm", "plugin test", "arm-1")
        self.strategy.control("start")
        self.service.tick()
        observation = self.strategy.observations()[0]
        self.assertEqual(observation["state"], "PAUSED")
        self.assertIn("RuntimeError", str(observation["last_error"]))
        self.assertEqual(self.store.observation_orders(), [])

    def test_unconfigured_or_disallowed_symbols_are_refused_at_configure(self):
        with self.assertRaises(ValueError):
            self.configure(symbol="9999")  # no contract
        tight = RiskLimits(allowed_symbols=("2330",))
        self.service.coordinator.risk.limits = tight
        with self.assertRaises(ValueError):
            self.configure(symbol="99991")

    def run_ticks(self, count):
        for _ in range(count):
            self.service.tick()

    def strategy_orders(self):
        rows = [row for row in self.store.observation_orders() if row.get("strategy_id") == "scripted-test"]
        orders = [(self.store.load_intent(str(row["intent_id"])), row["status"]) for row in rows]
        return sorted(orders, key=lambda item: item[0].created_at)

    def lot_of(self, intent):
        return intent.broker_request["body"]["stock_order"]["order_lot"]

    def test_auto_lot_rule_board_lots_from_1000_shares(self):
        for shares, expected in ((1, [("IntradayOdd", 1)]), (999, [("IntradayOdd", 999)]),
                                 (1000, [("Common", 1)]), (1500, [("Common", 1), ("IntradayOdd", 500)]),
                                 (2000, [("Common", 2)])):
            self.assertEqual([(p.lot, p.quantity) for p in stock_order_parts(shares)], expected)
        with self.assertRaises(ValueError):
            stock_order_parts(0)
        self.configure(symbol="2330", lot="AUTO", quantity=999)
        orders = self.strategy.dry_run_document()["proposals"][0]["orders"]
        self.assertEqual(orders, [{"lot": "IntradayOdd", "quantity": 999}])

    def test_auto_split_sends_odd_remainder_only_after_board_lot_fills(self):
        self.configure(symbol="2330", lot="AUTO", quantity=1500)
        self.service.control("arm", "plugin test", "arm-1")
        self.strategy.control("start")
        self.service.tick()
        orders = self.strategy_orders()
        self.assertEqual([(self.lot_of(i), i.quantity) for i, _ in orders], [("Common", 1)])
        self.run_ticks(3)
        orders = self.strategy_orders()
        self.assertEqual(
            [(self.lot_of(i), i.quantity, status) for i, status in orders],
            [("Common", 1, "FILLED"), ("IntradayOdd", 500, "FILLED")],
        )
        self.assertEqual(sorted(int(str(f["quantity"])) for f in self.store.observation_fills()), [500, 1000])
        self.assertEqual(self.strategy._activity()["2330"].buy_units, 1500)

    def test_odd_remainder_is_dropped_when_board_lot_does_not_fill(self):
        self.configure(symbol="2330", lot="AUTO", quantity=1500)
        self.service.control("arm", "plugin test", "arm-1")
        self.strategy.control("start")
        self.service.tick()
        row = next(iter(self.broker.rows.values()))
        row["status"].update(status="Cancelled", cancel_quantity=1)  # Shioaji keeps order_quantity
        self.run_ticks(3)
        orders = self.strategy_orders()
        self.assertEqual([(self.lot_of(i), status) for i, status in orders], [("Common", "CANCELLED")])
        dropped = [e for e in self.store.observation_audit_events()
                   if e["event_type"] == "STRATEGY_ODD_REMAINDER_DROPPED"]
        self.assertEqual([(e["details"]["board_lot_status"], e["details"]["odd_lot_shares"]) for e in dropped],
                         [("CANCELLED", 500)])
        self.assertEqual(self.strategy._activity()["2330"].buy_units, 0)

    def test_convertible_bonds_never_trade_odd_lots(self):
        self.configure(lot="AUTO", quantity=1500)
        self.service.control("arm", "plugin test", "arm-1")
        self.strategy.control("start")
        self.service.tick()
        self.assertEqual(self.strategy_orders(), [])
        self.assertIn("1,000 的倍數", str(self.strategy.observations()[0]["last_error"]))
        self.strategy.control("stop")
        self.configure(lot="AUTO", quantity=2000)
        self.strategy.control("start")
        self.service.tick()
        self.assertEqual([(self.lot_of(i), i.quantity) for i, _ in self.strategy_orders()], [("Common", 2)])

    def test_rolling_futures_alias_is_placed_on_its_month_contract(self):
        class Resolver:
            account_id, broker_id, futures_account_id = "stock-1", "B", "future-1"

            def broker_id_for(self, account_id):
                return {"stock-1": "B", "future-1": "F"}.get(account_id)

            def resolve_contract(self, symbol):
                return ContractRef("FUT", "TAIFEX", "TXFR1", "TXFJ6") if symbol == "TXFR1" else None

        store = SQLiteIntentStore(":memory:")
        self.addCleanup(store.close)
        runtime = PluginStrategyRuntime(OrderCoordinator(store), self.plugin, Resolver())
        intent = runtime._preview_intent("TXFR1")
        assert intent is not None
        self.assertEqual(intent.symbol, "TXFJ6")
        self.assertEqual(intent.broker_request["body"]["contract"]["code"], "TXFJ6")
        self.assertEqual(intent.broker_request["body"]["futures_order"]["account"]["broker_id"], "F")


if __name__ == "__main__":
    unittest.main()
