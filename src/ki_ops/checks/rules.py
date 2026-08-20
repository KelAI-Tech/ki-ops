"""Pre-trade risk checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from ki_ops.checks import CheckViolation, Severity, block, warn
from ki_ops.config import RiskManagementSettings
from ki_ops.models import Order, Portfolio, Side
from ki_ops.portfolio import project_orders, turnover_ratio

ET = ZoneInfo("America/New_York")


def check_order_size(orders: Sequence[Order], settings: RiskManagementSettings) -> list[CheckViolation]:
    """Per-ticket cap: abs(trade_intent qty) × trade-time px vs min/max_order_size."""
    if not settings.enforce_order_size_limits:
        return []
    out = []
    for o in orders:
        size = abs(o.quantity) * o.limit_price
        if size < settings.min_order_size:
            out.append(block("MIN_ORDER_SIZE", f"{size} < min {settings.min_order_size}", o.symbol))
        if size > settings.max_order_size:
            out.append(block("MAX_ORDER_SIZE", f"{size} > max {settings.max_order_size}", o.symbol))
    return out


def check_orders_per_minute(orders: Sequence[Order], settings: RiskManagementSettings) -> list[CheckViolation]:
    buckets: dict[datetime, int] = defaultdict(int)
    for o in orders:
        ts = (o.timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
        buckets[ts] += 1
    return [
        block("MAX_ORDERS_PER_MINUTE", f"{n} orders in {m.isoformat()} > {settings.max_orders_per_minute}")
        for m, n in buckets.items()
        if n > settings.max_orders_per_minute
    ]


def check_position_and_portfolio_limits(
    portfolio: Portfolio, orders: Sequence[Order], settings: RiskManagementSettings
) -> list[CheckViolation]:
    projected = project_orders(portfolio, orders)
    out = []
    gmv = projected.gross_exposure
    if gmv > settings.max_portfolio_value:
        out.append(block("MAX_PORTFOLIO_VALUE", f"GMV {gmv} > max {settings.max_portfolio_value}"))
    base = gmv
    for symbol, h in projected.holdings.items():
        abs_qty = abs(h.quantity)
        abs_value = abs(h.market_value)
        # Share qty (= dollar notional / trade-time px when SOD is dollar-denominated).
        if abs_qty > settings.max_position_size:
            out.append(warn("MAX_POSITION_SIZE", f"{abs_qty} > {settings.max_position_size}", symbol))
        if base > 0:
            conc = abs_value / base
            if conc > settings.max_position_concentration:
                out.append(
                    block(
                        "MAX_POSITION_CONCENTRATION",
                        f"{conc:.4f} > {settings.max_position_concentration}",
                        symbol,
                    )
                )
    return out


def check_turnover(portfolio: Portfolio, orders: Sequence[Order], settings: RiskManagementSettings) -> list[CheckViolation]:
    ratio = turnover_ratio(portfolio, orders)
    if ratio > settings.max_turnover:
        return [block("MAX_TURNOVER", f"{ratio:.4f} > {settings.max_turnover}")]
    return []


def check_daily_loss(pnl: Decimal, settings: RiskManagementSettings) -> list[CheckViolation]:
    loss = -pnl if pnl < 0 else Decimal("0")
    if loss > settings.max_daily_loss:
        return [block("MAX_DAILY_LOSS", f"loss {loss} > {settings.max_daily_loss}")]
    return []


def check_volatility(
    orders: Sequence[Order], vols: Mapping[str, Decimal], settings: RiskManagementSettings
) -> list[CheckViolation]:
    out = []
    for o in orders:
        vol = vols.get(o.symbol)
        if vol is not None and vol > settings.max_position_volatility:
            out.append(block("MAX_POSITION_VOLATILITY", f"{vol} > {settings.max_position_volatility}", o.symbol))
    return out


def check_stop_loss_context(portfolio: Portfolio, settings: RiskManagementSettings) -> list[CheckViolation]:
    if not settings.use_stop_losses:
        return []
    out = []
    for symbol, h in portfolio.holdings.items():
        if h.cost_basis is None:
            out.append(warn("STOP_LOSS_MISSING_BASIS", "cost_basis missing", symbol))
            continue
        if h.cost_basis > 0 and h.quantity > 0:
            drawdown = (h.cost_basis - h.market_price) / h.cost_basis
            if drawdown >= settings.default_stop_loss:
                out.append(warn("STOP_LOSS_TRIGGERED", f"drawdown {drawdown:.4f}", symbol))
    return out


def check_market_hours(orders: Sequence[Order], settings: RiskManagementSettings) -> list[CheckViolation]:
    if not settings.enforce_market_hours:
        return []
    out = []
    for o in orders:
        ts = o.timestamp or datetime.now(timezone.utc)
        local = ts.astimezone(ET)
        t = local.time()
        ok = local.weekday() < 5 and (
            time(9, 30) <= t < time(16, 0)
            or (settings.pre_market_trading and time(4, 0) <= t < time(9, 30))
            or (settings.after_hours_trading and time(16, 0) <= t < time(20, 0))
        )
        if not ok:
            out.append(block("MARKET_HOURS", f"{ts.isoformat()} outside session", o.symbol))
    return out


def check_sell_availability(
    portfolio: Portfolio, orders: Sequence[Order], settings: RiskManagementSettings
) -> list[CheckViolation]:
    """When shorts are disallowed, only sell up to current long qty (max(qty, 0))."""
    if settings.allow_shorts:
        return []
    avail = {s: max(h.quantity, Decimal("0")) for s, h in portfolio.holdings.items()}
    out = []
    for o in orders:
        if o.side is Side.BUY:
            avail[o.symbol] = avail.get(o.symbol, Decimal("0")) + o.quantity
            continue
        if o.side is Side.SELL:
            have = avail.get(o.symbol, Decimal("0"))
            if o.quantity > have:
                out.append(block("INSUFFICIENT_HOLDINGS", f"sell {o.quantity} > long {have}", o.symbol))
            else:
                avail[o.symbol] = have - o.quantity
    return out


def run_all_checks(
    portfolio: Portfolio,
    orders: Sequence[Order],
    settings: RiskManagementSettings,
    *,
    pnl: Decimal = Decimal("0"),
    vols: Mapping[str, Decimal] | None = None,
) -> list[CheckViolation]:
    vols = vols or {}
    findings: list[CheckViolation] = []
    findings += check_daily_loss(pnl, settings)
    findings += check_order_size(orders, settings)
    findings += check_orders_per_minute(orders, settings)
    findings += check_market_hours(orders, settings)
    findings += check_sell_availability(portfolio, orders, settings)
    findings += check_turnover(portfolio, orders, settings)
    findings += check_position_and_portfolio_limits(portfolio, orders, settings)
    findings += check_volatility(orders, vols, settings)
    findings += check_stop_loss_context(portfolio, settings)
    return findings


def split_findings(findings: Sequence[CheckViolation]) -> tuple[tuple[CheckViolation, ...], tuple[CheckViolation, ...]]:
    blocks = tuple(v for v in findings if v.severity is Severity.BLOCK)
    warnings = tuple(v for v in findings if v.severity is Severity.WARN)
    return blocks, warnings
