"""Strict read-model helpers for Shioaji broker reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .broker_http import HttpOpener
from .order_core import IntentStore, Status
from .simulation_snapshot import SimulationSnapshotProvider, SimulationStockAccount


_TERMINAL_STATUSES = {
    "Filled": Status.FILLED,
    "Cancelled": Status.CANCELLED,
    "Failed": Status.BROKER_REJECTED,
}


class TerminalFactProvider(Protocol):
    """Read-only account-scoped source used by reconciliation refresh."""

    def terminal_facts(self, account: SimulationStockAccount) -> tuple[tuple[str, Status], ...]: ...


@dataclass(frozen=True)
class ReconciliationRefresh:
    """Durable changes caused by one broker read; never a dispatch receipt."""

    observed_terminal_facts: int
    resolved_intents: int


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


def apply_reconciled_terminals(store: IntentStore, facts: tuple[tuple[str, Status], ...]) -> int:
    """Resolve only local UNKNOWN/RECONCILING orders with matching broker facts.

    Broker-only facts and local orders without a matching terminal fact remain
    untouched for operator review; this function never claims or creates outbox work.
    """
    # A read can contain duplicate observations (for example while a broker is
    # normalizing an event stream).  Identical duplicates are harmless, but
    # conflicting terminal claims cannot be resolved by choosing the last
    # array element.  Leave that identity UNKNOWN for a later read/manual
    # reconciliation instead of asserting an arbitrary terminal result.
    by_broker_id: dict[str, Status] = {}
    conflicted_broker_ids: set[str] = set()
    for broker_order_id, terminal in facts:
        if broker_order_id in conflicted_broker_ids:
            continue
        previous = by_broker_id.get(broker_order_id)
        if previous is None:
            by_broker_id[broker_order_id] = terminal
        elif previous != terminal:
            by_broker_id.pop(broker_order_id)
            conflicted_broker_ids.add(broker_order_id)
    resolved = 0
    for order in store.observation_orders():
        if order.get("status") not in {Status.UNKNOWN.value, Status.RECONCILING.value}:
            continue
        intent_id, broker_order_id = order.get("intent_id"), order.get("broker_order_id")
        terminal = by_broker_id.get(broker_order_id) if isinstance(broker_order_id, str) else None
        if isinstance(intent_id, str) and isinstance(broker_order_id, str) and terminal is not None:
            store.record_reconciled_terminal(intent_id, broker_order_id, terminal)
            resolved += 1
    return resolved


class ReconciliationRefresher:
    """Perform one account-scoped broker read and apply only safe terminals."""

    def __init__(
        self,
        store: IntentStore,
        provider: TerminalFactProvider,
        account: SimulationStockAccount,
    ) -> None:
        self.store = store
        self.provider = provider
        self.account = account

    def refresh(self) -> ReconciliationRefresh:
        facts = self.provider.terminal_facts(self.account)
        return ReconciliationRefresh(
            observed_terminal_facts=len(facts),
            resolved_intents=apply_reconciled_terminals(self.store, facts),
        )
