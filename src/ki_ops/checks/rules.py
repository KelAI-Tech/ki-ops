"""Pre-trade risk checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from ki_ops.checks import CheckViolation, Severity, block, warn
from ki_ops.config import RiskManagementSettings
from ki_ops.listing import ListingStatus
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
    # MAX_PORTFOLIO_VALUE caps deployed capital: positions GMV + cash.
    total = projected.gmv_plus_cash
    if total > settings.max_portfolio_value:
        out.append(
            block("MAX_PORTFOLIO_VALUE", f"GMV+cash {total} > max {settings.max_portfolio_value}")
        )
    # Concentration is |MV| / positions-only GMV (cash excluded, like kelaisim).
    base = projected.gmv
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


def check_adv_participation(
    orders: Sequence[Order],
    adv: Mapping[str, Decimal] | None,
    settings: RiskManagementSettings,
) -> list[CheckViolation]:
    """Warn when live order size exceeds ``max_adv_participation`` × ADV (share ADV)."""
    if settings.max_adv_participation <= 0 or not adv:
        return []
    out: list[CheckViolation] = []
    seen_missing: set[str] = set()
    for o in orders:
        if o.quantity == 0:
            continue
        a = adv.get(o.symbol)
        if a is None or a <= 0:
            if o.symbol not in seen_missing:
                seen_missing.add(o.symbol)
                out.append(
                    warn(
                        "MISSING_ADV",
                        "no ADV in snapshot; liquidity unknown",
                        o.symbol,
                    )
                )
            continue
        ratio = abs(o.quantity) / a
        if ratio > settings.max_adv_participation:
            out.append(
                warn(
                    "MAX_ADV_PARTICIPATION",
                    f"{ratio:.4f} of ADV > {settings.max_adv_participation}",
                    o.symbol,
                )
            )
    return out


def check_net_exposure(portfolio: Portfolio, orders: Sequence[Order], settings: RiskManagementSettings) -> list[CheckViolation]:
    """Block a projected book whose |NMV|/GMV exceeds ``max_net_exposure``."""
    projected = project_orders(portfolio, orders)
    if projected.gmv <= 0:
        return []
    ratio = projected.net_exposure
    if ratio > settings.max_net_exposure:
        return [
            block(
                "MAX_NET_EXPOSURE",
                f"{ratio:.4f} > {settings.max_net_exposure}",
            )
        ]
    return []


def check_tradability(
    orders: Sequence[Order],
    master: Mapping[str, ListingStatus] | None,
    *,
    as_of: date,
) -> list[CheckViolation]:
    """Warn on live trade intents in inactive / delisted names, or if unknown."""
    if master is None:
        return []
    out: list[CheckViolation] = []
    seen_unknown: set[str] = set()
    seen_dead: set[str] = set()
    for o in orders:
        if o.quantity == 0:
            continue
        rec = master.get(o.symbol)
        if rec is None:
            if o.symbol not in seen_unknown:
                seen_unknown.add(o.symbol)
                out.append(
                    warn(
                        "NOT_IN_SECURITY_MASTER",
                        "infocode not in security master; tradability unknown",
                        o.symbol,
                    )
                )
            continue
        if rec.is_tradable(as_of) or o.symbol in seen_dead:
            continue
        seen_dead.add(o.symbol)
        reason = rec.block_reason(as_of) or "not tradable"
        out.append(warn("NOT_TRADABLE", reason, o.symbol))
    return out


def drop_untradable_orders(
    orders: Sequence[Order],
    master: Mapping[str, ListingStatus] | None,
    *,
    as_of: date,
) -> tuple[list[Order], list[CheckViolation]]:
    """Drop live tickets in dead names; keep the rest of the book.

    Unknown infocodes stay in the batch (warn only). Zero-qty rows are kept.
    """
    findings = check_tradability(orders, master, as_of=as_of)
    dead = {v.symbol for v in findings if v.code == "NOT_TRADABLE" and v.symbol}
    kept = [o for o in orders if o.quantity == 0 or o.symbol not in dead]
    return kept, findings


def check_turnover(portfolio: Portfolio, orders: Sequence[Order], settings: RiskManagementSettings) -> list[CheckViolation]:
    ratio = turnover_ratio(portfolio, orders)
    if not ratio.is_finite():
        # Non-zero trade intents against a zero/negative gross-exposure book:
        # the turnover base is meaningless, so fail loudly instead of comparing.
        return [block("ZERO_GMV_BASE", "trade intents against a book with zero gross exposure")]
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
    listing: Mapping[str, ListingStatus] | None = None,
    as_of: date | None = None,
    adv: Mapping[str, Decimal] | None = None,
) -> list[CheckViolation]:
    vols = vols or {}
    findings: list[CheckViolation] = []
    findings += check_daily_loss(pnl, settings)
    findings += check_order_size(orders, settings)
    findings += check_orders_per_minute(orders, settings)
    findings += check_market_hours(orders, settings)
    findings += check_sell_availability(portfolio, orders, settings)
    findings += check_turnover(portfolio, orders, settings)
    findings += check_adv_participation(orders, adv, settings)
    findings += check_net_exposure(portfolio, orders, settings)
    findings += check_position_and_portfolio_limits(portfolio, orders, settings)
    findings += check_volatility(orders, vols, settings)
    findings += check_stop_loss_context(portfolio, settings)
    if listing is not None:
        trade_date = as_of or date.today()
        findings += check_tradability(orders, listing, as_of=trade_date)
    return findings


def split_findings(findings: Sequence[CheckViolation]) -> tuple[tuple[CheckViolation, ...], tuple[CheckViolation, ...]]:
    blocks = tuple(v for v in findings if v.severity is Severity.BLOCK)
    warnings = tuple(v for v in findings if v.severity is Severity.WARN)
    return blocks, warnings
