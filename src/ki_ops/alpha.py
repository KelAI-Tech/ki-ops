"""Alpha dollar panel → theoretical positions and day-over-day pre-trade POC.

Parquet layout: DatetimeIndex rows, security-id columns, float dollar notionals.
Without a price feed we use unit price ``1`` so ``quantity == dollars`` and
``market_value == dollars``. Security IDs are used as symbols for the POC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

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


def construct_lseg_trades_for_turnover(
    panel,
    *,
    sod_date: str = "2026-08-05",
    target_turnover: Decimal = Decimal("0.12"),
    unit_price: Decimal = UNIT_PRICE,
) -> dict[str, Any]:
    """Build theoretical LSEG-id trades so one-way TO vs SOD is ``target_turnover``.

    SOD = parquet row on ``sod_date`` (qty = dollars / unit_price).
    Trade shape = scaled (sod − prior parquet day) so the mix matches the last
    observed rebalance, then k is chosen so (Σ|Δ$|/2) / GMV = target_turnover.
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

    targets: list[TargetIntent] = []
    trade_rows: list[dict[str, str]] = []
    long_n = Decimal("0")
    short_n = Decimal("0")
    for sid in sod_row.index:
        sid_s = str(sid)
        sod_qty = Decimal(str(float(sod_row[sid]))) / unit_price
        dlt = Decimal(str(float(trades[sid]))) / unit_price
        tgt_qty = sod_qty + dlt
        if abs(tgt_qty) > Decimal("0"):
            targets.append(TargetIntent(sid_s, tgt_qty, unit_price))
        if dlt == 0:
            continue
        ntl = dlt * unit_price
        if ntl > 0:
            long_n += ntl
        else:
            short_n += ntl
        side = "BUY" if dlt > 0 else "SELL"
        trade_rows.append(
            {
                "symbol": sid_s,
                "side": side,
                "quantity": str(abs(dlt)),
                "price": str(unit_price),
                "signed_quantity": str(dlt),
            }
        )

    orders = [
        Order(r["symbol"], Side(r["side"]), Decimal(r["quantity"]), unit_price)
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


def write_lseg_rebalance_csvs(built: dict[str, Any], *, csv_dir: str | Path) -> dict[str, str]:
    import csv as _csv

    csv_dir = Path(csv_dir)
    sod_path = csv_dir / f"sod_lseg_{built['sod_date'].replace('-', '')}.csv"
    tgt_path = csv_dir / "target_intents_lseg_20260806.csv"
    trd_path = csv_dir / "trade_intents_lseg_20260806.csv"
    with sod_path.open("w", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["symbol", "quantity", "market_price"])
        w.writeheader()
        for h in built["sod"].holdings.values():
            w.writerow({"symbol": h.symbol, "quantity": str(h.quantity), "market_price": str(h.market_price)})
    with tgt_path.open("w", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["symbol", "quantity", "market_price"])
        w.writeheader()
        for t in built["targets"]:
            if t.quantity == 0:
                continue
            w.writerow({"symbol": t.symbol, "quantity": str(t.quantity), "market_price": str(t.market_price or UNIT_PRICE)})
    with trd_path.open("w", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["symbol", "side", "quantity", "price", "signed_quantity"])
        w.writeheader()
        for r in built["trade_rows"]:
            w.writerow(r)
    return {"sod_csv": str(sod_path), "target_csv": str(tgt_path), "trade_csv": str(trd_path)}

