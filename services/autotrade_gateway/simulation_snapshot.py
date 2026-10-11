"""Read-only simulation account and market facts for the Risk Engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, cast
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .broker_http import HttpOpener, ShioajiHttpDispatcher
from .order_core import PositionSnapshot, RiskContext, RiskPosition, futures_root

TAIPEI = ZoneInfo("Asia/Taipei")
_CONTRACT_CODE = re.compile(r"[A-Z0-9]{2,12}")


@dataclass(frozen=True)
class SimulationStockAccount:
    """Minimal broker identity retained in memory; no person or credential data."""

    broker_id: str
    account_id: str


@dataclass(frozen=True)
class SimulationFuturesAccount:
    """A verified SIM futures/options account identity (account_type F)."""

    broker_id: str
    account_id: str


@dataclass(frozen=True)
class SimulationAccount:
    """Minimal verified SIM identity used only for read-only account facts."""

    account_type: str
    broker_id: str
    account_id: str


@dataclass(frozen=True)
class ContractRef:
    """The broker's own contract identity; never inferred from a symbol shape."""

    security_type: str
    exchange: str
    code: str
    target_code: str | None = None

    def wire(self) -> dict[str, object]:
        return {
            "security_type": self.security_type,
            "region": "TW",
            "exchange": self.exchange,
            "code": self.code,
            "target_code": self.target_code,
        }


class SimulationSnapshotProvider:
    """Fetch only broker read contracts; any malformed fact returns unavailable."""

    def __init__(self, origin: str, *, timeout_seconds: float = 5, opener: HttpOpener | None = None) -> None:
        # Reuse the dispatcher's strict credential-free loopback validation.
        validated = ShioajiHttpDispatcher(origin, timeout_seconds=timeout_seconds, opener=opener)
        self.origin = validated.origin
        self.timeout_seconds = timeout_seconds
        self.opener = opener if opener is not None else cast(HttpOpener, urlopen)

    def simulation_stock_accounts(self) -> tuple[SimulationStockAccount, ...]:
        """Retain the existing stock-only Risk Engine contract."""
        return tuple(
            SimulationStockAccount(account.broker_id, account.account_id)
            for account in self.simulation_accounts()
            if account.account_type == "S"
        )

    def simulation_futures_accounts(self) -> tuple[SimulationFuturesAccount, ...]:
        return tuple(
            SimulationFuturesAccount(account.broker_id, account.account_id)
            for account in self.simulation_accounts()
            if account.account_type == "F"
        )

    def contract(self, code: str) -> ContractRef | None:
        """Resolve exchange/security type from the sidecar's contract table."""
        if not _CONTRACT_CODE.fullmatch(code or ""):
            return None
        value = self._request(f"/api/v1/data/contracts/{code}")
        if not isinstance(value, dict) or value.get("code") != code:
            return None
        security_type, exchange = value.get("security_type"), value.get("exchange")
        target = value.get("target_code")
        if security_type not in {"STK", "FUT"} or not isinstance(exchange, str) or not exchange:
            return None
        if target is not None and (not isinstance(target, str) or not _CONTRACT_CODE.fullmatch(target)):
            return None
        return ContractRef(str(security_type), exchange, code, target)

    def simulation_accounts(self) -> tuple[SimulationAccount, ...]:
        """Discover only accounts from an already-authenticated SIM session."""
        info = self._request("/api/v1/info")
        accounts = self._request("/api/v1/auth/accounts")
        if not isinstance(info, dict) or info.get("simulation") is not True or not isinstance(accounts, list):
            return ()
        discovered: list[SimulationAccount] = []
        for account in accounts:
            if not isinstance(account, dict) or account.get("account_type") not in {"S", "F"}:
                continue
            broker_id, account_id = account.get("broker_id"), account.get("account_id")
            if isinstance(broker_id, str) and broker_id and isinstance(account_id, str) and account_id:
                discovered.append(SimulationAccount(str(account["account_type"]), broker_id, account_id))
        return tuple(discovered)

    def position_snapshots(
        self, *, observed_at: datetime | None = None,
    ) -> tuple[PositionSnapshot, ...] | None:
        """Fetch all verified SIM positions as immutable, validated facts.

        This is deliberately a read-only sidecar sequence.  A malformed or
        unavailable response returns ``None`` rather than treating unknown
        broker state as an empty portfolio.  Account identifiers are retained
        only in the returned durable facts; Gateway projections mask them.
        """
        info = self._request("/api/v1/info")
        accounts_response = self._request("/api/v1/auth/accounts")
        if not isinstance(info, dict) or info.get("simulation") is not True or not isinstance(accounts_response, list):
            return None
        accounts: list[SimulationAccount] = []
        for raw_account in accounts_response:
            if not isinstance(raw_account, dict) or raw_account.get("account_type") not in {"S", "F"}:
                continue
            broker_id, account_id = raw_account.get("broker_id"), raw_account.get("account_id")
            if not isinstance(broker_id, str) or not broker_id or not isinstance(account_id, str) or not account_id:
                return None
            accounts.append(SimulationAccount(str(raw_account["account_type"]), broker_id, account_id))
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("position snapshot observation time must have a timezone")
        snapshots: list[PositionSnapshot] = []
        for account in accounts:
            positions = self._request("/api/v1/portfolio/position_unit", {
                "account_type": account.account_type,
                "broker_id": account.broker_id,
                "account_id": account.account_id,
                "unit": "Share" if account.account_type == "S" else "Common",
            })
            if not isinstance(positions, list):
                return None
            for position in positions:
                snapshot = self._position_snapshot(account, position, timestamp)
                if snapshot is None:
                    return None
                snapshots.append(snapshot)
        return tuple(snapshots)

    def stock_risk_context(self, account: SimulationStockAccount, symbol: str, *, exchange: str = "TSE") -> RiskContext | None:
        """Return current stock/CB position (shares) and quote facts, never a permissive fallback."""
        return self._risk_context(
            "S", account.broker_id, account.account_id,
            ContractRef("STK", exchange, symbol), unit="Share", multiplier=Decimal("1"),
        )

    def futures_risk_context(
        self, account: SimulationFuturesAccount, contract: ContractRef, *, multiplier: Decimal,
    ) -> RiskContext | None:
        """Return futures positions in contracts, aggregated per product root.

        Monthly codes (TXFJ6) and rolling aliases (TXFR1) of one product share
        a single position limit, so positions are keyed by the root (``TXF``).
        Exposure is contract value: contracts x price x multiplier.
        """
        if contract.security_type != "FUT":
            return None
        return self._risk_context(
            "F", account.broker_id, account.account_id, contract,
            unit="Common", multiplier=multiplier, key=futures_root,
        )

    def _risk_context(
        self,
        account_type: str,
        broker_id: str,
        account_id: str,
        contract: ContractRef,
        *,
        unit: str,
        multiplier: Decimal,
        key: Callable[[str], str | None] = lambda code: code,
    ) -> RiskContext | None:
        body = {"account_type": account_type, "broker_id": broker_id, "account_id": account_id}
        positions = self._request("/api/v1/portfolio/position_unit", {**body, "unit": unit})
        snapshots = self._request("/api/v1/data/snapshots", {"contracts": [contract.wire()]})
        if not isinstance(positions, list) or not isinstance(snapshots, list):
            return None
        realized = self.realized_pnl(account_type, broker_id, account_id)
        if realized is None:
            return None
        quoted = {contract.code, contract.target_code}
        snapshot = next((item for item in snapshots if isinstance(item, dict) and item.get("code") in quoted), None)
        if not isinstance(snapshot, dict):
            return None
        quote = self._decimal(snapshot.get("close"))
        quote_at = self._timestamp(snapshot.get("datetime"))
        if quote is None or quote_at is None:
            return None
        facts: dict[str, RiskPosition] = {}
        unrealized = Decimal("0")
        gross = Decimal("0")
        for position in positions:
            if not isinstance(position, dict) or not isinstance(position.get("code"), str):
                return None
            quantity = position.get("quantity")
            price = self._decimal(position.get("last_price")) or self._decimal(position.get("price"))
            pnl = self._signed_decimal(position.get("pnl"))
            position_key = key(position["code"])
            if (not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0
                    or price is None or price <= 0 or pnl is None or position_key is None
                    or position.get("direction") not in {"Buy", "Sell"}):
                return None
            signed = -quantity if position.get("direction") == "Sell" else quantity
            previous = facts.get(position_key, RiskPosition())
            facts[position_key] = RiskPosition(previous.quantity + signed, price)
            unrealized += pnl
            gross += abs(quantity) * price * multiplier
        return RiskContext(quote_price=quote, quote_at=quote_at, positions=facts,
                           realized_pnl=realized, unrealized_pnl=unrealized, gross_exposure=gross,
                           quote_bid=self._decimal(snapshot.get("buy_price")) or None,
                           quote_ask=self._decimal(snapshot.get("sell_price")) or None)

    def realized_pnl(self, account_type: str, broker_id: str, account_id: str) -> Decimal | None:
        """Use today's SIM realized-trade list, never the empty summary fallback."""
        day = datetime.now(TAIPEI).date()
        rows = self._request("/api/v1/portfolio/profit_loss", {
            "account_type": account_type, "broker_id": broker_id, "account_id": account_id,
            "begin_date": day.isoformat(), "end_date": day.isoformat(),
        })
        if not isinstance(rows, list):
            return None
        result = Decimal("0")
        identities: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get("date", "")).replace("-", "") != day.strftime("%Y%m%d"):
                return None
            pnl = self._signed_decimal(row.get("pnl"))
            if pnl is None or "id" not in row:
                return None
            identity = str(row["id"])
            if identity in identities:
                return None
            identities.add(identity)
            result += pnl
        return result

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
        return result if result.is_finite() and result >= 0 else None

    @staticmethod
    def _signed_decimal(value: object) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None

    def _position_snapshot(
        self, account: SimulationAccount, position: object, observed_at: datetime,
    ) -> PositionSnapshot | None:
        if not isinstance(position, dict):
            return None
        code, direction = position.get("code"), position.get("direction")
        if not isinstance(code, str) or not code or direction not in {"Buy", "Sell"}:
            return None
        quantity = self._decimal(position.get("quantity"))
        average_price = self._decimal(position.get("price"))
        market_price = self._decimal(position.get("last_price"))
        unrealized_pnl = self._signed_decimal(position.get("pnl"))
        if quantity is None or quantity <= 0 or average_price is None or market_price is None or unrealized_pnl is None:
            return None
        side = "LONG" if direction == "Buy" else "SHORT"
        # The deterministic identity makes an upstream retry of this exact
        # read idempotent while the broker identity itself never leaves the
        # durable store or enters an audit event/API projection.
        snapshot_id = str(uuid5(
            NAMESPACE_URL,
            f"sidecar-position:{account.account_type}:{account.account_id}:{code}:{side}:{observed_at.isoformat()}",
        ))
        return PositionSnapshot(
            snapshot_id=snapshot_id,
            account_type=account.account_type,
            account_id=account.account_id,
            symbol=code,
            side=side,
            quantity=format(quantity.normalize(), "f"),
            average_price=format(average_price.normalize(), "f"),
            market_price=format(market_price.normalize(), "f"),
            unrealized_pnl=format(unrealized_pnl.normalize(), "f"),
            observed_at=observed_at,
        )

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Sidecar 1.7.5 snapshots report naive Taiwan market wall-clock time
        # (an OTC print at 10:36 falls inside the 09:00-13:30 session).  Read
        # as UTC, every fresh quote would appear eight hours in the future and
        # the Risk Engine would reject all SIM orders as stale.
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=TAIPEI)
