"""Small compatibility surface for PAPER; never forwards a request upstream."""

from datetime import datetime, timezone

from .simulation_connection import (
    ConnectionObservation,
    ConnectionState,
    MaskedSimulationAccount,
)


class PaperConnectionMonitor:
    def probe(self) -> ConnectionObservation:
        return ConnectionObservation(
            ConnectionState.MARKET_DATA_READY,
            (MaskedSimulationAccount("S", "PAPER-LOCAL", True),),
            datetime.now(timezone.utc).isoformat(),
        )

    def latest(self) -> ConnectionObservation:
        return self.probe()


def read_document(path: str, method: str, runtime) -> tuple[int, object]:
    account = {
        "account_type": "S",
        "account_id": "PAPER-LOCAL",
        "broker_id": "LOCAL",
        "signed": True,
        "username": "本機紙上交易",
    }
    if path == "/api/v1/health":
        return 200, {"status": "ok", "mode": "PAPER"}
    if path == "/api/v1/info":
        return 200, {
            "simulation": True,
            "mode": "PAPER",
            "version": "paper-local",
            "accounts": [account],
        }
    if path == "/api/v1/auth/accounts":
        return 200, [account]
    if path == "/api/v1/order/trades":
        return 200, [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in runtime.latest_trades.values()
        ]
    if (
        path
        in {
            "/api/v1/monitor/subscriptions",
            "/api/v1/watchlist",
            "/api/v1/order/combotrades",
        }
        and method == "GET"
    ):
        return 200, []
    return 409, {
        "message": "此功能需要券商服務；PAPER 僅提供本機策略、委託與成交驗證",
        "mode": "PAPER",
    }
