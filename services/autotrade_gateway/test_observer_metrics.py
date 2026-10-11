from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.autotrade_gateway.observer_metrics import (
    risk_usage_document,
    run_performance_document,
    today_document,
)
from services.autotrade_gateway.order_core import (
    Fill,
    Intent,
    RiskLimits,
    SQLiteIntentStore,
    Source,
    StrategyRun,
    Verdict,
)
from services.autotrade_gateway.server import orders_document


class ObserverMetricsTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteIntentStore(":memory:")
        self.addCleanup(self.store.close)
        now = datetime.now(timezone.utc)
        self.store.record_strategy_run(StrategyRun(
            "run-1", "s1", "v1", "checksum", "RUNNING", None, None, 0, now, now, None, now,
        ))
        self.clock = now - timedelta(minutes=10)

    def order(self, side, limit, fill_price=None, quantity=100, allowed=True):
        self.clock += timedelta(seconds=30)
        intent = Intent(Source.STRATEGY, "ACC", "2330", side, quantity, str(limit), "s1", "run-1",
                        created_at=self.clock)
        self.store.persist(intent, Verdict(allowed, () if allowed else ("stale_quote",)))
        if allowed:
            self.store.mark_pending_submit(intent.intent_id, f"broker-{intent.intent_id[:8]}")
        if fill_price is not None:
            self.store.record_fill(Fill(
                f"fill-{intent.intent_id}", intent.intent_id, f"broker-{intent.intent_id[:8]}",
                quantity, str(fill_price), self.clock + timedelta(seconds=1),
            ))
        return intent

    def test_run_performance_round_trips_drawdown_slippage_and_rejects(self):
        self.order("BUY", 1000, 1001)   # 10 bps worse than the limit
        self.order("SELL", 1020, 1020)  # +1,900
        self.order("BUY", 1030, 1030)
        self.order("SELL", 1000, 1000)  # -3,000
        self.order("BUY", 1000, allowed=False)
        run = run_performance_document(self.store)[0]
        self.assertEqual(run["fill_count"], 4)
        self.assertEqual(run["closed_trade_count"], 2)
        self.assertEqual(run["win_rate"], 0.5)
        self.assertEqual(Decimal(run["realized_pnl"]), Decimal("-1100"))
        self.assertEqual(Decimal(run["max_drawdown"]), Decimal("3000"))
        self.assertEqual(run["rejected_count"], 1)
        self.assertEqual(run["reject_rate"], 0.2)
        self.assertEqual(Decimal(run["avg_slippage_bps"]), Decimal("2.5"))
        self.assertIsNotNone(run["avg_fill_latency_ms"])
        self.assertTrue(run["estimate"])

    def test_today_counts_and_average_fill_price(self):
        self.order("BUY", 1000, 1001)
        self.order("BUY", 1000, allowed=False)
        orders = orders_document(self.store)
        filled = next(o for o in orders if o["filled_quantity"])
        self.assertEqual(filled["avg_fill_price"], "1001")
        today = today_document(orders)
        self.assertEqual((today["auto_order_count"], today["auto_rejected_count"], today["auto_filled_count"]),
                         (1, 1, 1))

    def test_remaining_headroom_requires_a_fresh_account_snapshot(self):
        limits = RiskLimits()
        stale = risk_usage_document([], [], [], limits=limits, account={"stale": True})
        self.assertFalse(stale["authoritative_remaining_available"])
        fresh = risk_usage_document([], [], [], limits=limits, pending=Decimal("500000"), account={
            "realized_pnl": "-30000", "unrealized_pnl": "-10000", "gross_exposure": "2000000",
            "stale": False, "source": "BROKER_ACCOUNT",
        })
        self.assertTrue(fresh["authoritative_remaining_available"])
        self.assertEqual(Decimal(fresh["daily_loss_used"]), Decimal("40000"))
        self.assertEqual(Decimal(fresh["daily_loss_remaining"]), Decimal("60000"))
        self.assertEqual(Decimal(fresh["gross_exposure_remaining"]), Decimal("7500000"))


if __name__ == "__main__":
    unittest.main()
