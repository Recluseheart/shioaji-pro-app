"""Broker-free strategy ingress migrated from the legacy reference runner.

The runtime deliberately has no market subscription and no HTTP control route.
It records a disabled reference fixture so the Web UI can show an honest
strategy lifecycle before a separately-authorized execution worker is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .order_core import Intent, OrderCoordinator, Source, Status, Verdict


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StrategyObservation:
    strategy_id: str
    version: str
    config_checksum: str
    state: str
    enabled: bool
    allowed_symbols: tuple[str, ...]
    last_event_id: str | None = None
    last_intent_id: str | None = None
    last_status: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "config_checksum": self.config_checksum,
            "state": self.state,
            "enabled": self.enabled,
            "allowed_symbols": list(self.allowed_symbols),
            "last_event_id": self.last_event_id,
            "last_intent_id": self.last_intent_id,
            "last_status": self.last_status,
        }


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

    def __init__(self, coordinator: OrderCoordinator, *, allow_reference_fixture: bool = False) -> None:
        self._coordinator = coordinator
        self._allow_reference_fixture = allow_reference_fixture
        self._processed_event_ids: set[str] = set()
        self._last_event_id: str | None = None
        self._last_intent_id: str | None = None
        self._last_status: Status | None = None

    def observations(self) -> list[dict[str, object]]:
        return [
            StrategyObservation(
                strategy_id=self.strategy_id,
                version=self.version,
                config_checksum=self.config_checksum,
                state="DISABLED",
                enabled=False,
                allowed_symbols=self.allowed_symbols,
                last_event_id=self._last_event_id,
                last_intent_id=self._last_intent_id,
                last_status=self._last_status.value if self._last_status else None,
            ).document()
        ]

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
        self._processed_event_ids.add(event_id)
        intent = Intent(
            source=Source.STRATEGY,
            account_id="SIM-001",
            symbol="2330",
            side="BUY",
            quantity=1,
            limit_price="1000",
            strategy_id=self.strategy_id,
            run_id=f"fixture:{event_id}",
            intent_id=str(uuid5(NAMESPACE_URL, f"{self.strategy_id}:{self.version}:{event_id}")),
            correlation_id=str(uuid5(NAMESPACE_URL, f"correlation:{event_id}")),
            created_at=self._parse_observed_at(event.get("observed_at")),
        )
        status, verdict = self._coordinator.accept(intent)
        self._last_event_id = event_id
        self._last_intent_id = intent.intent_id
        self._last_status = status
        return status, verdict

    @staticmethod
    def _parse_observed_at(value: object) -> datetime:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            except ValueError:
                pass
        return utc_now()
