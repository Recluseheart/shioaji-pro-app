"""Broker-free strategy ingress migrated from the legacy reference runner.

The runtime deliberately has no market subscription and no HTTP control route.
It records a disabled reference fixture so the Web UI can show an honest
strategy lifecycle before a separately-authorized execution worker is added.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from .order_core import Intent, OrderCoordinator, Source, Status, StrategyDefinition, StrategyRun, StrategySignal, Verdict


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrategyRuntime:
    """One safe strategy ingress; it owns neither a broker nor a market feed.

    ``allow_reference_fixture`` exists only for isolated test harnesses.  The
    Gateway constructs this class with its default ``False`` value, so a
    process receiving an arbitrary market event cannot emit an order intent.
    """

    strategy_id = "reference-fixture"
    version = "v1"
    config_checksum = "reference-fixture-v1"
    allowed_symbols = ("2330",)
    reference_account_id = "SIM-001"
    reference_broker_id = "SIM-BROKER"

    def __init__(
        self,
        coordinator: OrderCoordinator,
        *,
        allow_reference_fixture: bool = False,
        allow_lifecycle: bool = False,
        reference_account_id: str = "SIM-001",
        reference_broker_id: str = "SIM-BROKER",
    ) -> None:
        self._coordinator = coordinator
        self._allow_reference_fixture = allow_reference_fixture
        self._allow_lifecycle = allow_lifecycle
        self.reference_account_id = reference_account_id
        self.reference_broker_id = reference_broker_id
        self._active_run_id: str | None = None
        self._processed_event_ids: set[str] = set()
        self._last_event_id: str | None = None
        self._last_intent_id: str | None = None
        self._last_status: Status | None = None
        self._coordinator.store.record_strategy_definition(StrategyDefinition(
            strategy_id=self.strategy_id, version=self.version,
            config_checksum=self.config_checksum, state="DISABLED", enabled=False,
            allowed_symbols=self.allowed_symbols,
        ))
        self._restore_latest_durable_signal()
        self._restore_active_run()

    def _restore_active_run(self) -> None:
        """Recover lifecycle context only; never revive mutation authority."""
        run = next((
            item for item in self._coordinator.store.observation_strategy_runs()
            if item.get("strategy_id") == self.strategy_id
            and item.get("state") in {"RUNNING", "PAUSED"}
        ), None)
        run_id = run.get("run_id") if isinstance(run, dict) else None
        self._active_run_id = run_id if isinstance(run_id, str) else None

    def _restore_latest_durable_signal(self) -> None:
        """Restore read-only operator context after a process restart.

        The signal journal is the durable source of truth.  Hydration must
        never call a strategy, Risk Engine, outbox, or broker: it only keeps
        the disabled strategy panel from claiming there was no recent event.
        """
        signal = next((
            item for item in self._coordinator.store.observation_signals()
            if item.get("strategy_id") == self.strategy_id
        ), None)
        if signal is None:
            return
        event_id = signal.get("event_id")
        intent_id = signal.get("intent_id")
        status = signal.get("status")
        self._last_event_id = event_id if isinstance(event_id, str) else None
        self._last_intent_id = intent_id if isinstance(intent_id, str) else None
        try:
            self._last_status = Status(status) if isinstance(status, str) else None
        except ValueError:
            self._last_status = None

    def observations(self) -> list[dict[str, object]]:
        definitions = self._coordinator.store.observation_strategy_definitions()
        return [
            {
                **definition,
                "lifecycle_enabled": self._allow_lifecycle,
                "last_event_id": self._last_event_id,
                "last_intent_id": self._last_intent_id,
                "last_status": self._last_status.value if self._last_status else None,
            }
            for definition in definitions
            if definition.get("strategy_id") == self.strategy_id
        ]

    @property
    def lifecycle_enabled(self) -> bool:
        """Whether HTTP lifecycle control may enter the broker-free state machine."""
        return self._allow_lifecycle

    def validate_document(self) -> dict[str, object]:
        """Return static fixture validation without starting a runtime."""
        return {
            "mode": "VALIDATE",
            "valid": True,
            "strategy": self.observations()[0],
            "warnings": ["strategy_runtime_disabled", "no_market_subscription", "no_broker_capability"],
        }

    def dry_run_document(self) -> dict[str, object]:
        """Preview the common risk verdict without persisting or dispatching."""
        intent = self._fixture_intent("dry-run", utc_now())
        verdict = self._coordinator.risk.evaluate(intent, self._coordinator.gate)
        return {
            "mode": "DRY_RUN",
            "would_persist": False,
            "would_create_outbox": False,
            "would_dispatch_broker": False,
            "intent": {
                "source": intent.source.value,
                "strategy_id": intent.strategy_id,
                "run_id": intent.run_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "limit_price": intent.limit_price,
            },
            "risk": {"allowed": verdict.allowed, "reasons": list(verdict.reasons)},
        }

    def control(self, action: str) -> dict[str, object]:
        """Apply an explicitly enabled, broker-free lifecycle transition.

        Controls only persist strategy/run state and a heartbeat.  They never
        arm the Gateway, create an intent, claim outbox work, or call a broker.
        """
        if not self._allow_lifecycle:
            raise RuntimeError("strategy lifecycle is not explicitly enabled")
        timestamp = utc_now()
        if action == "start":
            if self._active_run_id is not None:
                raise ValueError("strategy already has an active run")
            run_id = str(uuid4())
            self._coordinator.store.record_strategy_run(StrategyRun(
                run_id=run_id, strategy_id=self.strategy_id, version=self.version,
                config_checksum=self.config_checksum, state="RUNNING", last_intent_id=None,
                last_status=None, intent_count=0, started_at=timestamp,
                heartbeat_at=timestamp, ended_at=None, updated_at=timestamp,
            ))
            self._coordinator.store.set_strategy_definition_state(self.strategy_id, "RUNNING", True)
            self._active_run_id = run_id
            return {"state": "RUNNING", "enabled": True, "run_id": run_id}
        if action not in {"pause", "resume", "stop"} or self._active_run_id is None:
            raise ValueError("strategy has no active run")
        definitions = self.observations()
        current = definitions[0].get("state") if definitions else None
        expected = {"pause": "RUNNING", "resume": "PAUSED", "stop": {"RUNNING", "PAUSED"}}[action]
        if current != expected and not (isinstance(expected, set) and current in expected):
            raise ValueError(f"cannot {action} a {current} strategy")
        state = {"pause": "PAUSED", "resume": "RUNNING", "stop": "STOPPED"}[action]
        enabled = state == "RUNNING"
        self._coordinator.store.transition_strategy_run(self._active_run_id, state, timestamp)
        self._coordinator.store.set_strategy_definition_state(self.strategy_id, state, enabled)
        result = {"state": state, "enabled": enabled, "run_id": self._active_run_id}
        if action == "stop":
            self._active_run_id = None
        return result

    def process_fixture_event(self, event: dict[str, object]) -> tuple[Status, Verdict] | None:
        """Submit one deterministic fixture intent only in an isolated harness.

        Production Gateway construction leaves fixture processing off.  The
        call still enters the shared coordinator, which means Risk Engine and
        durable intent persistence cannot be bypassed.
        """
        if not self._allow_reference_fixture or event.get("reference_signal") is not True:
            return None
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in self._processed_event_ids:
            return None
        intent = self._fixture_intent(event_id, self._parse_observed_at(event.get("observed_at")))
        # The event id is mapped to a deterministic intent id.  This lookup
        # extends in-memory event dedupe across a process restart and prevents
        # a revived runtime from creating a second broker delivery attempt.
        if self._coordinator.store.has_intent(intent.intent_id):
            self._processed_event_ids.add(event_id)
            existing = self._coordinator.store.load_intent(intent.intent_id)
            observation = next((
                item for item in self._coordinator.store.observation_orders()
                if item.get("intent_id") == intent.intent_id
            ), {})
            reasons = observation.get("risk_reasons")
            self._record_fixture_signal(
                event_id,
                existing,
                self._coordinator.store.status(intent.intent_id),
                tuple(reason for reason in reasons if isinstance(reason, str))
                if isinstance(reasons, list) else (),
            )
            self._record_fixture_run(existing, self._coordinator.store.status(intent.intent_id))
            self._last_event_id = event_id
            self._last_intent_id = existing.intent_id
            self._last_status = self._coordinator.store.status(intent.intent_id)
            return None
        self._processed_event_ids.add(event_id)
        status, verdict = self._coordinator.accept(intent)
        self._record_fixture_signal(event_id, intent, status, verdict.reasons)
        self._record_fixture_run(intent, status)
        self._last_event_id = event_id
        self._last_intent_id = intent.intent_id
        self._last_status = status
        return status, verdict

    def _record_fixture_signal(
        self,
        event_id: str,
        intent: Intent,
        status: Status,
        reasons: tuple[str, ...],
    ) -> None:
        """Link the event to an existing immutable intent without dispatching."""
        self._coordinator.store.record_signal(StrategySignal(
            signal_id=str(uuid5(
                NAMESPACE_URL, f"signal:{self.strategy_id}:{self.version}:{event_id}",
            )),
            strategy_id=self.strategy_id,
            run_id=intent.run_id or f"fixture:{event_id}",
            event_id=event_id,
            signal_type="REFERENCE_SIGNAL",
            symbol=intent.symbol,
            side=intent.side,
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            status=status,
            risk_reasons=reasons,
            observed_at=intent.created_at,
        ))

    def _record_fixture_run(self, intent: Intent, status: Status) -> None:
        """Write test-harness lifecycle evidence; this never enables a runner."""
        run_id = intent.run_id
        if run_id is None:
            raise ValueError("fixture intent must have a run id")
        self._coordinator.store.record_strategy_run(StrategyRun(
            run_id=run_id,
            strategy_id=self.strategy_id,
            version=self.version,
            config_checksum=self.config_checksum,
            state="FIXTURE_COMPLETED",
            last_intent_id=intent.intent_id,
            last_status=status,
            intent_count=1,
            started_at=intent.created_at,
            heartbeat_at=intent.created_at,
            ended_at=intent.created_at,
            updated_at=intent.created_at,
        ))

    def _fixture_intent(self, event_id: str, created_at: datetime) -> Intent:
        return Intent(
            source=Source.STRATEGY,
            account_id=self.reference_account_id,
            symbol="2330",
            side="BUY",
            quantity=1,
            limit_price="1000",
            strategy_id=self.strategy_id,
            run_id=f"fixture:{event_id}",
            intent_id=str(uuid5(NAMESPACE_URL, f"{self.strategy_id}:{self.version}:{event_id}")),
            correlation_id=str(uuid5(NAMESPACE_URL, f"correlation:{event_id}")),
            created_at=created_at,
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {
                    "contract": {"code": "2330"},
                    "stock_order": {
                        "action": "Buy", "price": 1000, "quantity": 1,
                        "account": {"account_id": self.reference_account_id, "broker_id": self.reference_broker_id},
                    },
                },
            },
        )

    @staticmethod
    def _parse_observed_at(value: object) -> datetime:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            except ValueError:
                pass
        return utc_now()
