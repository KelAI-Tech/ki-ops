"""Alpha dollar panel → theoretical positions and day-over-day pre-trade POC.

Parquet layout: DatetimeIndex rows, security-id columns, float dollar notionals.
Without a price feed we use unit price ``1`` so ``quantity == dollars`` and
``market_value == dollars``. Security IDs are used as symbols for the POC.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from ki_ops.checks import CheckViolation, Severity, block, warn
from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.engine import PreTradeEngine, PreTradeResult, format_decimal, passed_status
from ki_ops.intents import TargetIntent
from ki_ops.models import Holding, Order, Portfolio, Side
from ki_ops.portfolio import (
    TURNOVER_CONVENTION,
    portfolio_from_holdings,
    project_orders,
    turnover_ratio,
)

# Unit price: qty = dollar notional / 1 → MV equals alpha dollars.
UNIT_PRICE = Decimal("1")


def _require_pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "pandas (and pyarrow) are required for alpha parquet support. "
            'Install with: pip install "ki-ops[alpha]" or pip install pandas pyarrow'
        ) from exc
    return pd


def load_alpha_dollar_panel(
    path: str | Path,
    *,
    start: str | None = None,
    end: str | None = None,
):
    """Load a wide alpha dollar panel; optionally slice by inclusive date range."""
    pd = _require_pandas()
    df = pd.read_parquet(path)
    if not hasattr(df.index, "year"):
        df.index = pd.to_datetime(df.index)
    df.columns = df.columns.astype(str)
    if start is not None:
        df = df.loc[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df.loc[df.index <= pd.Timestamp(end)]
    return df.sort_index()


def dollar_row_to_holdings(
    row,
    *,
    unit_price: Decimal = UNIT_PRICE,
) -> list[Holding]:
    """Convert one date's dollar series into holdings (skip NaN / zero)."""
    holdings: list[Holding] = []
    for sid, dollars in row.items():
        if dollars is None:
            continue
        try:
            if dollars != dollars:  # NaN
                continue
        except (TypeError, ValueError):
            continue
        d = Decimal(str(float(dollars)))
        if d == 0:
            continue
        holdings.append(Holding(str(sid), d / unit_price, unit_price))
    return holdings


def portfolio_from_dollar_row(
    row,
    *,
    cash: Decimal | float | int | str = 0,
    as_of: datetime | None = None,
    unit_price: Decimal = UNIT_PRICE,
) -> Portfolio:
    return portfolio_from_holdings(
        dollar_row_to_holdings(row, unit_price=unit_price),
        cash=cash,
        as_of=as_of,
    )


def targets_from_dollar_row(
    row,
    *,
    unit_price: Decimal = UNIT_PRICE,
) -> list[TargetIntent]:
    return [
        TargetIntent(h.symbol, h.quantity, h.market_price)
        for h in dollar_row_to_holdings(row, unit_price=unit_price)
    ]


@dataclass(frozen=True)
class AlphaDayResult:
    sod_date: str
    target_date: str
    allowed: bool
    turnover: Decimal
    sod_gmv: Decimal
    target_gross: Decimal
    sod_net_mv: Decimal
    target_net: Decimal
    n_sod_names: int
    n_target_names: int
    n_trade_intents: int
    violation_codes: tuple[str, ...]
    result: PreTradeResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "sod_date": self.sod_date,
            "target_date": self.target_date,
            "passed": passed_status(self.allowed, self.result.warnings),
            "turnover": format_decimal(self.turnover),
            "sod_gmv": format_decimal(self.sod_gmv),
            "target_gross": format_decimal(self.target_gross),
            "sod_net_mv": format_decimal(self.sod_net_mv),
            "target_net": format_decimal(self.target_net),
            "n_sod_names": self.n_sod_names,
            "n_target_names": self.n_target_names,
            "n_trade_intents": self.n_trade_intents,
            "violation_codes": list(self.violation_codes),
            "violations": [v.to_dict() for v in self.result.violations],
        }


def _as_of(ts) -> datetime:
    pd = _require_pandas()
    t = pd.Timestamp(ts).to_pydatetime()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def run_alpha_panel_checks(
    panel,
    engine: PreTradeEngine,
    *,
    cash: Decimal | float | int | str = 0,
    unit_price: Decimal = UNIT_PRICE,
) -> list[AlphaDayResult]:
    """For each consecutive pair of dates: SOD = day[i], targets = day[i+1]."""
    if len(panel.index) < 2:
        raise ValueError("Alpha panel needs at least two dates for day-over-day checks")

    out: list[AlphaDayResult] = []
    dates = list(panel.index)
    for i in range(len(dates) - 1):
        sod_ts, tgt_ts = dates[i], dates[i + 1]
        sod = portfolio_from_dollar_row(
            panel.loc[sod_ts], cash=cash, as_of=_as_of(sod_ts), unit_price=unit_price
        )
        targets = targets_from_dollar_row(panel.loc[tgt_ts], unit_price=unit_price)
        result = engine.evaluate_from_targets(
            sod,
            targets,
            timestamp=_as_of(tgt_ts),
            flatten_missing_targets=True,
        )
        tgt_port = portfolio_from_dollar_row(panel.loc[tgt_ts], cash=cash, unit_price=unit_price)
        codes = tuple(sorted({v.code for v in result.violations}))
        out.append(
            AlphaDayResult(
                sod_date=str(sod_ts.date()) if hasattr(sod_ts, "date") else str(sod_ts)[:10],
                target_date=str(tgt_ts.date()) if hasattr(tgt_ts, "date") else str(tgt_ts)[:10],
                allowed=result.allowed,
                turnover=result.turnover,
                sod_gmv=sod.gmv,
                target_gross=tgt_port.gmv,
                sod_net_mv=sod.total_value,
                target_net=tgt_port.total_value,
                n_sod_names=len(sod.holdings),
                n_target_names=len(tgt_port.holdings),
                n_trade_intents=len(result.trade_intents),
                violation_codes=codes,
                result=result,
            )
        )
    return out


def summarize_alpha_days(days: Sequence[AlphaDayResult]) -> dict[str, Any]:
    if not days:
        return {"n_days": 0, "daily": []}
    turnovers = [d.turnover for d in days]
    finite = [t for t in turnovers if t.is_finite()]
    avg = sum(finite, Decimal("0")) / len(finite) if finite else Decimal("0")
    code_counts: dict[str, int] = {}
    for d in days:
        for c in d.violation_codes:
            code_counts[c] = code_counts.get(c, 0) + 1
    return {
        "n_rebalance_days": len(days),
        "n_allowed": sum(1 for d in days if d.allowed),
        "n_blocked": sum(1 for d in days if not d.allowed),
        "turnover_convention": TURNOVER_CONVENTION,
        "turnover_avg": format_decimal(avg),
        "turnover_min": format_decimal(min(finite)) if finite else None,
        "turnover_max": format_decimal(max(finite)) if finite else None,
        "violation_code_day_counts": code_counts,
        "daily": [
            {
                "sod_date": d.sod_date,
                "target_date": d.target_date,
                "passed": passed_status(d.allowed, d.result.warnings),
                "turnover": format_decimal(d.turnover),
                "sod_gmv": format_decimal(d.sod_gmv),
                "target_gross": format_decimal(d.target_gross),
                "n_trade_intents": d.n_trade_intents,
                "violation_codes": list(d.violation_codes),
            }
            for d in days
        ],
    }


def write_turnover_csv(days: Sequence[AlphaDayResult], path: str | Path) -> Path:
    import csv

    path = Path(path)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "sod_date",
                "target_date",
                "passed",
                "turnover",
                "sod_gmv",
                "target_gross",
                "sod_net_mv",
                "target_net",
                "n_sod_names",
                "n_target_names",
                "n_trade_intents",
                "violation_codes",
            ],
        )
        w.writeheader()
        for d in days:
            w.writerow(
                {
                    "sod_date": d.sod_date,
                    "target_date": d.target_date,
                    "passed": passed_status(d.allowed, d.result.warnings),
                    "turnover": format_decimal(d.turnover),
                    "sod_gmv": format_decimal(d.sod_gmv),
                    "target_gross": format_decimal(d.target_gross),
                    "sod_net_mv": format_decimal(d.sod_net_mv),
                    "target_net": format_decimal(d.target_net),
                    "n_sod_names": d.n_sod_names,
                    "n_target_names": d.n_target_names,
                    "n_trade_intents": d.n_trade_intents,
                    "violation_codes": "|".join(d.violation_codes),
                }
            )
    return path


def load_infocode_ticker_map(path: str | Path) -> dict[str, str]:
    """Return ``{INFOCODE: TICKER}`` from ``KELAI.LSEG.SECURITY_MASTER_DT``-style CSV."""
    path = Path(path)
    out: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        fields = {str(k).strip().lower(): k for k in reader.fieldnames if k}
        id_key = fields.get("infocode") or fields.get("security_id")
        tic_key = fields.get("ticker") or fields.get("symbol")
        if not id_key or not tic_key:
            raise ValueError(f"Need INFOCODE and TICKER columns in {path}")
        for raw in reader:
            sid = (raw.get(id_key) or "").strip()
            tic = (raw.get(tic_key) or "").strip().upper()
            if sid and tic:
                out[sid] = tic
    return out


def apply_trade_time_prices(
    orders: Sequence[Order],
    prices: Mapping[str, Decimal],
    *,
    intents_are_dollars: bool = False,
) -> list[Order]:
    """Attach trade-time px so order size is abs(qty) × px.

    POC trade CSVs store signed share quantities. Pass ``intents_are_dollars=True``
    only for legacy dollar-intent rows (shares = dollars / px).
    """
    out: list[Order] = []
    for o in orders:
        px = prices.get(o.symbol)
        if px is None or px <= 0:
            out.append(o)
            continue
        qty = abs(o.quantity) / px if intents_are_dollars else abs(o.quantity)
        out.append(replace(o, quantity=qty, limit_price=px))
    return out


def portfolio_at_trade_time_prices(
    portfolio: Portfolio,
    prices: Mapping[str, Decimal],
    *,
    notionals_are_dollars: bool = True,
) -> Portfolio:
    """Reprice holdings with trade-time px (dollar SOD → shares = $ / px)."""
    holdings: list[Holding] = []
    for h in portfolio.holdings.values():
        px = prices.get(h.symbol)
        if px is None or px <= 0:
            holdings.append(h)
            continue
        qty = h.quantity / px if notionals_are_dollars else h.quantity
        holdings.append(Holding(h.symbol, qty, px, h.cost_basis))
    return portfolio_from_holdings(holdings, cash=portfolio.cash, as_of=portfolio.as_of)


def load_infocode_price_map(path: str | Path, *, field: str = "close") -> dict[str, Decimal]:
    """Return ``{INFOCODE: price}`` from a Datastream2 snapshot CSV (``open`` or ``close``)."""
    path = Path(path)
    out: dict[str, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        fields = {str(k).strip().lower(): k for k in reader.fieldnames if k}
        id_key = fields.get("infocode") or fields.get("security_id")
        px_key = fields.get(field.lower()) or fields.get("price") or fields.get("close")
        if not id_key or not px_key:
            raise ValueError(f"Need INFOCODE and {field} columns in {path}")
        for raw in reader:
            sid = (raw.get(id_key) or "").strip()
            px_raw = (raw.get(px_key) or "").strip()
            if not sid or not px_raw:
                continue
            px = Decimal(px_raw)
            if px > 0:
                out[sid] = px
    return out


def construct_lseg_trades_for_turnover(
    panel,
    *,
    sod_date: str = "2026-08-05",
    target_turnover: Decimal = Decimal("0.24"),
    unit_price: Decimal = UNIT_PRICE,
    ticker_by_infocode: Mapping[str, str] | None = None,
    price_by_infocode: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Build theoretical LSEG-id trades so two-way TO vs SOD is ``target_turnover``.

    SOD stays in dollar notionals (unit price 1). Trade shape = scaled
    (sod − prior parquet day). If ``price_by_infocode`` is set, trade quantity
    is shares = dollars / px so notional (qty × px) still hits the TO target.
    """
    pd = _require_pandas()
    sod_ts = pd.Timestamp(sod_date)
    if sod_ts not in panel.index:
        raise ValueError(f"{sod_date} not in alpha panel")
    prior_ts = prior_index_date(panel, sod_ts)
    sod_row = panel.loc[sod_ts].fillna(0.0)
    prior_row = panel.loc[prior_ts].reindex(sod_row.index).fillna(0.0)
    template = sod_row - prior_row
    gmv = Decimal(str(float(sod_row.abs().sum())))
    raw_gross = Decimal(str(float(template.abs().sum())))
    if gmv <= 0 or raw_gross <= 0:
        raise ValueError("Need positive SOD GMV and a non-zero prior-day move to scale")
    k = (target_turnover * gmv) / raw_gross
    trades = template * float(k)

    sod_holdings = [
        Holding(str(sid), Decimal(str(float(v))) / unit_price, unit_price)
        for sid, v in sod_row.items()
        if abs(float(v)) > 1e-8
    ]
    sod = portfolio_from_holdings(sod_holdings)

    tickers = ticker_by_infocode or {}
    prices = price_by_infocode or {}
    targets: list[TargetIntent] = []
    trade_rows: list[dict[str, str]] = []
    long_n = Decimal("0")
    short_n = Decimal("0")
    n_with_ticker = 0
    n_with_px = 0
    for sid in sod_row.index:
        sid_s = str(sid)
        ticker = tickers.get(sid_s, "")
        sod_dollars = Decimal(str(float(sod_row[sid])))
        dlt = Decimal(str(float(trades[sid])))
        tgt_dollars = sod_dollars + dlt
        if abs(tgt_dollars) > Decimal("0"):
            targets.append(TargetIntent(sid_s, tgt_dollars / unit_price, unit_price))
        if dlt == 0:
            continue
        if ticker:
            n_with_ticker += 1
        px = prices.get(sid_s) or unit_price
        if sid_s in prices:
            n_with_px += 1
        share_dlt = dlt / px
        if dlt > 0:
            long_n += dlt
        else:
            short_n += dlt
        side = "BUY" if dlt > 0 else "SELL"
        trade_rows.append(
            {
                "infocode": sid_s,
                "ticker": ticker,
                "symbol": sid_s,
                "side": side,
                "quantity": str(abs(share_dlt)),
                "price": str(px),
                "signed_quantity": str(share_dlt),
                "notional": str(dlt),
            }
        )

    orders = [
        Order(r["symbol"], Side(r["side"]), Decimal(r["quantity"]), Decimal(r["price"]))
        for r in trade_rows
    ]
    realized = turnover_ratio(sod, orders)
    return {
        "sod_date": str(sod_ts.date()),
        "prior_template_date": str(prior_ts.date()) if hasattr(prior_ts, "date") else str(prior_ts)[:10],
        "target_date": "theoretical+1",
        "scale_k": str(k),
        "target_turnover": str(target_turnover),
        "realized_turnover": str(realized),
        "turnover_convention": TURNOVER_CONVENTION,
        "sod_gmv": str(sod.gmv),
        "trade_long_notional": str(long_n),
        "trade_short_notional": str(short_n),
        "n_sod_names": len(sod.holdings),
        "n_targets": len(targets),
        "n_trades": len(trade_rows),
        "n_trades_with_ticker": n_with_ticker,
        "n_trades_with_px": n_with_px,
        "sod": sod,
        "targets": targets,
        "trade_rows": trade_rows,
        "orders": orders,
    }


def prior_index_date(panel, sod_ts):
    prior = panel.index[panel.index < sod_ts]
    if len(prior) == 0:
        raise ValueError(f"No parquet date before {sod_ts}")
    return prior.max()


def _round_usd(value: Decimal | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def scale_orders(orders: Sequence[Order], factor: Decimal) -> list[Order]:
    return [replace(o, quantity=o.quantity * factor) for o in orders]


def _expands_gross(sod: Portfolio, o: Order) -> bool:
    cur = sod.holdings[o.symbol].quantity if o.symbol in sod.holdings else Decimal("0")
    return abs(cur + o.signed_quantity()) > abs(cur)


def _clip_flatten(sod: Portfolio, o: Order, factor: Decimal) -> Order | None:
    """Scale a reducing ticket but do not reverse through zero (keeps GMV from rebounding)."""
    cur = sod.holdings[o.symbol].quantity if o.symbol in sod.holdings else Decimal("0")
    signed = o.signed_quantity() * factor
    if cur > 0 and signed < 0:
        signed = max(signed, -cur)
    elif cur < 0 and signed > 0:
        signed = min(signed, -cur)
    qty = abs(signed).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if qty == 0:
        return None
    side = Side.BUY if signed > 0 else Side.SELL
    return replace(o, quantity=qty, side=side)


def scale_orders_to_turnover_and_gmv(
    sod: Portfolio,
    orders: Sequence[Order],
    *,
    target_turnover: Decimal = Decimal("0.52"),
    target_gmv: Decimal = Decimal("90000000"),
) -> list[Order]:
    """Scale expanding vs contracting trades to hit two-way TO and projected GMV."""
    exp = [o for o in orders if _expands_gross(sod, o)]
    con = [o for o in orders if not _expands_gross(sod, o)]
    add = sum((abs(o.notional) for o in exp), Decimal("0"))
    red = sum((abs(o.notional) for o in con), Decimal("0"))
    g = sod.gmv
    if add <= 0 or red <= 0 or g <= 0:
        k = turnover_breach_scale(sod, orders, target_turnover=target_turnover)
        return round_order_shares(scale_orders(orders, k))

    rhs_sum = target_turnover * g
    rhs_diff = target_gmv - g
    ke = (rhs_sum + rhs_diff) / (Decimal("2") * add)
    kc = (rhs_sum - rhs_diff) / (Decimal("2") * red)
    if ke <= 0 or kc <= 0:
        k = turnover_breach_scale(sod, orders, target_turnover=target_turnover)
        return round_order_shares(scale_orders(orders, k))

    def assemble(k_exp: Decimal, k_con: Decimal) -> list[Order]:
        out: list[Order] = []
        for o in exp:
            scaled = replace(o, quantity=o.quantity * k_exp)
            qty = scaled.quantity.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if qty != 0:
                out.append(replace(scaled, quantity=qty))
        for o in con:
            clipped = _clip_flatten(sod, o, k_con)
            if clipped is not None:
                out.append(clipped)
        return out

    trial = assemble(ke, kc)
    lo, hi = ke * Decimal("0.2"), ke * Decimal("3")
    best = trial
    best_err = abs(project_orders(sod, trial).gmv - target_gmv)
    for _ in range(24):
        mid = (lo + hi) / 2
        kc_mid = (rhs_sum - mid * add) / red
        if kc_mid <= 0:
            hi = mid
            continue
        trial = assemble(mid, kc_mid)
        to = turnover_ratio(sod, trial)
        if to > 0:
            adj = target_turnover / to
            trial = assemble(mid * adj, kc_mid * adj)
        gmv = project_orders(sod, trial).gmv
        err = abs(gmv - target_gmv)
        if err < best_err:
            best, best_err, ke, kc = trial, err, mid, kc_mid
        if gmv > target_gmv:
            hi = mid
        else:
            lo = mid
    return best


def round_order_shares(orders: Sequence[Order]) -> list[Order]:
    """Half-up whole-share qty; drop names that round to 0."""
    out: list[Order] = []
    for o in orders:
        qty = o.quantity.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if qty == 0:
            continue
        out.append(replace(o, quantity=qty))
    return out


def tickers_from_trade_intents_csv(path: str | Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            sid = row.get("infocode") or row.get("symbol") or ""
            tic = row.get("ticker") or ""
            if sid:
                out[sid] = tic
    return out


def write_trade_intents_csv(
    orders: Sequence[Order],
    path: str | Path,
    *,
    ticker_by_infocode: Mapping[str, str] | None = None,
    keep_zero_qty: bool = False,
) -> Path:
    """Write ``ticker,infocode,quantity`` (signed whole-share qty)."""
    path = Path(path)
    tickers = ticker_by_infocode or {}
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "infocode", "quantity"])
        w.writeheader()
        for o in orders:
            qty = o.signed_quantity().quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if qty == 0 and not keep_zero_qty:
                continue
            w.writerow(
                {
                    "ticker": tickers.get(o.symbol, ""),
                    "infocode": o.symbol,
                    "quantity": str(int(qty)),
                }
            )
    return path


def zero_order_quantities(orders: Sequence[Order]) -> list[Order]:
    """Force every trade-intent qty to 0 (same names / sides / prices)."""
    return [replace(o, quantity=Decimal("0")) for o in orders]


def turnover_breach_scale(
    sod: Portfolio,
    orders: Sequence[Order],
    *,
    target_turnover: Decimal = Decimal("0.52"),
) -> Decimal:
    """Scale factor so two-way turnover vs ``sod`` reaches ``target_turnover``."""
    base = turnover_ratio(sod, orders)
    if base <= 0:
        raise ValueError("Need positive base turnover to scale trades")
    return target_turnover / base


def relax_settings_for_turnover_perturb(settings: RiskManagementSettings) -> RiskManagementSettings:
    """Isolate MAX_TURNOVER — other POC limits are relaxed for this scenario."""
    return replace(
        settings,
        max_order_size=Decimal("100000000"),
        max_portfolio_value=Decimal("1000000000"),
        max_position_size=Decimal("100000000"),
        max_position_concentration=Decimal("1"),
        min_order_size=Decimal("0"),
    )


def missing_price_findings(
    sod: Portfolio,
    orders: Sequence[Order],
    prices: Mapping[str, Decimal],
) -> list[CheckViolation]:
    """Findings for names absent from the trade-time price map.

    Silently keeping unit price 1 would understate order notionals in turnover
    and make dollar SOD notionals look like share quantities to the position
    size check — so live (non-zero qty) unpriced orders BLOCK and unpriced SOD
    names WARN.
    """
    unpriced_orders = sorted({o.symbol for o in orders if o.quantity != 0 and not prices.get(o.symbol)})
    unpriced_sod = sorted(s for s in sod.holdings if not prices.get(s))
    out: list[CheckViolation] = []
    if unpriced_orders:
        shown = ", ".join(unpriced_orders[:20])
        out.append(
            block(
                "MISSING_PRICE",
                f"{len(unpriced_orders)} live trade intents have no trade-time px: {shown}"
                f"{' …' if len(unpriced_orders) > 20 else ''}",
            )
        )
    if unpriced_sod:
        shown = ", ".join(unpriced_sod[:20])
        out.append(
            warn(
                "MISSING_PRICE_SOD",
                f"{len(unpriced_sod)} SOD names have no trade-time px "
                f"(share-based checks unreliable for them): {shown}"
                f"{' …' if len(unpriced_sod) > 20 else ''}",
            )
        )
    return out


PerturbScenario = Literal["baseline", "max-turnover", "zero-turnover"]


def run_lseg_perturb(
    orders: Sequence[Order],
    *,
    scenario: PerturbScenario,
    sod: Portfolio | None = None,
    sod_csv: str | Path | None = None,
    trades_csv: str | Path | None = None,
    cash: Decimal | float | int | str = 0,
    config_path: str | Path | None = None,
    target_turnover: Decimal = Decimal("0.52"),
    target_gmv: Decimal = Decimal("90000000"),
    prices_csv: str | Path | None = None,
    price_by_infocode: Mapping[str, Decimal] | None = None,
    ticker_by_infocode: Mapping[str, str] | None = None,
    ticker_map_csv: str | Path | None = None,
    scaled_trades_csv: str | Path | None = None,
) -> dict[str, Any]:
    """POC risk check: same SOD + trade CSVs; optional scale/relax for one breach."""
    from ki_ops.intents import load_sod_positions_csv

    settings = load_risk_settings(config_path)
    if sod is None:
        if sod_csv is None:
            raise ValueError("Need sod Portfolio or sod_csv")
        sod = load_sod_positions_csv(sod_csv, cash=cash)
    if not price_by_infocode and prices_csv:
        price_by_infocode = load_infocode_price_map(prices_csv, field="close")
    if price_by_infocode:
        orders = apply_trade_time_prices(orders, price_by_infocode)
        sod = portfolio_at_trade_time_prices(sod, price_by_infocode)

    trades_out = Path(trades_csv) if trades_csv else None
    if scenario == "max-turnover":
        settings = relax_settings_for_turnover_perturb(settings)
        orders = scale_orders_to_turnover_and_gmv(
            sod, orders, target_turnover=target_turnover, target_gmv=target_gmv
        )
        if trades_out is not None:
            tickers = dict(ticker_by_infocode or {})
            tickers.update(tickers_from_trade_intents_csv(trades_out))
            dest = Path(scaled_trades_csv) if scaled_trades_csv else trades_out.with_name(f"{trades_out.stem}_scaled.csv")
            trades_out = write_trade_intents_csv(orders, dest, ticker_by_infocode=tickers)
    elif scenario == "zero-turnover":
        orders = zero_order_quantities(orders)
        if trades_out is not None:
            tickers = dict(ticker_by_infocode or {})
            tickers.update(tickers_from_trade_intents_csv(trades_out))
            dest = Path(scaled_trades_csv) if scaled_trades_csv else trades_out.with_name(f"{trades_out.stem}_zero.csv")
            trades_out = write_trade_intents_csv(
                orders, dest, ticker_by_infocode=tickers, keep_zero_qty=True
            )
    elif scenario == "baseline":
        pass
    else:
        raise ValueError(f"Unknown perturb scenario: {scenario}")

    engine = PreTradeEngine(settings=settings)
    result = engine.evaluate(sod, list(orders))
    if price_by_infocode:
        findings = missing_price_findings(sod, list(orders), price_by_infocode)
        extra_blocks = tuple(v for v in findings if v.severity is Severity.BLOCK)
        extra_warnings = tuple(v for v in findings if v.severity is Severity.WARN)
        if findings:
            result = replace(
                result,
                allowed=result.allowed and not extra_blocks,
                violations=result.violations + extra_blocks,
                warnings=result.warnings + extra_warnings,
            )
    codes = sorted({v.code for v in result.violations})
    warn_codes = sorted({v.code for v in result.warnings})
    return {
        "perturb": scenario,
        "sod_source": "csv",
        "config": str(config_path) if config_path else None,
        "ticker_map": str(ticker_map_csv) if ticker_map_csv else None,
        "prices_csv": str(prices_csv) if prices_csv else None,
        "sod_csv": str(sod_csv) if sod_csv else None,
        "trade_intents_file": str(trades_out) if trades_out else None,
        "max_turnover": format_decimal(settings.max_turnover),
        "max_order_size": format_decimal(settings.max_order_size),
        "n_priced": sum(1 for o in orders if o.limit_price != UNIT_PRICE),
        "n_sod_names": len(sod.holdings),
        "sod_gmv": format_decimal(sod.gmv),
        "sod_net_mv": format_decimal(sod.total_value),
        "n_orders": len(result.trade_intents),
        "turnover": format_decimal(result.turnover),
        "turnover_convention": TURNOVER_CONVENTION,
        "passed": passed_status(result.allowed, result.warnings),
        "projected_portfolio_value": format_decimal(result.projected_portfolio_value),
        "violation_codes": codes,
        "violations": [v.to_dict() for v in result.violations],
        "warning_codes": warn_codes,
        "warnings": [v.to_dict() for v in result.warnings],
    }


def evaluate_parquet_sod_vs_trade_intents(
    parquet_path: str | Path,
    orders: Sequence[Order],
    engine: PreTradeEngine,
    *,
    sod_date: str = "2026-08-05",
    cash: Decimal | float | int | str = 0,
    trades_csv: str | Path | None = None,
    config_path: str | Path | None = None,
    price_by_infocode: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Pre-trade: SOD = parquet row on ``sod_date``, orders from a trade-intent CSV."""
    panel = load_alpha_dollar_panel(parquet_path, start=sod_date, end=sod_date)
    pd = _require_pandas()
    sod_ts = pd.Timestamp(sod_date)
    if sod_ts not in panel.index:
        raise ValueError(f"{sod_date} not in alpha panel {parquet_path}")
    sod = portfolio_from_dollar_row(panel.loc[sod_ts], cash=cash, as_of=_as_of(sod_ts))
    if price_by_infocode:
        sod = portfolio_at_trade_time_prices(sod, price_by_infocode)
    result = engine.evaluate(sod, list(orders))
    codes = sorted({v.code for v in result.violations})
    return {
        "sod_source": "parquet",
        "sod_date": sod_date,
        "alpha_parquet": str(parquet_path),
        "trade_intents_file": str(trades_csv) if trades_csv else None,
        "config": str(config_path) if config_path else None,
        "n_sod_names": len(sod.holdings),
        "sod_gmv": format_decimal(sod.gmv),
        "sod_net_mv": format_decimal(sod.total_value),
        "n_orders": len(result.trade_intents),
        "turnover": format_decimal(result.turnover),
        "turnover_convention": TURNOVER_CONVENTION,
        "passed": passed_status(result.allowed, result.warnings),
        "projected_portfolio_value": format_decimal(result.projected_portfolio_value),
        "violation_codes": codes,
        "violations": [v.to_dict() for v in result.violations],
        "warnings": [v.to_dict() for v in result.warnings],
    }


def write_lseg_rebalance_csvs(
    built: dict[str, Any],
    *,
    csv_dir: str | Path,
    ticker_by_infocode: Mapping[str, str] | None = None,
) -> dict[str, str]:
    csv_dir = Path(csv_dir)
    tickers = ticker_by_infocode or {}
    sod_path = csv_dir / f"sod_lseg_{built['sod_date'].replace('-', '')}.csv"
    tgt_path = csv_dir / "target_intents_lseg_20260806.csv"
    trd_path = csv_dir / "trade_intents_lseg_20260806.csv"
    with sod_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["infocode", "ticker", "notional"])
        w.writeheader()
        for h in built["sod"].holdings.values():
            w.writerow(
                {
                    "infocode": h.symbol,
                    "ticker": tickers.get(h.symbol, ""),
                    "notional": str(_round_usd(h.quantity * h.market_price)),
                }
            )
    with tgt_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["infocode", "ticker", "notional"])
        w.writeheader()
        for t in built["targets"]:
            if t.quantity == 0:
                continue
            ntl = t.quantity * (t.market_price or UNIT_PRICE)
            w.writerow(
                {
                    "infocode": t.symbol,
                    "ticker": tickers.get(t.symbol, ""),
                    "notional": str(ntl),
                }
            )
    with trd_path.open("w", encoding="utf-8", newline="") as fh:
        cols = ["ticker", "infocode", "quantity"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in built["trade_rows"]:
            qty = Decimal(r["signed_quantity"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if qty == 0:
                continue
            w.writerow(
                {
                    "ticker": r.get("ticker", ""),
                    "infocode": r["infocode"],
                    "quantity": str(qty),
                }
            )
    return {"sod_csv": str(sod_path), "target_csv": str(tgt_path), "trade_csv": str(trd_path)}

