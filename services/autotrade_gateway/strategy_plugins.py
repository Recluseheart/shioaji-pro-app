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
from zoneinfo import ZoneInfo

from .order_core import (
    Gate,
    Intent,
    OrderCoordinator,
    RiskContext,
    Source,
    StrategyDefinition,
    futures_root,
    instrument_profile,
)
from .simulation_snapshot import ContractRef
from .strategy_runtime import StrategyRuntime

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TAIPEI = ZoneInfo("Asia/Taipei")


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
class DailyActivity:
    """This strategy's orders today (Asia/Taipei) for one symbol, in units.

    Rejected orders do not count; cancelled or broker-rejected orders count
    only what was filled. Read from the durable journal, so it survives restarts.
    """

    symbol: str
    buy_units: int = 0
    sell_units: int = 0


@dataclass(frozen=True)
class OrderProposal:
    """A strategy's request.

    ``lot`` decides the quantity unit for stocks:

    - ``AUTO`` (default): quantity in shares. 999 or fewer trade as intraday
      odd lots; 1,000 or more trade as board lots, and a non-multiple is split
      into a board-lot order plus an odd-lot order (1,500 = 1 張 + 500 股).
      Symbols without an odd-lot market (CBs) must be whole multiples of 1,000.
    - ``Common``: quantity in board lots (張).
    - ``IntradayOdd``: quantity in shares, 1-999.

    Futures quantities are always contracts (口) and ignore ``lot``.
    """

    symbol: str
    side: str
    quantity: int
    price: Decimal
    lot: str = "AUTO"
    reason: str = ""


@dataclass(frozen=True)
class OrderPart:
    lot: str
    quantity: int


BOARD_LOT_SHARES = 1000


def stock_order_parts(shares: int) -> tuple[OrderPart, ...]:
    """Board lots first, then the odd remainder (1,500 -> 1 張 + 500 股)."""
    if type(shares) is not int or shares <= 0:
        raise ValueError("股數必須為正整數")
    lots, rest = divmod(shares, BOARD_LOT_SHARES)
    return tuple(
        part for part in (OrderPart("Common", lots), OrderPart("IntradayOdd", rest)) if part.quantity
    )


TERMINAL_UNFILLED = {"CANCELLED", "BROKER_REJECTED"}
WAITING = {"APPROVED", "SENDING", "PENDING_SUBMIT", "SUBMITTED", "PART_FILLED"}


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
        activity: Mapping[str, DailyActivity],
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
        # (proposal, contract, odd-lot part, event id, board-lot intent id)
        self._queued_part: tuple[OrderProposal, ContractRef, OrderPart, str, str] | None = None
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
        proposal = OrderProposal(symbol, "BUY", 1, Decimal("1"), lot="Common")
        return self._intent(proposal, contract, "preview", datetime.now(timezone.utc), OrderPart("Common", 1))

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
            if action == "stop":
                self._queued_part = None
            return super().control(action)

    def _activity(self) -> dict[str, DailyActivity]:
        """Today's (Asia/Taipei) units per configured symbol from the journal."""
        store = self._coordinator.store
        today = datetime.now(TAIPEI).date()
        aliases: dict[str, str] = {}
        for symbol in self.allowed_symbols:
            aliases[symbol] = symbol
            contract = self.resolver.resolve_contract(symbol)
            if contract is not None and contract.target_code:
                aliases[contract.target_code] = symbol
        filled: dict[str, int] = {}
        for fill in store.observation_fills():
            key = str(fill["intent_id"])
            filled[key] = filled.get(key, 0) + int(str(fill["quantity"]))
        totals = {symbol: [0, 0] for symbol in self.allowed_symbols}
        for row in store.observation_orders():
            status = str(row.get("status"))
            if row.get("strategy_id") != self.strategy_id or status == "RISK_REJECTED":
                continue
            intent = store.load_intent(str(row["intent_id"]))
            configured = aliases.get(intent.symbol)
            if (
                configured is None
                or intent.operation != "PLACE"
                or intent.created_at.astimezone(TAIPEI).date() != today
            ):
                continue
            try:
                units = instrument_profile(intent, self._coordinator.risk.limits).units
            except ValueError:
                continue
            if status in TERMINAL_UNFILLED:
                units = filled.get(intent.intent_id, 0)
            totals[configured][0 if intent.side == "BUY" else 1] += units
        return {symbol: DailyActivity(symbol, buy, sell) for symbol, (buy, sell) in totals.items()}

    def _parts(self, proposal: OrderProposal, contract: ContractRef) -> tuple[OrderPart, ...]:
        """Apply the board-lot / odd-lot rule; raise with an operator-readable reason."""
        if type(proposal.quantity) is not int or proposal.quantity <= 0:
            raise ValueError("策略提出無效數量，已略過")
        if contract.security_type == "FUT":
            return (OrderPart("", proposal.quantity),)
        board_lot_only = proposal.symbol in self._coordinator.risk.limits.board_lot_only_symbols
        if proposal.lot == "Common":
            return (OrderPart("Common", proposal.quantity),)
        if proposal.lot == "IntradayOdd":
            if proposal.quantity > BOARD_LOT_SHARES - 1 or board_lot_only:
                raise ValueError("零股單筆須為 1-999 股，且 CB 不能下零股")
            return (OrderPart("IntradayOdd", proposal.quantity),)
        if proposal.lot == "AUTO":
            if board_lot_only:
                if proposal.quantity % BOARD_LOT_SHARES:
                    raise ValueError("CB 沒有零股，數量須為 1,000 的倍數")
                return (OrderPart("Common", proposal.quantity // BOARD_LOT_SHARES),)
            return stock_order_parts(proposal.quantity)
        raise ValueError("策略提出未知的整股／零股類型，已略過")

    # ----- signals -------------------------------------------------------

    def dry_run_document(self) -> dict[str, object]:
        markets, positions, contexts = self._views()
        try:
            proposals = self.plugin.decide(self.config, markets, positions, self._activity())
        except Exception as error:  # noqa: BLE001 - private code must not crash the Gateway
            return {"mode": "DRY_RUN", "would_persist": False, "would_create_outbox": False,
                    "would_dispatch_broker": False, "risk": {"allowed": False, "reasons": ["strategy_error"]},
                    "error": type(error).__name__}
        results: list[dict[str, object]] = []
        for proposal in proposals:
            contract = self.resolver.resolve_contract(proposal.symbol)
            if contract is None or proposal.symbol not in self.allowed_symbols:
                results.append({"proposal": self._proposal_document(proposal),
                                "risk": {"allowed": False, "reasons": ["symbol_not_configured"]}})
                continue
            try:
                parts = self._parts(proposal, contract)
            except ValueError:
                results.append({"proposal": self._proposal_document(proposal),
                                "risk": {"allowed": False, "reasons": ["invalid_quantity"]}})
                continue
            # Each part is evaluated alone; a later part is only sent after the
            # earlier one completes, so it will be re-evaluated with new facts.
            reasons: list[str] = []
            for part in parts:
                intent = self._intent(proposal, contract, "preview", datetime.now(timezone.utc), part)
                verdict = self._coordinator.risk.evaluate(intent, self._coordinator.gate, contexts.get(proposal.symbol))
                reasons.extend(reason for reason in verdict.reasons if reason not in reasons)
            results.append({"proposal": self._proposal_document(proposal),
                            "orders": [{"lot": part.lot, "quantity": part.quantity} for part in parts],
                            "risk": {"allowed": not reasons, "reasons": reasons}})
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
            if self._coordinator.gate is not Gate.ARMED:
                return account_context
            if self._queued_part is not None:
                # A split order is in progress; the strategy waits for it.
                self._advance_queued_part()
                return account_context
            if not markets:
                return account_context
            signals = [s for s in self._coordinator.store.observation_signals()
                       if s.get("run_id") == self._active_run_id]
            if len(signals) >= self.plugin.max_signals(self.config):
                return account_context
            try:
                proposals = self.plugin.decide(self.config, markets, positions, self._activity())
            except Exception as error:  # noqa: BLE001 - isolate private code failures
                self.last_error = f"策略程式錯誤：{type(error).__name__}；已暫停"
                self.control("pause")
                return account_context
            for proposal in proposals[:1]:
                self._submit(proposal, markets)
            return account_context

    def _advance_queued_part(self) -> None:
        """Send the odd-lot remainder only after the board-lot part fully filled.

        If the first part was rejected, cancelled or not fully filled, the
        remainder is dropped so a half-built order is never completed alone.
        The queue lives in memory: after a restart the remainder is not sent.
        """
        assert self._queued_part is not None
        proposal, contract, part, event_id, first_intent_id = self._queued_part
        status = self._coordinator.store.status(first_intent_id).value
        if status in WAITING:
            return
        if status != "FILLED":
            self._queued_part = None
            self.last_error = f"整股部分未完全成交（{status}），零股餘數 {part.quantity} 股不送出"
            # Durable, so the operator still sees why after the message changes.
            self._coordinator.store.record_audit_event(first_intent_id, "STRATEGY_ODD_REMAINDER_DROPPED", {
                "strategy_id": self.strategy_id, "board_lot_status": status,
                "odd_lot_shares": part.quantity, "symbol": proposal.symbol,
            })
            return
        self._queued_part = None
        self._accept_part(proposal, contract, part, event_id)

    def _submit(self, proposal: OrderProposal, markets: Mapping[str, MarketView]) -> None:
        market = markets.get(proposal.symbol)
        contract = self.resolver.resolve_contract(proposal.symbol)
        if (
            market is None
            or contract is None
            or proposal.symbol not in self.allowed_symbols
            or proposal.side not in {"BUY", "SELL"}
        ):
            self.last_error = "策略提出無效委託，已略過"
            return
        try:
            parts = self._parts(proposal, contract)
        except ValueError as error:
            self.last_error = str(error)
            return
        try:
            price = Decimal(str(proposal.price))
        except (InvalidOperation, ValueError):
            self.last_error = "策略提出無效價格，已略過"
            return
        if not price.is_finite() or price <= 0:
            self.last_error = "策略提出無效價格，已略過"
            return
        # One signal per trade print and side. A split order uses one event per
        # part; only one order may be outstanding per account, so the odd-lot
        # remainder waits until the board-lot part has completed.
        event_id = f"{self._active_run_id}:{proposal.symbol}:{proposal.side}:{market.last_trade_at.isoformat()}"
        if len(parts) == 1:
            self._accept_part(proposal, contract, parts[0], event_id)
            return
        first = self._accept_part(proposal, contract, parts[0], f"{event_id}:board")
        if first is not None and first[1] == "APPROVED":
            self._queued_part = (proposal, contract, parts[1], f"{event_id}:odd", first[0])

    def _accept_part(
        self, proposal: OrderProposal, contract: ContractRef, part: OrderPart, event_id: str,
    ) -> tuple[str, str] | None:
        # The intent time is the real creation time so rate limits and
        # cooldowns see it correctly.
        intent = self._intent(proposal, contract, event_id, datetime.now(timezone.utc), part)
        if self._coordinator.store.has_intent(intent.intent_id):
            return None
        status, verdict = self._coordinator.accept(intent)
        self._record_fixture_signal(event_id, intent, status, verdict.reasons)
        self._record_fixture_run(intent, status)
        self._last_event_id, self._last_intent_id, self._last_status = event_id, intent.intent_id, status
        return intent.intent_id, status.value

    def _intent(
        self,
        proposal: OrderProposal,
        contract: ContractRef,
        event_id: str,
        created_at: datetime,
        part: OrderPart,
    ) -> Intent:
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
            "quantity": part.quantity,
            "price_type": "LMT",
            "order_type": "ROD",
            "account": account,
        }
        if futures:
            order["octype"] = "Auto"
        else:
            order.update(order_lot=part.lot, order_cond="Cash")
        return Intent(
            Source.STRATEGY,
            account_id,
            code,
            proposal.side,
            part.quantity,
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
