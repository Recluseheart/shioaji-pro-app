"""Strict read-model helpers for Shioaji broker reconciliation."""

from __future__ import annotations

from .broker_http import HttpOpener
from .order_core import Status
from .simulation_snapshot import SimulationSnapshotProvider, SimulationStockAccount


_TERMINAL_STATUSES = {
    "Filled": Status.FILLED,
    "Cancelled": Status.CANCELLED,
    "Failed": Status.BROKER_REJECTED,
}


def terminal_callback_from_trade(trade: object) -> tuple[str, Status] | None:
    """Extract only an internally consistent terminal fact from a Trade read.

    Partial, inactive, pending and malformed records intentionally produce no
    local state change.  The caller must separately bind the returned broker
    identity to a durable intent before recording a terminal callback.
    """
    if not isinstance(trade, dict):
        return None
    order = trade.get("order")
    status = trade.get("status")
    if not isinstance(order, dict) or not isinstance(status, dict):
        return None
    order_id = order.get("id")
    status_id = status.get("id")
    source_status = status.get("status")
    if (
        not isinstance(order_id, str)
        or not order_id
        or order_id != status_id
        or not isinstance(source_status, str)
    ):
        return None
    terminal = _TERMINAL_STATUSES.get(source_status)
    return (order_id, terminal) if terminal is not None else None


class SimulationTradeProvider:
    """Read only account-scoped trade facts from the loopback SIM sidecar."""

    def __init__(self, origin: str, *, timeout_seconds: float = 5, opener: HttpOpener | None = None) -> None:
        self._snapshot = SimulationSnapshotProvider(
            origin, timeout_seconds=timeout_seconds, opener=opener,
        )

    def terminal_facts(self, account: SimulationStockAccount) -> tuple[tuple[str, Status], ...]:
        """Return verified terminal facts; malformed/unsettled rows are ignored."""
        result = self._snapshot._request("/api/v1/order/trades", {
            "account_type": "S", "broker_id": account.broker_id, "account_id": account.account_id,
        })
        if not isinstance(result, list):
            return ()
        return tuple(fact for item in result if (fact := terminal_callback_from_trade(item)) is not None)
