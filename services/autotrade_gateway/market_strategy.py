"""A configurable, snapshot-driven strategy with a durable run identity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from .order_core import Gate, Intent, RiskContext, Source, StrategyDefinition
from .strategy_runtime import StrategyRuntime


@dataclass(frozen=True)
class StrategyConfig:
    symbol: str = "2330"
    side: str = "BUY"
    quantity: int = 1  # shares, always IntradayOdd
    threshold: str = "1000"
    condition: str = "AT_OR_BELOW"
    max_signals: int = 1

    @classmethod
    def parse(cls, raw: object, allowed: tuple[str, ...]) -> StrategyConfig:
        if not isinstance(raw, dict) or set(raw) != set(asdict(cls())):
            raise ValueError("請提供商品、方向、股數、門檻、條件與最大訊號數")
        if raw["symbol"] not in allowed or raw["side"] not in {"BUY", "SELL"}:
            raise ValueError("商品或買賣方向不在允許範圍")
        if raw["condition"] not in {"AT_OR_BELOW", "AT_OR_ABOVE"}:
            raise ValueError("價格條件無效")
        for name, maximum in (("quantity", 999), ("max_signals", 100)):
            if type(raw[name]) is not int or not 1 <= raw[name] <= maximum:
                raise ValueError(f"{name} 超出允許範圍")
        try:
            threshold = Decimal(str(raw["threshold"]))
            if not threshold.is_finite() or threshold <= 0:
                raise ValueError("價格門檻必須為正數")
        except InvalidOperation as error:
            raise ValueError("價格門檻無效") from error
        return cls(
            str(raw["symbol"]),
            str(raw["side"]),
            raw["quantity"],
            str(threshold),
            str(raw["condition"]),
            raw["max_signals"],
        )


class MarketStrategyRuntime(StrategyRuntime):
    strategy_id = "price-threshold"
    signal_type = "PRICE_THRESHOLD"
    execution_mode = "OBSERVE"
    version = "v1"
    allowed_symbols = ("2330", "2317", "0050")

    def __init__(self, coordinator, *, account_id: str, broker_id: str) -> None:
        self.config = StrategyConfig()
        self.last_market_at: str | None = None
        self.last_error: str | None = None
        super().__init__(
            coordinator,
            allow_lifecycle=True,
            reference_account_id=account_id,
            reference_broker_id=broker_id,
        )
        events = coordinator.store.observation_audit_events()
        saved = next(
            (
                event["details"]
                for event in events
                if event["event_type"] == "STRATEGY_CONFIGURED"
                and event["details"].get("strategy_id") == self.strategy_id
            ),
            None,
        )
        if saved:
            self.config = StrategyConfig.parse(saved["config"], self.allowed_symbols)
        self._set_version()
        # Restore history, never automatic execution authority after restart.
        if self._active_run_id:
            coordinator.store.transition_strategy_lifecycle(
                self._active_run_id,
                self.strategy_id,
                "PAUSED",
                False,
                datetime.now(timezone.utc),
            )

    def _set_version(self) -> None:
        self.config_checksum = sha256(
            json.dumps(asdict(self.config), sort_keys=True).encode()
        ).hexdigest()
        self.version = f"v1-{self.config_checksum[:12]}"
        self._coordinator.store.record_strategy_definition(
            StrategyDefinition(
                self.strategy_id,
                self.version,
                self.config_checksum,
                "DISABLED",
                False,
                self.allowed_symbols,
            )
        )

    def configure(self, raw: object) -> dict[str, object]:
        with self._coordinator.lock:
            if self._active_run_id:
                raise ValueError("請先停止目前策略，再修改設定")
            config = StrategyConfig.parse(raw, self.allowed_symbols)
            self._coordinator.store.record_audit_event(
                None,
                "STRATEGY_CONFIGURED",
                {
                    "strategy_id": self.strategy_id,
                    "config": asdict(config),
                },
            )
            self.config = config
            self._set_version()
            return asdict(config)

    def observations(self) -> list[dict[str, object]]:
        rows = [
            row for row in super().observations() if row.get("version") == self.version
        ]
        return [
            {
                **row,
                "version": self.version,
                "config_checksum": self.config_checksum,
                "config": asdict(self.config),
                "display_name": "價格門檻策略",
                "last_market_at": self.last_market_at,
                "last_error": self.last_error,
                "market_driven": True,
            }
            for row in rows
        ]

    def control(self, action: str) -> dict[str, object]:
        with self._coordinator.lock:
            if action in {"start", "resume"}:
                context = self._context()
                if context is None or not self._fresh(context):
                    raise ValueError("行情或帳務資料尚未就緒，無法啟動策略")
            return super().control(action)

    def _context(self) -> RiskContext | None:
        provider = self._coordinator.risk_context_provider
        return (
            provider(
                self._market_intent(
                    "preview", datetime.now(timezone.utc), self.config.threshold
                )
            )
            if provider
            else None
        )

    def _fresh(self, context: RiskContext) -> bool:
        return (
            context.quote_at is not None
            and context.quote_at.tzinfo is not None
            and -1
            <= (datetime.now(timezone.utc) - context.quote_at).total_seconds()
            <= self._coordinator.risk.limits.max_quote_age_seconds
            and context.quote_price is not None
            and context.quote_price.is_finite()
            and context.quote_price > 0
            and context.realized_pnl is not None
            and context.realized_pnl.is_finite()
            and context.unrealized_pnl is not None
            and context.unrealized_pnl.is_finite()
        )

    def validate_document(self) -> dict[str, object]:
        try:
            StrategyConfig.parse(asdict(self.config), self.allowed_symbols)
            return {
                "mode": "VALIDATE",
                "valid": True,
                "strategy": self.observations()[0],
                "warnings": [],
            }
        except ValueError as error:
            return {"mode": "VALIDATE", "valid": False, "warnings": [str(error)]}

    def dry_run_document(self) -> dict[str, object]:
        context = self._context()
        price = (
            str(context.quote_price)
            if context and context.quote_price
            else self.config.threshold
        )
        intent = self._market_intent("preview", datetime.now(timezone.utc), price)
        verdict = self._coordinator.risk.evaluate(
            intent, self._coordinator.gate, context
        )
        return {
            "mode": "DRY_RUN",
            "would_persist": False,
            "would_create_outbox": False,
            "would_dispatch_broker": False,
            "market_ready": bool(context and self._fresh(context)),
            "condition_met": bool(
                context and context.quote_price and self._matches(context.quote_price)
            ),
            "intent": {
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "limit_price": intent.limit_price,
                "source": intent.source.value,
            },
            "risk": {"allowed": verdict.allowed, "reasons": list(verdict.reasons)},
        }

    def _matches(self, price: Decimal) -> bool:
        threshold = Decimal(self.config.threshold)
        return (
            price <= threshold
            if self.config.condition == "AT_OR_BELOW"
            else price >= threshold
        )

    def on_tick(self) -> RiskContext:
        """Evaluate one market snapshot; return the account facts it used."""
        context = self._context()
        if context is None:
            raise ValueError("market/account snapshot unavailable")
        self.on_market(context)
        return context

    def on_market(self, context: RiskContext) -> None:
        with self._coordinator.lock:
            if (
                self._active_run_id is None
                or self.observations()[0].get("state") != "RUNNING"
            ):
                return
            if not self._fresh(context):
                self.last_error = "行情或帳務資料過期，停止產生新訊號"
                return
            assert context.quote_at is not None and context.quote_price is not None
            self.last_market_at = context.quote_at.isoformat()
            self.last_error = None
            self._coordinator.store.record_strategy_heartbeat(
                self._active_run_id, datetime.now(timezone.utc)
            )
            if self._coordinator.gate is not Gate.ARMED:
                return
            signals = [
                signal
                for signal in self._coordinator.store.observation_signals()
                if signal.get("run_id") == self._active_run_id
            ]
            if len(signals) >= self.config.max_signals or not self._matches(
                context.quote_price
            ):
                return
            event_id = f"{self._active_run_id}:{self.last_market_at}"
            intent = self._market_intent(
                event_id, context.quote_at, str(context.quote_price)
            )
            if self._coordinator.store.has_intent(intent.intent_id):
                return
            status, verdict = self._coordinator.accept(intent)
            self._record_fixture_signal(event_id, intent, status, verdict.reasons)
            self._record_fixture_run(intent, status)
            self._last_intent_id, self._last_status = intent.intent_id, status

    def _market_intent(self, event_id: str, timestamp: datetime, price: str) -> Intent:
        return Intent(
            Source.STRATEGY,
            self.reference_account_id,
            self.config.symbol,
            self.config.side,
            self.config.quantity,
            price,
            self.strategy_id,
            self._active_run_id or "preview",
            intent_id=str(
                uuid5(NAMESPACE_URL, f"{self.strategy_id}:{self.version}:{event_id}")
            ),
            created_at=timestamp,
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {
                    "contract": {
                        "code": self.config.symbol,
                        "security_type": "STK",
                        "exchange": "TSE",
                        "region": "TW",
                    },
                    "stock_order": {
                        "action": self.config.side.title(),
                        "price": float(price),
                        "quantity": self.config.quantity,
                        "price_type": "LMT",
                        "order_type": "ROD",
                        "order_lot": "IntradayOdd",
                        "order_cond": "Cash",
                        "account": {
                            "account_id": self.reference_account_id,
                            "broker_id": self.reference_broker_id,
                            "account_type": "S",
                        },
                    },
                },
            },
        )
