"""Read-only simulation account and market facts for the Risk Engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.request import Request, urlopen

from .broker_http import HttpOpener, ShioajiHttpDispatcher
from .order_core import RiskContext, RiskPosition


@dataclass(frozen=True)
class SimulationStockAccount:
    """Minimal broker identity retained in memory; no person or credential data."""

    broker_id: str
    account_id: str


class SimulationSnapshotProvider:
    """Fetch only broker read contracts; any malformed fact returns unavailable."""

    def __init__(self, origin: str, *, timeout_seconds: float = 5, opener: HttpOpener | None = None) -> None:
        # Reuse the dispatcher's strict credential-free loopback validation.
        validated = ShioajiHttpDispatcher(origin, timeout_seconds=timeout_seconds, opener=opener)
        self.origin = validated.origin
        self.timeout_seconds = timeout_seconds
        self.opener = opener if opener is not None else cast(HttpOpener, urlopen)

    def simulation_stock_accounts(self) -> tuple[SimulationStockAccount, ...]:
        info = self._request("/api/v1/info")
        accounts = self._request("/api/v1/auth/accounts")
        if not isinstance(info, dict) or info.get("simulation") is not True or not isinstance(accounts, list):
            return ()
        discovered: list[SimulationStockAccount] = []
        for account in accounts:
            if not isinstance(account, dict) or account.get("account_type") != "S":
                continue
            broker_id, account_id = account.get("broker_id"), account.get("account_id")
            if isinstance(broker_id, str) and broker_id and isinstance(account_id, str) and account_id:
                discovered.append(SimulationStockAccount(broker_id, account_id))
        return tuple(discovered)

    def stock_risk_context(self, account: SimulationStockAccount, symbol: str, *, exchange: str = "TSE") -> RiskContext | None:
        """Return current stock position/quote facts, never a permissive fallback."""
        body = {"account_type": "S", "broker_id": account.broker_id, "account_id": account.account_id}
        positions = self._request("/api/v1/portfolio/position_unit", {**body, "unit": "Share"})
        snapshots = self._request("/api/v1/data/snapshots", {"contracts": [{
            "security_type": "STK", "region": "TW", "exchange": exchange, "code": symbol, "target_code": None,
        }]})
        if not isinstance(positions, list) or not isinstance(snapshots, list):
            return None
        snapshot = next((item for item in snapshots if isinstance(item, dict) and item.get("code") == symbol), None)
        if not isinstance(snapshot, dict):
            return None
        quote = self._decimal(snapshot.get("close"))
        quote_at = self._timestamp(snapshot.get("datetime"))
        if quote is None or quote_at is None:
            return None
        facts: dict[str, RiskPosition] = {}
        unrealized = Decimal("0")
        for position in positions:
            if not isinstance(position, dict) or not isinstance(position.get("code"), str):
                return None
            quantity = position.get("quantity")
            price = self._decimal(position.get("last_price")) or self._decimal(position.get("price"))
            pnl = self._decimal(position.get("pnl"))
            if not isinstance(quantity, int) or price is None or pnl is None:
                return None
            signed = -quantity if position.get("direction") == "Sell" else quantity
            facts[position["code"]] = RiskPosition(signed, price)
            unrealized += pnl
        return RiskContext(quote_price=quote, quote_at=quote_at, positions=facts, unrealized_pnl=unrealized)

    def _request(self, path: str, body: dict[str, object] | None = None) -> object:
        request = Request(
            f"{self.origin}{path}", data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})},
            method="POST" if body is not None else "GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result > 0 or result == 0 else None

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
