"""The only future broker-dispatch owner; safe until a broker adapter is installed."""

from __future__ import annotations

from typing import Callable, Protocol

from .order_core import Intent, IntentStore


class Dispatcher(Protocol):
    def dispatch(self, intent: Intent) -> str: ...


class DispatchRefused(ValueError):
    """Raised only before any broker I/O: the order was definitely not sent."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__(",".join(reasons))
        self.reasons = reasons


class ExecutionWorker:
    def __init__(self, store: IntentStore, dispatcher: Dispatcher) -> None:
        self.store = store
        self.dispatcher = dispatcher

    def dispatch_once(self, ready: Callable[[Intent], bool] | None = None) -> bool:
        """Send the oldest queued delivery that ``ready`` allows; False if none.

        ``ready`` holds back an order (it stays queued, untouched) while an
        earlier order of the same product awaits broker acknowledgement.
        """
        intent_id: str | None = None
        for candidate in self.store.pending_outbox_ids():
            if ready is not None and not ready(self.store.load_intent(candidate)):
                continue
            intent_id = self.store.claim_outbox(candidate)
            if intent_id is not None:
                break
        if intent_id is None:
            return False
        try:
            intent = self.store.load_intent(intent_id)
            broker_order_id = self.dispatcher.dispatch(intent)
            if intent.operation == "PLACE":
                self.store.mark_pending_submit(intent_id, broker_order_id)
            else:
                self.store.mark_operation_pending(intent_id, broker_order_id)
        except DispatchRefused as refused:
            # The final preflight ran before the broker call; nothing was sent,
            # so this is a rejection rather than an ambiguous delivery.
            self.store.mark_dispatch_refused(intent_id, refused.reasons)
        except Exception as error:
            # A timeout/crash never implies delivery did not happen.
            self.store.mark_delivery_unknown(intent_id, f"dispatch_uncertain:{type(error).__name__}")
        return True
