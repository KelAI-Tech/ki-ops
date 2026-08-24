"""EMS trade intents vs SOD from the alpha dollar parquet (no .pkl).

SOD: latest parquet date strictly before trade as-of (dollars → positions).
Trade intents: headerless EMS drop ``ticker,signed_share_qty,algo``.
Missing px: ``px_approx = |SOD alpha $| / |trade qty|`` after ticker→security_id map.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ki_ops.alpha import load_alpha_dollar_panel, portfolio_from_dollar_row
from ki_ops.engine import PreTradeEngine, format_decimal, passed_status
from ki_ops.models import D, Order, Side
from ki_ops.portfolio import TURNOVER_CONVENTION

_ASOF_RE = re.compile(r"(20\d{6})")


@dataclass(frozen=True)
class EmsIntent:
    symbol: str
    quantity: Decimal
    algo: str
    security_id: str | None = None
    prior_alpha_usd: Decimal | None = None
    px_approx: Decimal | None = None
    px_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "quantity", D(self.quantity))
        if self.prior_alpha_usd is not None:
            object.__setattr__(self, "prior_alpha_usd", D(self.prior_alpha_usd))
        if self.px_approx is not None:
            object.__setattr__(self, "px_approx", D(self.px_approx))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "algo": self.algo,
            "security_id": self.security_id,
            "prior_alpha_usd": None if self.prior_alpha_usd is None else str(self.prior_alpha_usd),
            "px_approx": None if self.px_approx is None else str(self.px_approx),
            "px_source": self.px_source,
        }


def parse_asof_from_filename(path: str | Path) -> date | None:
    """``Portfolio_20260813.csv`` → 2026-08-13."""
    m = _ASOF_RE.search(Path(path).name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d").date()


_EMS_HEADER = {"ticker", "symbol"}


def load_ems_trade_intents_csv(path: str | Path) -> list[EmsIntent]:
    """Load ``ticker,qty,algo[,infocode]``. Optional header row is skipped."""
    path = Path(path)
    out: list[EmsIntent] = []
    with path.open(encoding="utf-8", newline="") as fh:
        first = True
        for raw in csv.reader(fh):
            if not raw or not any(c.strip() for c in raw):
                continue
            if first and raw[0].strip().lower() in _EMS_HEADER:
                first = False
                continue
            first = False
            if len(raw) < 2:
                raise ValueError(f"Expected ticker,qty[,algo[,infocode]] in {path}: {raw!r}")
            sym, qty = raw[0].strip(), raw[1].strip()
            if not sym:
                continue
            algo = raw[2].strip() if len(raw) > 2 else ""
            sid = raw[3].strip() if len(raw) > 3 and raw[3].strip() else None
            out.append(EmsIntent(sym, Decimal(qty), algo, security_id=sid))
    return out


def load_security_id_ticker_map(path: str | Path | None) -> dict[str, str]:
    """Return ``{SYMBOL: security_id}``. CSV needs ``security_id`` and ``symbol`` (any case)."""
    if path is None:
        return {}
    path = Path(path)
    if not path.is_file():
        return {}
    mapping: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        fields = {str(k).strip().lower(): k for k in reader.fieldnames if k}
        sid_key = (
            fields.get("security_id")
            or fields.get("infocode")
            or fields.get("id")
            or fields.get("sid")
        )
        sym_key = fields.get("symbol") or fields.get("ticker")
        if not sid_key or not sym_key:
            raise ValueError(f"id-map CSV needs security_id and symbol columns: {path}")
        for raw in reader:
            sid = (raw.get(sid_key) or "").strip()
            sym = (raw.get(sym_key) or "").strip().upper()
            if sid and sym:
                mapping[sym] = sid
    return mapping


def prior_alpha_date(panel, as_of: date):
    """Latest panel date strictly before ``as_of``."""
    pd = __import__("pandas")
    cutoff = pd.Timestamp(as_of)
    prior = panel.index[panel.index < cutoff]
    if len(prior) == 0:
        raise ValueError(f"No alpha dollar date before {as_of} in panel")
    return prior.max()


def alpha_dollars_by_id(panel, as_of_ts) -> dict[str, Decimal]:
    row = panel.loc[as_of_ts]
    out: dict[str, Decimal] = {}
    for sid, val in row.items():
        if val is None:
            continue
        try:
            if val != val:
                continue
        except (TypeError, ValueError):
            continue
        d = Decimal(str(float(val)))
        if d == 0:
            continue
        out[str(sid)] = d
    return out


def approx_px_from_alpha_dollars(quantity: Decimal, dollars: Decimal) -> Decimal | None:
    """``px = prior_alpha_usd / trade_qty``. Price is unsigned."""
    if quantity == 0 or dollars is None:
        return None
    px = abs(dollars) / abs(quantity)
    return px if px > 0 else None


def enrich_intents_with_prior_alpha_px(
    intents: Sequence[EmsIntent],
    dollars_by_id: Mapping[str, Decimal],
    id_map: Mapping[str, str] | None = None,
) -> list[EmsIntent]:
    """Attach security_id, prior-day dollars, and px_approx.

    Lookup order per ticker:
    1. explicit id map
    2. ticker equals parquet security id
    """
    id_map = id_map or {}
    enriched: list[EmsIntent] = []
    for it in intents:
        sid = id_map.get(it.symbol)
        if sid is None and it.symbol in dollars_by_id:
            sid = it.symbol
        dollars = dollars_by_id.get(sid) if sid else None
        px = None
        source = None
        if it.quantity != 0 and dollars is not None:
            px = approx_px_from_alpha_dollars(it.quantity, dollars)
            source = "prior_alpha_usd / qty" if px is not None else None
        elif it.quantity == 0:
            source = "no_trade"
        elif sid is None:
            source = "unmapped_ticker"
        else:
            source = "no_prior_alpha_usd"
        enriched.append(
            EmsIntent(
                it.symbol,
                it.quantity,
                it.algo,
                security_id=sid,
                prior_alpha_usd=dollars,
                px_approx=px,
                px_source=source,
            )
        )
    return enriched


def intents_to_orders(intents: Iterable[EmsIntent], *, timestamp: datetime | None = None) -> list[Order]:
    """Non-zero intents with a px become Orders for turnover / pre-trade."""
    ts = timestamp
    orders: list[Order] = []
    for it in intents:
        if it.quantity == 0 or it.px_approx is None:
            continue
        side = Side.BUY if it.quantity > 0 else Side.SELL
        orders.append(
            Order(
                it.security_id or it.symbol,
                side,
                abs(it.quantity),
                it.px_approx,
                timestamp=ts,
                order_id=f"ems-{it.symbol}",
                display_label=side.value if it.quantity > 0 else "SELL",
            )
        )
    return orders


def summarize_px_coverage(intents: Sequence[EmsIntent], *, prior_date: str | None = None) -> dict[str, Any]:
    live = [i for i in intents if i.quantity != 0]
    priced = [i for i in live if i.px_approx is not None]
    unmapped = [i.symbol for i in live if i.px_source == "unmapped_ticker"]
    no_alpha = [i.symbol for i in live if i.px_source == "no_prior_alpha_usd"]
    traded_usd = sum((abs(i.quantity) * i.px_approx for i in priced), Decimal("0"))
    return {
        "prior_alpha_date": prior_date,
        "n_rows": len(intents),
        "n_live_intents": len(live),
        "n_priced": len(priced),
        "n_unmapped_live": len(unmapped),
        "n_mapped_no_alpha": len(no_alpha),
        "gross_traded_usd_approx": str(traded_usd),
        "one_way_traded_usd_approx": str(traded_usd / Decimal("2") if traded_usd else "0"),
        "px_formula": "abs(prior_alpha_usd) / abs(qty)",
        "unmapped_live_sample": unmapped[:20],
    }


def write_enriched_intents_csv(intents: Sequence[EmsIntent], path: str | Path) -> Path:
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "symbol",
                "quantity",
                "algo",
                "security_id",
                "prior_alpha_usd",
                "px_approx",
                "px_source",
            ],
        )
        w.writeheader()
        for it in intents:
            w.writerow(it.to_dict())
    return path


def scale_ems_targets_to_turnover(
    intents: Sequence[EmsIntent],
    open_px: Mapping[str, Decimal],
    sod_gross: Decimal,
    *,
    target_turnover: Decimal = Decimal("0.24"),
) -> tuple[list[EmsIntent], dict[str, Any]]:
    """Scale live EMS share qtys so two-way TO vs ``sod_gross`` equals ``target_turnover``.

    px from the as-of open. Unchanged names keep qty 0. Returns scaled rows (all names)
    plus stats. ``px_approx`` on live names is the open px.
    """
    live_gross = Decimal("0")
    for it in intents:
        if it.quantity == 0:
            continue
        px = open_px.get(it.symbol)
        if px is None or px == 0:
            raise ValueError(f"Missing open px for live name {it.symbol}")
        live_gross += abs(it.quantity * px)
    if live_gross == 0 or sod_gross <= 0:
        raise ValueError("Need live notionals and positive SOD GMV to scale turnover")
    raw_to = live_gross / sod_gross
    k = target_turnover / raw_to
    scaled: list[EmsIntent] = []
    long_n = Decimal("0")
    short_n = Decimal("0")
    for it in intents:
        px = open_px.get(it.symbol)
        if it.quantity == 0:
            scaled.append(
                EmsIntent(it.symbol, Decimal("0"), it.algo, px_approx=px, px_source="open")
            )
            continue
        qty = it.quantity * k
        ntl = qty * px
        if ntl > 0:
            long_n += ntl
        else:
            short_n += ntl
        scaled.append(
            EmsIntent(
                it.symbol,
                qty,
                it.algo,
                px_approx=px,
                px_source="open",
            )
        )
    gross = abs(long_n) + abs(short_n)
    to = gross / sod_gross
    stats = {
        "scale_k": format_decimal(k),
        "target_turnover": format_decimal(target_turnover),
        "realized_turnover": format_decimal(to),
        "sod_gmv": format_decimal(sod_gross),
        "trade_long_notional": format_decimal(long_n),
        "trade_short_notional": format_decimal(short_n),
        "gross_traded": format_decimal(gross),
        "one_way_notional": format_decimal(gross / Decimal("2")),
        "n_live": sum(1 for i in scaled if i.quantity != 0),
        "n_buy": sum(1 for i in scaled if i.quantity > 0),
        "n_sell": sum(1 for i in scaled if i.quantity < 0),
    }
    return scaled, stats


def write_target_intents_csv(intents: Sequence[EmsIntent], path: str | Path, *, live_only: bool = True) -> Path:
    """ki-ops target format: symbol,quantity,market_price."""
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol", "quantity", "market_price"])
        w.writeheader()
        for it in intents:
            if live_only and it.quantity == 0:
                continue
            w.writerow(
                {
                    "symbol": it.symbol,
                    "quantity": str(it.quantity),
                    "market_price": "" if it.px_approx is None else str(it.px_approx),
                }
            )
    return path


def _as_of_date(intents_csv: str | Path, as_of: date | str | None) -> date:
    if as_of is None:
        parsed = parse_asof_from_filename(intents_csv)
        if parsed is None:
            raise ValueError("Pass --as-of or use a filename like Portfolio_YYYYMMDD.csv")
        return parsed
    if isinstance(as_of, date):
        return as_of
    return datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()


def approximate_ems_prices_from_alpha(
    intents_csv: str | Path,
    alpha_parquet: str | Path,
    *,
    as_of: date | str | None = None,
    id_map_csv: str | Path | None = None,
) -> tuple[list[EmsIntent], dict[str, Any], str]:
    """SOD dollars + EMS intents; return enriched intents, coverage summary, SOD date."""
    intents = load_ems_trade_intents_csv(intents_csv)
    as_of_date = _as_of_date(intents_csv, as_of)
    panel = load_alpha_dollar_panel(alpha_parquet, end=str(as_of_date))
    prior_ts = prior_alpha_date(panel, as_of_date)
    dollars = alpha_dollars_by_id(panel, prior_ts)
    id_map = load_security_id_ticker_map(id_map_csv)
    enriched = enrich_intents_with_prior_alpha_px(intents, dollars, id_map)
    prior_str = str(prior_ts.date()) if hasattr(prior_ts, "date") else str(prior_ts)[:10]
    summary = summarize_px_coverage(enriched, prior_date=prior_str)
    summary["as_of"] = str(as_of_date)
    summary["sod_source"] = "parquet"
    summary["alpha_parquet"] = str(alpha_parquet)
    summary["id_map_csv"] = str(id_map_csv) if id_map_csv else None
    summary["n_id_map"] = len(id_map)
    return enriched, summary, prior_str


def evaluate_ems_against_alpha_sod(
    intents_csv: str | Path,
    alpha_parquet: str | Path,
    engine: PreTradeEngine,
    *,
    as_of: date | str | None = None,
    id_map_csv: str | Path | None = None,
) -> dict[str, Any]:
    """Pre-trade: SOD from parquet, trade intents from the EMS drop."""
    intents = load_ems_trade_intents_csv(intents_csv)
    as_of_date = _as_of_date(intents_csv, as_of)
    panel = load_alpha_dollar_panel(alpha_parquet, end=str(as_of_date))
    prior_ts = prior_alpha_date(panel, as_of_date)
    sod = portfolio_from_dollar_row(panel.loc[prior_ts])
    dollars = alpha_dollars_by_id(panel, prior_ts)
    id_map = load_security_id_ticker_map(id_map_csv)
    enriched = enrich_intents_with_prior_alpha_px(intents, dollars, id_map)
    orders = intents_to_orders(enriched)
    result = engine.evaluate(sod, orders)
    prior_str = str(prior_ts.date()) if hasattr(prior_ts, "date") else str(prior_ts)[:10]
    coverage = summarize_px_coverage(enriched, prior_date=prior_str)
    return {
        "as_of": str(as_of_date),
        "sod_date": prior_str,
        "sod_source": "parquet",
        "trade_intents_file": str(intents_csv),
        "n_sod_names": len(sod.holdings),
        "sod_gmv": format_decimal(sod.gmv),
        "sod_net_mv": format_decimal(sod.total_value),
        "passed": passed_status(result.allowed, result.warnings),
        "turnover": format_decimal(result.turnover),
        "turnover_convention": TURNOVER_CONVENTION,
        "n_orders": len(orders),
        "violations": [v.to_dict() for v in result.violations],
        "warnings": [v.to_dict() for v in result.warnings],
        "px_coverage": coverage,
        "id_map_csv": str(id_map_csv) if id_map_csv else None,
        "_enriched": enriched,
    }
