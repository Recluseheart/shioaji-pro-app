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
from datetime import datetime, timezone
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
class MaskedSimulationAccount:
    account_type: str
    account_label: str
    signed: bool


@dataclass(frozen=True)
class ConnectionObservation:
    state: ConnectionState
    accounts: tuple[MaskedSimulationAccount, ...]
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


def mask_account_id(account_id: str) -> str:
    """Return a stable UI label without disclosing the broker/account number."""
    suffix = account_id[-2:] if len(account_id) >= 2 else "**"
    return f"***{suffix}"


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
        self._latest = ConnectionObservation(ConnectionState.DISCONNECTED, (), self._timestamp())

    def probe(self) -> ConnectionObservation:
        """Read session/account/market readiness without making a broker mutation."""
        with self._lock:
            self._latest = ConnectionObservation(ConnectionState.CONNECTING, (), self._timestamp())
            health = self._request("/api/v1/health")
            if health is None:
                return self._record(ConnectionState.LOST if self._last_healthy else ConnectionState.DISCONNECTED)
            info = self._request("/api/v1/info")
            if not isinstance(info, dict) or info.get("simulation") is not True:
                return self._record(ConnectionState.DEGRADED)
            accounts_document = self._request("/api/v1/auth/accounts")
            if not isinstance(accounts_document, list):
                return self._record(ConnectionState.DEGRADED)
            accounts = tuple(
                account for item in accounts_document
                if (account := self._masked_account(item)) is not None
            )
            # An empty authenticated account list is still a valid session
            # posture, but it cannot be claimed market-data-ready.
            if not accounts:
                self._last_healthy = True
                return self._record(ConnectionState.AUTHENTICATED, accounts)
            subscriptions = self._request("/api/v1/monitor/subscriptions")
            if not isinstance(subscriptions, (dict, list)):
                return self._record(ConnectionState.DEGRADED, accounts)
            self._last_healthy = True
            return self._record(ConnectionState.MARKET_DATA_READY, accounts)

    def latest(self) -> ConnectionObservation:
        with self._lock:
            return self._latest

    def _record(
        self,
        state: ConnectionState,
        accounts: tuple[MaskedSimulationAccount, ...] = (),
    ) -> ConnectionObservation:
        self._latest = ConnectionObservation(state, accounts, self._timestamp())
        return self._latest

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
    def _masked_account(value: object) -> MaskedSimulationAccount | None:
        if not isinstance(value, dict):
            return None
        account_type = value.get("account_type")
        account_id = value.get("account_id")
        signed = value.get("signed")
        if account_type not in {"S", "F"} or not isinstance(account_id, str) or not account_id:
            return None
        return MaskedSimulationAccount(
            account_type=account_type,
            account_label=mask_account_id(account_id),
            signed=signed is True,
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
