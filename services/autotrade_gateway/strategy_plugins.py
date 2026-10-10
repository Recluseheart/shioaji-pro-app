"""Operator-owned strategies loaded from outside the repository.

Strategy *logic* is private: it lives in ``AUTOTRADE_PRIVATE_STRATEGY_DIR``,
which must not be inside any git work tree, and is never committed. A plugin
receives only read-only market/position views and returns order proposals;
the Gateway turns each proposal into an immutable intent that still passes
the shared Risk Engine, durable outbox and broker readback chain.

A plugin module is ``<strategy_id with '-' as '_'>.py`` exporting ``STRATEGY``
that satisfies :class:`StrategyPlugin`.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Protocol, cast
from uuid import NAMESPACE_URL, uuid5

from .order_core import (
    Gate,
    Intent,
    OrderCoordinator,
    RiskContext,
    Source,
    StrategyDefinition,
    futures_root,
)
from .simulation_snapshot import ContractRef
from .strategy_runtime import StrategyRuntime

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MarketView:
    """The latest trade and best quotes of one configured symbol."""

    symbol: str
    last_price: Decimal
    last_trade_at: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None


@dataclass(frozen=True)
class PositionView:
    """Signed holding: shares for stocks/CBs, contracts for futures."""

    symbol: str
    units: int


@dataclass(frozen=True)
class OrderProposal:
    """A strategy's request; quantity is in broker units (張 / 股 / 口)."""

    symbol: str
    side: str
    quantity: int
    price: Decimal
    lot: str = "Common"
    reason: str = ""


class StrategyPlugin(Protocol):
    strategy_id: str
    display_name: str
    version: str
    config_fields: tuple[dict[str, object], ...]

    def default_config(self) -> dict[str, object]: ...

    def parse_config(self, raw: object) -> dict[str, object]: ...

    def symbols(self, config: Mapping[str, object]) -> tuple[str, ...]: ...

    def max_signals(self, config: Mapping[str, object]) -> int: ...

    def decide(
        self,
        config: Mapping[str, object],
        markets: Mapping[str, MarketView],
        positions: Mapping[str, PositionView],
    ) -> list[OrderProposal]: ...


class ContractResolver(Protocol):
    account_id: str
    broker_id: str
    futures_account_id: str | None

    def broker_id_for(self, account_id: str) -> str | None: ...

    def resolve_contract(self, symbol: str) -> ContractRef | None: ...


def _inside_git_work_tree(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def load_private_strategy(
    directory: str | Path,
    strategy_id: str,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> StrategyPlugin:
    """Import one named private strategy; refuse any location git could track."""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("private strategy directory does not exist")
    repository = repository_root.resolve()
    if root == repository or repository in root.parents or _inside_git_work_tree(root):
        raise ValueError("private strategies must live outside every git work tree")
    if not strategy_id or not strategy_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid strategy id")
    path = root / f"{strategy_id.replace('-', '_')}.py"
    if not path.is_file():
        raise ValueError("private strategy module not found")
    spec = importlib.util.spec_from_file_location(f"autotrade_private_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError("private strategy module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = getattr(module, "STRATEGY", None)
    if getattr(plugin, "strategy_id", None) != strategy_id:
        raise ValueError("private strategy module does not export the requested STRATEGY")
    for name in ("display_name", "version", "config_fields", "default_config", "parse_config",
                 "symbols", "max_signals", "decide"):
        if not hasattr(plugin, name):
            raise ValueError(f"private strategy is missing {name}")
    return cast(StrategyPlugin, plugin)


class PluginStrategyRuntime(StrategyRuntime):
    """Lifecycle, persistence and intent mapping around a private plugin."""

    signal_type = "PLUGIN_SIGNAL"
    execution_mode = "OBSERVE"
    market_driven = True

    def __init__(
        self,
        coordinator: OrderCoordinator,
        plugin: StrategyPlugin,
        resolver: ContractResolver,
    ) -> None:
        self.plugin = plugin
        self.resolver = resolver
        self.strategy_id = plugin.strategy_id
        self.config: dict[str, object] = plugin.parse_config(plugin.default_config())
        self.allowed_symbols = plugin.symbols(self.config)
        self.last_market_at: str | None = None
        self.last_error: str | None = None
        self._coordinator = coordinator
        saved = next(
            (
                details
                for event in coordinator.store.observation_audit_events()
                if event["event_type"] == "STRATEGY_CONFIGURED"
                and isinstance(details := event["details"], dict)
                and details.get("strategy_id") == self.strategy_id
            ),
            None,
        )
        if isinstance(saved, dict):
            try:
                self.config = plugin.parse_config(saved["config"])
            except ValueError:
                # A config the current plugin no longer accepts is ignored, not
                # silently reinterpreted; the operator must save it again.
                self.last_error = "已儲存的策略設定與目前策略版本不相容，已改用預設值"
        self._set_version()
        super().__init__(
            coordinator,
            allow_lifecycle=True,
            reference_account_id=resolver.account_id,
            reference_broker_id=resolver.broker_id,
        )
        # Restore history, never automatic execution authority after restart.
        if self._active_run_id:
            coordinator.store.transition_strategy_lifecycle(
                self._active_run_id, self.strategy_id, "PAUSED", False, datetime.now(timezone.utc),
            )

    def _set_version(self) -> None:
        self.allowed_symbols = self.plugin.symbols(self.config)
        self.config_checksum = sha256(
            json.dumps(self.config, sort_keys=True, default=str).encode()
        ).hexdigest()
        self.version = f"{self.plugin.version}-{self.config_checksum[:12]}"
        self._coordinator.store.record_strategy_definition(StrategyDefinition(
            self.strategy_id, self.version, self.config_checksum, "DISABLED", False, self.allowed_symbols,
        ))

    # ----- configuration -------------------------------------------------

    def configure(self, raw: object) -> dict[str, object]:
        with self._coordinator.lock:
            if self._active_run_id:
                raise ValueError("請先停止目前策略，再修改設定")
            config = self.plugin.parse_config(raw)
            for symbol in self.plugin.symbols(config):
                if self.resolver.resolve_contract(symbol) is None:
                    raise ValueError(f"找不到商品 {symbol} 的合約")
                if not self._symbol_allowed(symbol):
                    raise ValueError(f"商品 {symbol} 不在風控允許清單")
            self._coordinator.store.record_audit_event(
                None, "STRATEGY_CONFIGURED", {"strategy_id": self.strategy_id, "config": config},
            )
            self.config = config
            self._set_version()
            return dict(config)

    def _symbol_allowed(self, symbol: str) -> bool:
        limits = self._coordinator.risk.limits
        contract = self.resolver.resolve_contract(symbol)
        if contract is not None and contract.security_type == "FUT":
            return futures_root(symbol) in limits.allowed_futures_roots
        return symbol in limits.allowed_symbols

    def observations(self) -> list[dict[str, object]]:
        rows = [row for row in super().observations() if row.get("version") == self.version]
        return [
            {
                **row,
                "version": self.version,
                "config_checksum": self.config_checksum,
                "config": dict(self.config),
                "config_schema": list(self.plugin.config_fields),
                "display_name": self.plugin.display_name,
                "last_market_at": self.last_market_at,
                "last_error": self.last_error,
                "market_driven": True,
                "private_strategy": True,
            }
            for row in rows
        ]

    def validate_document(self) -> dict[str, object]:
        try:
            self.plugin.parse_config(self.config)
            warnings = [] if all(self._symbol_allowed(s) for s in self.allowed_symbols) else ["symbol_not_allowed"]
            return {"mode": "VALIDATE", "valid": not warnings, "strategy": self.observations()[0], "warnings": warnings}
        except ValueError as error:
            return {"mode": "VALIDATE", "valid": False, "warnings": [str(error)]}

    # ----- market facts --------------------------------------------------

    def _preview_intent(self, symbol: str) -> Intent | None:
        contract = self.resolver.resolve_contract(symbol)
        if contract is None:
            return None
        proposal = OrderProposal(symbol, "BUY", 1, Decimal("1"))
        return self._intent(proposal, contract, "preview", datetime.now(timezone.utc))

    def _contexts(self) -> dict[str, RiskContext]:
        provider = self._coordinator.risk_context_provider
        contexts: dict[str, RiskContext] = {}
        if provider is None:
            return contexts
        for symbol in self.allowed_symbols:
            intent = self._preview_intent(symbol)
            context = provider(intent) if intent else None
            if context is not None:
                contexts[symbol] = context
        return contexts

    def _context(self) -> RiskContext | None:
        """Account/market facts for arming and PnL snapshots (first symbol)."""
        contexts = self._contexts()
        return next(iter(contexts.values()), None)

    def _fresh(self, context: RiskContext) -> bool:
        """Readiness means facts are available; trade recency is checked per signal.

        Convertible bonds may not print for minutes. Each signal still requires
        a trade within the Risk Engine's age limit for that symbol.
        """
        return (
            context.quote_price is not None
            and context.quote_price.is_finite()
            and context.quote_price > 0
            and context.quote_at is not None
            and context.realized_pnl is not None
            and context.realized_pnl.is_finite()
            and context.unrealized_pnl is not None
            and context.unrealized_pnl.is_finite()
        )

    def _views(self) -> tuple[dict[str, MarketView], dict[str, PositionView], dict[str, RiskContext]]:
        limits = self._coordinator.risk.limits
        now = datetime.now(timezone.utc)
        markets: dict[str, MarketView] = {}
        positions: dict[str, PositionView] = {}
        contexts = self._contexts()
        for symbol, context in contexts.items():
            contract = self.resolver.resolve_contract(symbol)
            key = (futures_root(symbol) or symbol) if contract and contract.security_type == "FUT" else symbol
            held = context.positions.get(key)
            positions[symbol] = PositionView(symbol, held.quantity if held else 0)
            max_age = (
                limits.slow_quote_max_age_seconds if symbol in limits.slow_quote_symbols
                else limits.max_quote_age_seconds
            )
            if (
                self._fresh(context)
                and context.quote_at is not None
                and -1 <= (now - context.quote_at).total_seconds() <= max_age
            ):
                assert context.quote_price is not None
                markets[symbol] = MarketView(
                    symbol, context.quote_price, context.quote_at, context.quote_bid, context.quote_ask,
                )
        return markets, positions, contexts

    def control(self, action: str) -> dict[str, object]:
        with self._coordinator.lock:
            if action in {"start", "resume"}:
                contexts = self._contexts()
                missing = [s for s in self.allowed_symbols if s not in contexts or not self._fresh(contexts[s])]
                if missing:
                    raise ValueError(f"行情或帳務資料尚未就緒：{'、'.join(missing)}")
            return super().control(action)

    # ----- signals -------------------------------------------------------

    def dry_run_document(self) -> dict[str, object]:
        markets, positions, contexts = self._views()
        try:
            proposals = self.plugin.decide(self.config, markets, positions)
        except Exception as error:  # noqa: BLE001 - private code must not crash the Gateway
            return {"mode": "DRY_RUN", "would_persist": False, "would_create_outbox": False,
                    "would_dispatch_broker": False, "risk": {"allowed": False, "reasons": ["strategy_error"]},
                    "error": type(error).__name__}
        results = []
        for proposal in proposals:
            contract = self.resolver.resolve_contract(proposal.symbol)
            if contract is None or proposal.symbol not in self.allowed_symbols:
                results.append({"proposal": self._proposal_document(proposal),
                                "risk": {"allowed": False, "reasons": ["symbol_not_configured"]}})
                continue
            intent = self._intent(proposal, contract, "preview", datetime.now(timezone.utc))
            verdict = self._coordinator.risk.evaluate(intent, self._coordinator.gate, contexts.get(proposal.symbol))
            results.append({"proposal": self._proposal_document(proposal),
                            "risk": {"allowed": verdict.allowed, "reasons": list(verdict.reasons)}})
        first = results[0]["risk"] if results else {"allowed": False, "reasons": ["no_signal_condition"]}
        return {
            "mode": "DRY_RUN",
            "would_persist": False,
            "would_create_outbox": False,
            "would_dispatch_broker": False,
            "market_ready": bool(markets),
            "condition_met": bool(proposals),
            "markets": {s: {"last_price": str(m.last_price), "last_trade_at": m.last_trade_at.isoformat()}
                        for s, m in markets.items()},
            "positions": {s: p.units for s, p in positions.items()},
            "proposals": results,
            "risk": first,
        }

    @staticmethod
    def _proposal_document(proposal: OrderProposal) -> dict[str, object]:
        return {"symbol": proposal.symbol, "side": proposal.side, "quantity": proposal.quantity,
                "price": str(proposal.price), "lot": proposal.lot, "reason": proposal.reason}

    def on_tick(self) -> RiskContext:
        """Evaluate the plugin once; at most one new intent per tick."""
        with self._coordinator.lock:
            markets, positions, contexts = self._views()
            account_context = next(iter(contexts.values()), None)
            if account_context is None:
                raise ValueError("market/account snapshot unavailable")
            if self._active_run_id is None or self.observations()[0].get("state") != "RUNNING":
                return account_context
            if not markets:
                self.last_error = "等待新成交（行情超過時效，暫不產生訊號）"
            else:
                latest = max(market.last_trade_at for market in markets.values())
                self.last_market_at = latest.isoformat()
                self.last_error = None
            self._coordinator.store.record_strategy_heartbeat(self._active_run_id, datetime.now(timezone.utc))
            if self._coordinator.gate is not Gate.ARMED or not markets:
                return account_context
            signals = [s for s in self._coordinator.store.observation_signals()
                       if s.get("run_id") == self._active_run_id]
            if len(signals) >= self.plugin.max_signals(self.config):
                return account_context
            try:
                proposals = self.plugin.decide(self.config, markets, positions)
            except Exception as error:  # noqa: BLE001 - isolate private code failures
                self.last_error = f"策略程式錯誤：{type(error).__name__}；已暫停"
                self.control("pause")
                return account_context
            for proposal in proposals[:1]:
                self._submit(proposal, markets)
            return account_context

    def _submit(self, proposal: OrderProposal, markets: Mapping[str, MarketView]) -> None:
        market = markets.get(proposal.symbol)
        contract = self.resolver.resolve_contract(proposal.symbol)
        if (
            market is None
            or contract is None
            or proposal.symbol not in self.allowed_symbols
            or proposal.side not in {"BUY", "SELL"}
            or type(proposal.quantity) is not int
            or proposal.quantity <= 0
        ):
            self.last_error = "策略提出無效委託，已略過"
            return
        try:
            price = Decimal(str(proposal.price))
        except (InvalidOperation, ValueError):
            self.last_error = "策略提出無效價格，已略過"
            return
        if not price.is_finite() or price <= 0:
            self.last_error = "策略提出無效價格，已略過"
            return
        # One signal per trade print and side; the intent time is the real
        # creation time so rate limits and cooldowns see it correctly.
        event_id = f"{self._active_run_id}:{proposal.symbol}:{proposal.side}:{market.last_trade_at.isoformat()}"
        intent = self._intent(proposal, contract, event_id, datetime.now(timezone.utc))
        if self._coordinator.store.has_intent(intent.intent_id):
            return
        status, verdict = self._coordinator.accept(intent)
        self._record_fixture_signal(event_id, intent, status, verdict.reasons)
        self._record_fixture_run(intent, status)
        self._last_event_id, self._last_intent_id, self._last_status = event_id, intent.intent_id, status

    def _intent(self, proposal: OrderProposal, contract: ContractRef, event_id: str, created_at: datetime) -> Intent:
        futures = contract.security_type == "FUT"
        # Rolling futures aliases are placed on the month contract they resolve to.
        code = contract.target_code if futures and contract.target_code else contract.code
        account_id = (self.resolver.futures_account_id or "") if futures else self.resolver.account_id
        account = {
            "account_id": account_id,
            "broker_id": self.resolver.broker_id_for(account_id) or "",
            "account_type": "F" if futures else "S",
        }
        wire = {**ContractRef(contract.security_type, contract.exchange, code).wire()}
        price = float(proposal.price)
        order: dict[str, object] = {
            "action": proposal.side.title(),
            "price": price,
            "quantity": proposal.quantity,
            "price_type": "LMT",
            "order_type": "ROD",
            "account": account,
        }
        if futures:
            order["octype"] = "Auto"
        else:
            order.update(order_lot=proposal.lot, order_cond="Cash")
        return Intent(
            Source.STRATEGY,
            account_id,
            code,
            proposal.side,
            proposal.quantity,
            str(proposal.price),
            self.strategy_id,
            self._active_run_id or "preview",
            intent_id=str(uuid5(NAMESPACE_URL, f"{self.strategy_id}:{self.version}:{event_id}")),
            created_at=created_at,
            broker_request={
                "path": "/api/v1/order/place_order",
                "body": {"contract": wire, ("futures_order" if futures else "stock_order"): order},
            },
        )
