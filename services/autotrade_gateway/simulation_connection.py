"""Read-only Shioaji SIM sidecar session posture for Gateway observers.

The native sidecar is the credential and SDK-session owner.  This module never
accepts API keys, invokes a login endpoint, activates CA, or exposes a raw
account identity.  It only probes the loopback sidecar's already-authenticated
read contracts so a Gateway/UI can distinguish an unavailable process from an
authenticated account whose market-data reads are not yet ready.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import Lock
from typing import cast
from urllib.request import Request, urlopen

from .broker_http import HttpOpener, ShioajiHttpDispatcher


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATED = "AUTHENTICATED"
    MARKET_DATA_READY = "MARKET_DATA_READY"
    DEGRADED = "DEGRADED"
    LOST = "LOST"


@dataclass(frozen=True)
class SimulationAccountView:
    account_type: str
    account_label: str
    signed: bool


@dataclass(frozen=True)
class ConnectionObservation:
    state: ConnectionState
    accounts: tuple[SimulationAccountView, ...]
    observed_at: str

    def document(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "accounts": [
                {
                    "account_type": account.account_type,
                    "account_label": account.account_label,
                    "signed": account.signed,
                }
                for account in self.accounts
            ],
            "last_updated_at": self.observed_at,
        }


class SimulationConnectionMonitor:
    """Probe only loopback SIM reads; retain enough state to report a loss."""

    def __init__(
        self,
        origin: str,
        *,
        timeout_seconds: float = 3,
        opener: HttpOpener | None = None,
    ) -> None:
        validated = ShioajiHttpDispatcher(origin, timeout_seconds=timeout_seconds, opener=opener)
        self.origin = validated.origin
        self.timeout_seconds = timeout_seconds
        self.opener = opener if opener is not None else cast(HttpOpener, urlopen)
        self._lock = Lock()
        self._last_healthy = False
        self._failure_count = 0
        self._retry_not_before: datetime | None = None
        self._latest = ConnectionObservation(ConnectionState.DISCONNECTED, (), self._timestamp())

    def probe(self) -> ConnectionObservation:
        """Read session/account/market readiness without making a broker mutation."""
        with self._lock:
            if self._retry_not_before is not None and datetime.now(timezone.utc) < self._retry_not_before:
                return self._latest
            self._latest = ConnectionObservation(ConnectionState.CONNECTING, (), self._timestamp())
            health = self._request("/api/v1/health")
            if health is None:
                return self._record_failure()
            if isinstance(health, dict) and health.get("status") in {"starting", "connecting"}:
                return self._record(ConnectionState.CONNECTING)
            info = self._request("/api/v1/info")
            if not isinstance(info, dict) or info.get("simulation") is not True:
                return self._record_failure(ConnectionState.DEGRADED)
            accounts_document = self._request("/api/v1/auth/accounts")
            if not isinstance(accounts_document, list):
                return self._record_failure(ConnectionState.DEGRADED)
            accounts = tuple(
                account for item in accounts_document
                if (account := self._account_view(item)) is not None
            )
            # An empty authenticated account list is still a valid session
            # posture, but it cannot be claimed market-data-ready.
            if not accounts:
                self._last_healthy = True
                return self._record(ConnectionState.AUTHENTICATED, accounts)
            subscriptions = self._request("/api/v1/monitor/subscriptions")
            if not isinstance(subscriptions, (dict, list)):
                return self._record_failure(ConnectionState.DEGRADED, accounts)
            self._last_healthy = True
            self._failure_count = 0
            self._retry_not_before = None
            return self._record(ConnectionState.MARKET_DATA_READY, accounts)

    def latest(self) -> ConnectionObservation:
        with self._lock:
            return self._latest

    def _record(
        self,
        state: ConnectionState,
        accounts: tuple[SimulationAccountView, ...] = (),
    ) -> ConnectionObservation:
        self._latest = ConnectionObservation(state, accounts, self._timestamp())
        return self._latest

    def _record_failure(
        self,
        state: ConnectionState | None = None,
        accounts: tuple[SimulationAccountView, ...] | None = None,
    ) -> ConnectionObservation:
        """Apply bounded exponential backoff without retrying a sidecar mutation."""
        self._failure_count = min(self._failure_count + 1, 5)
        delay_seconds = min(30, 2 ** (self._failure_count - 1))
        self._retry_not_before = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        return self._record(
            state or (ConnectionState.LOST if self._last_healthy else ConnectionState.DISCONNECTED),
            self._latest.accounts if accounts is None else accounts,
        )

    def _request(self, path: str) -> object | None:
        request = Request(
            f"{self.origin}{path}", headers={"Accept": "application/json"}, method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _account_view(value: object) -> SimulationAccountView | None:
        if not isinstance(value, dict):
            return None
        account_type = value.get("account_type")
        account_id = value.get("account_id")
        signed = value.get("signed")
        if account_type not in {"S", "F"} or not isinstance(account_id, str) or not account_id:
            return None
        return SimulationAccountView(
            account_type=account_type,
            # The operator chose to see full account numbers; person id,
            # username and broker branch are still never projected.
            account_label=account_id,
            signed=signed is True,
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
