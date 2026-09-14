"""The only future broker-dispatch owner; safe until a broker adapter is installed."""

from __future__ import annotations

from typing import Protocol


class Dispatcher(Protocol):
    def dispatch(self, intent_id: str) -> str: ...


class ExecutionWorker:
    def __init__(self, store, dispatcher: Dispatcher) -> None:
        self.store = store
        self.dispatcher = dispatcher

    def dispatch_once(self) -> bool:
        intent_id = self.store.claim_outbox()
        if intent_id is None:
            return False
        try:
            broker_order_id = self.dispatcher.dispatch(intent_id)
            self.store.mark_pending_submit(intent_id, broker_order_id)
        except Exception as error:
            # A timeout/crash never implies delivery did not happen.
            self.store.mark_delivery_unknown(intent_id, f"dispatch_uncertain:{type(error).__name__}")
        return True
