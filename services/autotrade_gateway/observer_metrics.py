"""Read-only metrics for the Autotrade panels, derived from the durable journal.

Everything here is computed from facts the Gateway already persisted (intents,
fills, audit events, account snapshots). Nothing contacts a broker or changes
state. Strategy performance is an estimate (no fees or taxes); account-level
figures keep their broker or PAPER source label.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .order_core import IntentStore, RiskLimits, instrument_profile

TAIPEI = ZoneInfo("Asia/Taipei")
OPEN_STATUSES = frozenset({
    "APPROVED", "SENDING", "PENDING_SUBMIT", "SUBMITTED", "PART_FILLED", "UNKNOWN", "RECONCILING",
})
REJECTED_STATUSES = frozenset({"RISK_REJECTED", "BROKER_REJECTED"})


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _taipei_date(value: datetime) -> date:
    return value.astimezone(TAIPEI).date()


def today_document(orders: list[dict[str, object]], now: datetime | None = None) -> dict[str, object]:
    """Counts of today's (Asia/Taipei) new orders by source and outcome."""
    today = _taipei_date(now or datetime.now(timezone.utc))
    counts = {"auto_order_count": 0, "auto_rejected_count": 0, "auto_filled_count": 0,
              "manual_order_count": 0, "open_order_count": 0}
    for order in orders:
        if order.get("operation", "PLACE") != "PLACE":
            continue
        if order.get("status") in OPEN_STATUSES:
            counts["open_order_count"] += 1
        created = _parse_time(order.get("created_at"))
        if created is None or _taipei_date(created) != today:
            continue
        automatic = order.get("source") in {"STRATEGY", "SYSTEM_TRIGGER"}
        if not automatic:
            counts["manual_order_count"] += 1
            continue
        if order.get("status") in REJECTED_STATUSES:
            counts["auto_rejected_count"] += 1
        else:
            counts["auto_order_count"] += 1
        if isinstance(order.get("filled_quantity"), int) and int(str(order["filled_quantity"])) > 0:
            counts["auto_filled_count"] += 1
    return {"date": today.isoformat(), **counts}


def pending_exposure(store: IntentStore, orders: list[dict[str, object]], limits: RiskLimits) -> Decimal:
    """Value of the unfilled remainder of every open order (all accounts)."""
    filled: dict[str, int] = defaultdict(int)
    for fill in store.observation_fills():
        filled[str(fill["intent_id"])] += int(str(fill["quantity"]))
    total = Decimal("0")
    for order in orders:
        if order.get("status") not in OPEN_STATUSES or order.get("operation", "PLACE") != "PLACE":
            continue
        intent = store.load_intent(str(order["intent_id"]))
        price = _decimal(intent.limit_price)
        try:
            profile = instrument_profile(intent, limits)
        except ValueError:
            continue
        if price is None or price <= 0:
            continue
        remaining = max(0, profile.units - filled[intent.intent_id])
        total += Decimal(remaining) * price * profile.multiplier
    return total


def risk_usage_document(
    orders: list[dict[str, object]],
    fills: list[dict[str, object]],
    reconciliation: list[dict[str, object]],
    *,
    limits: RiskLimits | None = None,
    account: dict[str, object] | None = None,
    pending: Decimal = Decimal("0"),
    now: datetime | None = None,
) -> dict[str, object]:
    """Durable usage plus remaining headroom when a fresh account snapshot exists."""
    moment = now or datetime.now(timezone.utc)
    confirmed = [q for f in fills for q in [f.get("quantity")] if isinstance(q, int) and q > 0]
    recent = 0
    for order in orders:
        created = _parse_time(order.get("created_at"))
        if created is not None and order.get("status") not in REJECTED_STATUSES \
                and 0 <= (moment - created).total_seconds() < 60:
            recent += 1
    document: dict[str, object] = {
        "intent_count": len(orders),
        "confirmed_fill_count": len(confirmed),
        "confirmed_filled_quantity": sum(confirmed),
        "reconciliation_count": len(reconciliation),
        "orders_last_minute": recent,
        "pending_exposure": str(pending),
        "authoritative_remaining_available": False,
    }
    if limits is None or not account or account.get("stale") is not False:
        return document
    realized = _decimal(account.get("realized_pnl"))
    unrealized = _decimal(account.get("unrealized_pnl"))
    gross = _decimal(account.get("gross_exposure"))
    if realized is None or unrealized is None:
        return document
    loss_used = max(Decimal("0"), -(realized + unrealized))
    document.update(
        authoritative_remaining_available=True,
        daily_loss_used=str(loss_used),
        daily_loss_remaining=str(max(Decimal("0"), limits.max_daily_loss - loss_used)),
        orders_per_minute_remaining=max(0, limits.max_orders_per_minute - recent),
        source=account.get("source"),
        observed_at=account.get("observed_at"),
    )
    if gross is not None:
        document.update(
            gross_exposure=str(gross),
            gross_exposure_remaining=str(max(Decimal("0"), limits.max_gross_exposure - gross - pending)),
        )
    return document


def run_performance_document(store: IntentStore, limits: RiskLimits | None = None) -> list[dict[str, object]]:
    """Per-run estimates: trades, win rate, PnL, drawdown, reject rate, slippage, latency."""
    limits = limits or RiskLimits()
    orders_by_run: dict[str, list[dict[str, object]]] = defaultdict(list)
    for order in store.observation_orders():
        run_id = order.get("run_id")
        if isinstance(run_id, str) and run_id and order.get("operation", "PLACE") == "PLACE":
            orders_by_run[run_id].append(order)
    fills_by_intent: dict[str, list[dict[str, object]]] = defaultdict(list)
    for fill in store.observation_fills():
        fills_by_intent[str(fill["intent_id"])].append(fill)
    bound_at: dict[str, datetime] = {}
    for event in store.observation_audit_events():
        if event["event_type"] == "BROKER_ORDER_BOUND" and isinstance(event.get("intent_id"), str):
            occurred = _parse_time(event.get("occurred_at"))
            if occurred is not None:
                bound_at[str(event["intent_id"])] = occurred
    documents: list[dict[str, object]] = []
    for run in store.observation_strategy_runs():
        run_id = str(run["run_id"])
        orders = orders_by_run.get(run_id, [])
        executions: list[tuple[datetime, str, str, int, Decimal, Decimal]] = []
        slippages: list[Decimal] = []
        ack_ms: list[float] = []
        fill_ms: list[float] = []
        rejected = 0
        for order in orders:
            if order.get("status") in REJECTED_STATUSES:
                rejected += 1
            intent = store.load_intent(str(order["intent_id"]))
            try:
                multiplier = instrument_profile(intent, limits).multiplier
            except ValueError:
                multiplier = Decimal("1")
            limit = _decimal(intent.limit_price)
            if intent.intent_id in bound_at:
                ack_ms.append((bound_at[intent.intent_id] - intent.created_at).total_seconds() * 1000)
            fills = sorted(fills_by_intent.get(intent.intent_id, []), key=lambda f: str(f["filled_at"]))
            for index, fill in enumerate(fills):
                at = _parse_time(fill.get("filled_at"))
                price = _decimal(fill.get("price"))
                if at is None or price is None:
                    continue
                if index == 0:
                    fill_ms.append((at - intent.created_at).total_seconds() * 1000)
                if limit is not None and limit > 0:
                    worse = (price - limit) if intent.side == "BUY" else (limit - price)
                    slippages.append(worse / limit * Decimal("10000"))
                executions.append((at, intent.symbol, intent.side, int(str(fill["quantity"])), price, multiplier))
        executions.sort(key=lambda item: item[0])
        lots: dict[str, deque[list[Decimal]]] = defaultdict(deque)  # [signed units, price]
        realized = Decimal("0")
        peak = Decimal("0")
        drawdown = Decimal("0")
        wins = closed = 0
        for _, symbol, side, units, price, multiplier in executions:
            signed = Decimal(units if side == "BUY" else -units)
            book = lots[symbol]
            while signed and book and (book[0][0] > 0) != (signed > 0):
                take = min(abs(signed), abs(book[0][0]))
                direction = Decimal(1 if book[0][0] > 0 else -1)
                pnl = (price - book[0][1]) * take * direction * multiplier
                realized += pnl
                closed += 1
                wins += 1 if pnl > 0 else 0
                book[0][0] -= take * direction
                signed += take * direction
                if book[0][0] == 0:
                    book.popleft()
                peak = max(peak, realized)
                drawdown = max(drawdown, peak - realized)
            if signed:
                book.append([signed, price])
        documents.append({
            "run_id": run_id,
            "strategy_id": run.get("strategy_id"),
            "version": run.get("version"),
            "state": run.get("state"),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "heartbeat_at": run.get("heartbeat_at"),
            "intent_count": len(orders),
            "rejected_count": rejected,
            "reject_rate": round(rejected / len(orders), 4) if orders else None,
            "fill_count": len(executions),
            "closed_trade_count": closed,
            "win_rate": round(wins / closed, 4) if closed else None,
            "realized_pnl": str(realized),
            "max_drawdown": str(drawdown),
            "avg_slippage_bps": str(round(sum(slippages) / len(slippages), 2)) if slippages else None,
            "avg_ack_latency_ms": round(sum(ack_ms) / len(ack_ms), 1) if ack_ms else None,
            "avg_fill_latency_ms": round(sum(fill_ms) / len(fill_ms), 1) if fill_ms else None,
            "estimate": True,
        })
    return documents


def reconciliation_notes(store: IntentStore) -> dict[str, dict[str, object]]:
    """Latest operator conclusion per intent (audit events are newest first)."""
    notes: dict[str, dict[str, object]] = {}
    for event in store.observation_audit_events():
        intent_id = event.get("intent_id")
        if event["event_type"] == "RECONCILIATION_NOTE" and isinstance(intent_id, str) and intent_id not in notes:
            details = event.get("details")
            if isinstance(details, dict):
                notes[intent_id] = {"conclusion": details.get("conclusion"), "noted_at": event.get("occurred_at")}
    return notes
