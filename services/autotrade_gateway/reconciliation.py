"""Strict read-model helpers for Shioaji broker reconciliation."""

from __future__ import annotations

from .order_core import Status


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
