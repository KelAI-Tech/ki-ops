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

from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.engine import PreTradeEngine, PreTradeResult
from ki_ops.intents import TargetIntent
from ki_ops.models import Holding, Order, Portfolio, Side
from ki_ops.portfolio import portfolio_from_holdings, turnover_ratio

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
    sod_gross: Decimal
    target_gross: Decimal
    sod_net: Decimal
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
            "allowed": self.allowed,
            "turnover": str(self.turnover),
            "sod_gross": str(self.sod_gross),
            "target_gross": str(self.target_gross),
            "sod_net": str(self.sod_net),
            "target_net": str(self.target_net),
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
                sod_gross=sod.gross_exposure,
                target_gross=tgt_port.gross_exposure,
                sod_net=sod.total_value,
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
        "turnover_avg": str(avg),
        "turnover_min": str(min(finite)) if finite else None,
        "turnover_max": str(max(finite)) if finite else None,
        "violation_code_day_counts": code_counts,
        "daily": [
            {
                "sod_date": d.sod_date,
                "target_date": d.target_date,
                "allowed": d.allowed,
                "turnover": str(d.turnover),
                "sod_gross": str(d.sod_gross),
                "target_gross": str(d.target_gross),
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
                "allowed",
                "turnover",
                "sod_gross",
                "target_gross",
                "sod_net",
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
                    "allowed": d.allowed,
                    "turnover": str(d.turnover),
                    "sod_gross": str(d.sod_gross),
                    "target_gross": str(d.target_gross),
                    "sod_net": str(d.sod_net),
                    "target_net": str(d.target_net),
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
    target_turnover: Decimal = Decimal("0.12"),
    unit_price: Decimal = UNIT_PRICE,
    ticker_by_infocode: Mapping[str, str] | None = None,
    price_by_infocode: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Build theoretical LSEG-id trades so one-way TO vs SOD is ``target_turnover``.

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
    k = (target_turnover * Decimal("2") * gmv) / raw_gross
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
        "sod_gross": str(sod.gross_exposure),
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


def turnover_breach_scale(
    sod: Portfolio,
    orders: Sequence[Order],
    *,
    target_turnover: Decimal = Decimal("0.26"),
) -> Decimal:
    """Scale factor so one-way turnover vs ``sod`` reaches ``target_turnover``."""
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
    )


def relax_settings_for_order_size_perturb(settings: RiskManagementSettings) -> RiskManagementSettings:
    """Isolate MAX_ORDER_SIZE — position/portfolio caps relaxed for this scenario."""
    return replace(
        settings,
        max_portfolio_value=Decimal("1000000000"),
        max_position_size=Decimal("100000000"),
        max_position_concentration=Decimal("1"),
    )


PerturbScenario = Literal["max-turnover", "max-order-size"]


def run_lseg_perturb(
    parquet_path: str | Path,
    trades_csv: str | Path,
    orders: Sequence[Order],
    *,
    scenario: PerturbScenario,
    sod_date: str = "2026-08-05",
    cash: Decimal | float | int | str = 0,
    config_path: str | Path | None = None,
    target_turnover: Decimal = Decimal("0.26"),
) -> dict[str, Any]:
    """SOD from parquet; perturb 8/6 POC trades to breach a specific YAML limit."""
    settings = load_risk_settings(config_path)
    panel = load_alpha_dollar_panel(parquet_path, start=sod_date, end=sod_date)
    pd = _require_pandas()
    sod_ts = pd.Timestamp(sod_date)
    if sod_ts not in panel.index:
        raise ValueError(f"{sod_date} not in alpha panel {parquet_path}")
    sod = portfolio_from_dollar_row(panel.loc[sod_ts], cash=cash, as_of=_as_of(sod_ts))

    base_to = turnover_ratio(sod, orders)
    scale = Decimal("1")
    if scenario == "max-turnover":
        settings = relax_settings_for_turnover_perturb(settings)
        scale = turnover_breach_scale(sod, orders, target_turnover=target_turnover)
        orders = scale_orders(orders, scale)
    elif scenario == "max-order-size":
        settings = relax_settings_for_order_size_perturb(settings)
    else:
        raise ValueError(f"Unknown perturb scenario: {scenario}")

    engine = PreTradeEngine(settings=settings)
    result = engine.evaluate(sod, list(orders))
    codes = sorted({v.code for v in result.violations})
    return {
        "perturb": scenario,
        "sod_source": "parquet",
        "sod_date": sod_date,
        "alpha_parquet": str(parquet_path),
        "trade_intents_file": str(trades_csv),
        "config": str(config_path) if config_path else None,
        "max_turnover": str(settings.max_turnover),
        "max_order_size": str(settings.max_order_size),
        "n_sod_names": len(sod.holdings),
        "sod_gross": str(sod.gross_exposure),
        "sod_net": str(sod.total_value),
        "n_orders": len(result.trade_intents),
        "base_turnover": str(base_to),
        "trade_scale": str(scale),
        "turnover": str(result.turnover),
        "allowed": result.allowed,
        "projected_portfolio_value": str(result.projected_portfolio_value),
        "violation_codes": codes,
        "violations": [v.to_dict() for v in result.violations],
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
) -> dict[str, Any]:
    """Pre-trade: SOD = parquet row on ``sod_date``, orders from a trade-intent CSV."""
    panel = load_alpha_dollar_panel(parquet_path, start=sod_date, end=sod_date)
    pd = _require_pandas()
    sod_ts = pd.Timestamp(sod_date)
    if sod_ts not in panel.index:
        raise ValueError(f"{sod_date} not in alpha panel {parquet_path}")
    sod = portfolio_from_dollar_row(panel.loc[sod_ts], cash=cash, as_of=_as_of(sod_ts))
    result = engine.evaluate(sod, list(orders))
    codes = sorted({v.code for v in result.violations})
    return {
        "sod_source": "parquet",
        "sod_date": sod_date,
        "alpha_parquet": str(parquet_path),
        "trade_intents_file": str(trades_csv) if trades_csv else None,
        "config": str(config_path) if config_path else None,
        "n_sod_names": len(sod.holdings),
        "sod_gross": str(sod.gross_exposure),
        "sod_net": str(sod.total_value),
        "n_orders": len(result.trade_intents),
        "turnover": str(result.turnover),
        "allowed": result.allowed,
        "projected_portfolio_value": str(result.projected_portfolio_value),
        "violation_codes": codes,
        "violations": [v.to_dict() for v in result.violations],
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
            w.writerow(
                {
                    "ticker": r.get("ticker", ""),
                    "infocode": r["infocode"],
                    "quantity": str(_round_usd(r["notional"])),
                }
            )
    return {"sod_csv": str(sod_path), "target_csv": str(tgt_path), "trade_csv": str(trd_path)}

