from __future__ import annotations

import tempfile
import unittest
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
        return {"symbol": "11011", "side": "BUY", "quantity": 1, "max_signals": 1, "fail": False}

    def parse_config(self, raw):
        if not isinstance(raw, dict) or set(raw) != set(self.default_config()):
            raise ValueError("bad config")
        return dict(raw)

    def symbols(self, config):
        return (config["symbol"],)

    def max_signals(self, config):
        return config["max_signals"]

    def decide(self, config, markets, positions):
        if config["fail"]:
            raise RuntimeError("scripted failure")
        market = markets.get(config["symbol"])
        if market is None:
            return []
        return [OrderProposal(config["symbol"], config["side"], config["quantity"], market.last_price)]


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
            limits=RiskLimits(cooldown_seconds=0, slow_quote_symbols=("11011",)),
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
            self.configure(symbol="11011")

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
