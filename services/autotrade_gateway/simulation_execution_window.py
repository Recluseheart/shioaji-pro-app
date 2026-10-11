"""A narrow in-process SIM execution window with durable fail-closed evidence.

This module deliberately provides no HTTP handler, scheduler, or browser
capability.  A later local-only command owner may construct it only after its
own authorization and preflight checks; remote observers and the Web UI cannot
obtain its authorization value or call ``execute``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest
from typing import Callable

from .order_core import Gate, Intent, IntentStore, OrderCoordinator, RiskContext, RiskEngine, Status
from .worker import ExecutionWorker


class SimulationExecutionPreflightError(ValueError):
    """The window was not opened because durable state is not safe to extend."""


@dataclass(frozen=True)
class SimulationExecutionResult:
    """The durable outcome of one authorized in-process dispatch attempt."""

    intent_id: str
    status: Status
    dispatched: bool
    risk_reasons: tuple[str, ...]


class SimulationExecutionWindow:
    """Run at most one fresh intent through Risk and the sole worker owner.

    The durable trading gate is intentionally never changed to ``ARMED``.  The
    temporary gate exists only while ``OrderCoordinator.accept`` runs, so a
    process crash restarts DISARMED and existing recovery converts an uncertain
    delivery to UNKNOWN rather than retrying it.  The caller owns the secret
    comparison value; no browser or HTTP route is given a path to it here.
    """

    def __init__(
        self,
        store: IntentStore,
        worker: ExecutionWorker,
        *,
        authorization: str,
        risk: RiskEngine | None = None,
        risk_context_provider: Callable[[Intent], RiskContext | None] | None = None,
    ) -> None:
        if not authorization:
            raise ValueError("simulation window authorization is required")
        self.store = store
        self.worker = worker
        self.authorization = authorization
        self.risk = risk or RiskEngine()
        self.risk_context_provider = risk_context_provider

    def execute(
        self,
        intent: Intent,
        *,
        authorization: str,
        reason: str,
    ) -> SimulationExecutionResult:
        """Persist, dispatch, and immediately return to a durable DISARMED state."""
        if not authorization or not compare_digest(self.authorization, authorization):
            raise PermissionError("simulation window authorization is invalid")
        if not reason.strip():
            raise ValueError("simulation window reason is required")
        self._require_safe_preflight()

        coordinator = OrderCoordinator(
            self.store,
            gate=Gate.ARMED,
            risk=self.risk,
            risk_context_provider=self.risk_context_provider,
        )
        status, verdict = coordinator.accept(intent)
        self.store.record_audit_event(
            intent.intent_id,
            "SIMULATION_WINDOW_ACCEPTED",
            {"reason": reason, "status": status.value, "risk_reasons": list(verdict.reasons)},
        )
        dispatched = False
        try:
            if status is Status.APPROVED:
                dispatched = self.worker.dispatch_once()
                if not dispatched:
                    # A missing lease is an ownership ambiguity, not proof no
                    # broker call happened.  It becomes non-claimable UNKNOWN.
                    self.store.mark_delivery_unknown(intent.intent_id, "simulation_window_claim_missing")
            result_status = self.store.status(intent.intent_id)
        except BaseException:
            if self.store.has_intent(intent.intent_id):
                self.store.mark_delivery_unknown(intent.intent_id, "simulation_window_interrupted")
                self.store.record_audit_event(
                    intent.intent_id,
                    "SIMULATION_WINDOW_ABORTED",
                    {"reason": reason},
                )
            raise
        self.store.record_audit_event(
            intent.intent_id,
            "SIMULATION_WINDOW_FINISHED",
            {"reason": reason, "status": result_status.value, "dispatched": dispatched},
        )
        return SimulationExecutionResult(
            intent_id=intent.intent_id,
            status=result_status,
            dispatched=dispatched,
            risk_reasons=verdict.reasons,
        )

    def _require_safe_preflight(self) -> None:
        if self.store.trading_gate() is Gate.KILLED:
            raise SimulationExecutionPreflightError("kill switch is active")
        unresolved_statuses = {
            Status.APPROVED.value,
            Status.SENDING.value,
            Status.PENDING_SUBMIT.value,
            Status.SUBMITTED.value,
            Status.UNKNOWN.value,
            Status.RECONCILING.value,
            Status.MANUAL_REVIEW.value,
        }
        if any(
            item.get("status") in unresolved_statuses
            for item in self.store.observation_orders()
        ):
            raise SimulationExecutionPreflightError("unresolved intent is already present")
        if any(
            item.get("state") != "RESOLVED"
            for item in self.store.observation_reconciliation_cases()
        ):
            raise SimulationExecutionPreflightError("open reconciliation case is already present")
