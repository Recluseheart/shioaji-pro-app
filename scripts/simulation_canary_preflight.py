"""Fail-closed read-only preflight for a separately authorized SIM canary.

This script deliberately has no order payload, dispatcher, worker or mutation
route. A passing result is only a prerequisite for a later controlled canary.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Direct script execution sets sys.path to scripts/, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.autotrade_gateway.simulation_snapshot import SimulationSnapshotProvider


def main() -> int:
    if os.getenv("AUTOTRADE_ENV", "SIM").upper() != "SIM":
        print("preflight refused: AUTOTRADE_ENV must be SIM", file=sys.stderr)
        return 2
    provider = SimulationSnapshotProvider(
        os.getenv("SHIOAJI_HTTP_ORIGIN", "http://127.0.0.1:21322")
    )
    accounts = provider.simulation_stock_accounts()
    if len(accounts) != 1:
        print("preflight refused: expected exactly one verified SIM stock account", file=sys.stderr)
        return 3
    if provider.stock_risk_context(accounts[0], "2330") is None:
        print("preflight refused: fresh 2330 risk context is unavailable", file=sys.stderr)
        return 4
    print("simulation canary preflight passed: SIM account and fresh risk context verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
