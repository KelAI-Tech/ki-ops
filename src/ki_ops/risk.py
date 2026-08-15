"""Market-neutral book snapshot: factor, sector/industry, and beta exposures.

Weights are signed market value over position GMV (cash excluded). That keeps a
dollar-neutral book from collapsing onto NAV/cash the way net-equity weights would.

Security attributes are file-driven. Loadings are not estimated here — feed an
export from Arcana, Barra, Axioma, Wolfe, or an internal model.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ki_ops.intents import TargetIntent, _rows, build_trade_intent_batch
from ki_ops.models import D, Portfolio
from ki_ops.portfolio import project_orders

ZERO = Decimal("0")

FACTOR_ORDER = ("value", "momentum", "size", "quality", "lowvol", "growth", "leverage")
BETA_ORDER = ("spx", "qqq", "iwm")
FACTOR_LABELS = {
    "value": "Value",
    "momentum": "Momentum",
    "size": "Size",
    "quality": "Quality",
    "lowvol": "Low Vol",
    "growth": "Growth",
    "leverage": "Leverage",
}
BETA_LABELS = {"spx": "SPX", "qqq": "QQQ", "iwm": "IWM"}
RESERVED = {"symbol", "sector", "industry", "name", "beta"}


def _label(key: str, labels: Mapping[str, str]) -> str:
    return labels.get(key, key.replace("_", " ").title())


@dataclass(frozen=True)
class SecurityRecord:
    symbol: str
    sector: str = "Unknown"
    industry: str = "Unknown"
    name: str | None = None
    betas: Mapping[str, Decimal] = field(default_factory=dict)
    factors: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "sector", self.sector or "Unknown")
        object.__setattr__(self, "industry", self.industry or "Unknown")
        object.__setattr__(self, "betas", {k.lower(): D(v) for k, v in self.betas.items()})
        object.__setattr__(self, "factors", {k.lower(): D(v) for k, v in self.factors.items()})


@dataclass(frozen=True)
class SecurityUniverse:
    records: Mapping[str, SecurityRecord]
    factor_ids: tuple[str, ...]
    beta_ids: tuple[str, ...]

    def get(self, symbol: str) -> SecurityRecord | None:
        return self.records.get(symbol.upper())


@dataclass(frozen=True)
class BucketExposure:
    id: str
    name: str
    net: Decimal
    long: Decimal
    short: Decimal
    net_mv: Decimal
    long_mv: Decimal
    short_mv: Decimal
    n_long: int
    n_short: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "net": str(self.net),
            "long": str(self.long),
            "short": str(self.short),
            "net_mv": str(self.net_mv),
            "long_mv": str(self.long_mv),
            "short_mv": str(self.short_mv),
            "n_long": self.n_long,
            "n_short": self.n_short,
        }


@dataclass(frozen=True)
class PositionExposure:
    symbol: str
    side: str
    quantity: Decimal
    market_value: Decimal
    weight_gmv: Decimal
    sector: str
    industry: str
    betas: Mapping[str, Decimal]
    factors: Mapping[str, Decimal]
    beta_contribution: Mapping[str, Decimal]
    factor_contribution: Mapping[str, Decimal]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "market_value": str(self.market_value),
            "weight_gmv": str(self.weight_gmv),
            "sector": self.sector,
            "industry": self.industry,
            "betas": {k: str(v) for k, v in self.betas.items()},
            "factors": {k: str(v) for k, v in self.factors.items()},
            "beta_contribution": {k: str(v) for k, v in self.beta_contribution.items()},
            "factor_contribution": {k: str(v) for k, v in self.factor_contribution.items()},
        }


@dataclass(frozen=True)
class BookExposures:
    nav: Decimal
    cash: Decimal
    gmv: Decimal
    nmv: Decimal
    long_mv: Decimal
    short_mv: Decimal
    net_pct_nav: Decimal | None
    gross_pct_nav: Decimal | None
    net_pct_gmv: Decimal
    n_long: int
    n_short: int
    dollar_beta: Mapping[str, Decimal]
    beta_gmv: Mapping[str, Decimal]
    beta_nav: Mapping[str, Decimal]
    factors: tuple[BucketExposure, ...]
    sectors: tuple[BucketExposure, ...]
    industries: tuple[BucketExposure, ...]
    betas: tuple[BucketExposure, ...]
    positions: tuple[PositionExposure, ...]
    missing_universe: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "nav": str(self.nav),
            "cash": str(self.cash),
            "gmv": str(self.gmv),
            "nmv": str(self.nmv),
            "long_mv": str(self.long_mv),
            "short_mv": str(self.short_mv),
            "net_pct_nav": None if self.net_pct_nav is None else str(self.net_pct_nav),
            "gross_pct_nav": None if self.gross_pct_nav is None else str(self.gross_pct_nav),
            "net_pct_gmv": str(self.net_pct_gmv),
            "n_long": self.n_long,
            "n_short": self.n_short,
            "dollar_beta": {k: str(v) for k, v in self.dollar_beta.items()},
            "beta_gmv": {k: str(v) for k, v in self.beta_gmv.items()},
            "beta_nav": {k: str(v) for k, v in self.beta_nav.items()},
            "factors": [b.to_dict() for b in self.factors],
            "sectors": [b.to_dict() for b in self.sectors],
            "industries": [b.to_dict() for b in self.industries],
            "betas": [b.to_dict() for b in self.betas],
            "positions": [p.to_dict() for p in self.positions],
            "missing_universe": list(self.missing_universe),
        }


@dataclass(frozen=True)
class RiskSnapshot:
    current: BookExposures
    projected: BookExposures | None = None

    def to_dict(self) -> dict:
        out = {
            "weighting": "signed_mv / position_gmv",
            "current": self.current.to_dict(),
        }
        if self.projected is not None:
            out["projected"] = self.projected.to_dict()
            out["delta"] = _delta_books(self.current, self.projected)
        return out


def _ordered_ids(present: set[str], preferred: Sequence[str]) -> tuple[str, ...]:
    head = [k for k in preferred if k in present]
    tail = sorted(present - set(head))
    return tuple(head + tail)


def load_security_master_csv(path: str | Path) -> SecurityUniverse:
    """CSV with ``symbol,sector,industry`` plus ``beta_*`` and ``factor_*`` columns.

    A bare ``beta`` column is treated as ``beta_spx``.
    """
    records: dict[str, SecurityRecord] = {}
    factor_ids: set[str] = set()
    beta_ids: set[str] = set()
    for row in _rows(Path(path)):
        symbol = (row.get("symbol") or "").upper()
        if not symbol:
            continue
        betas: dict[str, Decimal] = {}
        factors: dict[str, Decimal] = {}
        if row.get("beta"):
            betas["spx"] = D(row["beta"])
            beta_ids.add("spx")
        for key, raw in row.items():
            if not raw or key in RESERVED:
                continue
            if key.startswith("beta_"):
                bid = key[5:]
                betas[bid] = D(raw)
                beta_ids.add(bid)
            elif key.startswith("factor_"):
                fid = key[7:]
                factors[fid] = D(raw)
                factor_ids.add(fid)
        records[symbol] = SecurityRecord(
            symbol=symbol,
            sector=row.get("sector") or "Unknown",
            industry=row.get("industry") or "Unknown",
            name=row.get("name") or None,
            betas=betas,
            factors=factors,
        )
    return SecurityUniverse(
        records=records,
        factor_ids=_ordered_ids(factor_ids, FACTOR_ORDER),
        beta_ids=_ordered_ids(beta_ids, BETA_ORDER),
    )


def universe_from_records(records: Iterable[SecurityRecord]) -> SecurityUniverse:
    by_symbol = {r.symbol: r for r in records}
    factor_ids: set[str] = set()
    beta_ids: set[str] = set()
    for rec in by_symbol.values():
        factor_ids.update(rec.factors)
        beta_ids.update(rec.betas)
    return SecurityUniverse(
        records=by_symbol,
        factor_ids=_ordered_ids(factor_ids, FACTOR_ORDER),
        beta_ids=_ordered_ids(beta_ids, BETA_ORDER),
    )


class _Acc:
    __slots__ = ("net", "long", "short", "net_mv", "long_mv", "short_mv", "n_long", "n_short")

    def __init__(self) -> None:
        self.net = ZERO
        self.long = ZERO
        self.short = ZERO
        self.net_mv = ZERO
        self.long_mv = ZERO
        self.short_mv = ZERO
        self.n_long = 0
        self.n_short = 0

    def add(self, mv: Decimal, contrib: Decimal) -> None:
        self.net += contrib
        self.net_mv += mv
        if mv > 0:
            self.long += contrib
            self.long_mv += mv
            self.n_long += 1
        elif mv < 0:
            self.short += contrib
            self.short_mv += mv
            self.n_short += 1

    def to_bucket(self, key: str, labels: Mapping[str, str]) -> BucketExposure:
        return BucketExposure(
            id=key,
            name=_label(key, labels),
            net=self.net,
            long=self.long,
            short=self.short,
            net_mv=self.net_mv,
            long_mv=self.long_mv,
            short_mv=self.short_mv,
            n_long=self.n_long,
            n_short=self.n_short,
        )


def _pct(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    return num / den


def snapshot_book(portfolio: Portfolio, universe: SecurityUniverse) -> BookExposures:
    holdings = [h for h in portfolio.holdings.values() if h.quantity != 0]
    gmv = sum((abs(h.market_value) for h in holdings), ZERO)
    nmv = sum((h.market_value for h in holdings), ZERO)
    long_mv = sum((h.market_value for h in holdings if h.market_value > 0), ZERO)
    short_mv = sum((h.market_value for h in holdings if h.market_value < 0), ZERO)
    n_long = sum(1 for h in holdings if h.market_value > 0)
    n_short = sum(1 for h in holdings if h.market_value < 0)
    nav = portfolio.total_value
    missing: list[str] = []

    factor_acc = {fid: _Acc() for fid in universe.factor_ids}
    beta_acc = {bid: _Acc() for bid in universe.beta_ids}
    sector_acc: dict[str, _Acc] = defaultdict(_Acc)
    industry_acc: dict[str, _Acc] = defaultdict(_Acc)
    positions: list[PositionExposure] = []

    for h in holdings:
        rec = universe.get(h.symbol)
        if rec is None:
            missing.append(h.symbol)
            rec = SecurityRecord(h.symbol)
        mv = h.market_value
        w = ZERO if gmv == 0 else mv / gmv
        sector_acc[rec.sector].add(mv, w)
        industry_acc[rec.industry].add(mv, w)

        factor_contrib: dict[str, Decimal] = {}
        for fid in universe.factor_ids:
            loading = rec.factors.get(fid, ZERO)
            contrib = w * loading
            factor_acc[fid].add(mv, contrib)
            factor_contrib[fid] = contrib

        beta_contrib: dict[str, Decimal] = {}
        for bid in universe.beta_ids:
            loading = rec.betas.get(bid, ZERO)
            contrib = w * loading
            beta_acc[bid].add(mv, contrib)
            beta_contrib[bid] = contrib

        positions.append(
            PositionExposure(
                symbol=h.symbol,
                side="LONG" if mv >= 0 else "SHORT",
                quantity=h.quantity,
                market_value=mv,
                weight_gmv=w,
                sector=rec.sector,
                industry=rec.industry,
                betas=dict(rec.betas),
                factors=dict(rec.factors),
                beta_contribution=beta_contrib,
                factor_contribution=factor_contrib,
            )
        )

    dollar_beta = {bid: acc.net * gmv for bid, acc in beta_acc.items()}
    beta_gmv = {bid: acc.net for bid, acc in beta_acc.items()}
    beta_nav = {
        bid: (ZERO if nav == 0 else dollar / nav) for bid, dollar in dollar_beta.items()
    }
    positions.sort(key=lambda p: abs(p.market_value), reverse=True)
    return BookExposures(
        nav=nav,
        cash=portfolio.cash,
        gmv=gmv,
        nmv=nmv,
        long_mv=long_mv,
        short_mv=short_mv,
        net_pct_nav=_pct(nmv, nav),
        gross_pct_nav=_pct(gmv, nav),
        net_pct_gmv=ZERO if gmv == 0 else nmv / gmv,
        n_long=n_long,
        n_short=n_short,
        dollar_beta=dollar_beta,
        beta_gmv=beta_gmv,
        beta_nav=beta_nav,
        factors=tuple(factor_acc[k].to_bucket(k, FACTOR_LABELS) for k in universe.factor_ids),
        sectors=_sorted_buckets(sector_acc, {}),
        industries=_sorted_buckets(industry_acc, {}),
        betas=tuple(beta_acc[k].to_bucket(k, BETA_LABELS) for k in universe.beta_ids),
        positions=tuple(positions),
        missing_universe=tuple(sorted(missing)),
    )


def _sorted_buckets(acc: Mapping[str, _Acc], labels: Mapping[str, str]) -> tuple[BucketExposure, ...]:
    buckets = [a.to_bucket(k, labels or {k: k}) for k, a in acc.items()]
    buckets.sort(key=lambda b: (-abs(b.net), b.name))
    return tuple(buckets)


def _delta_bucket(cur: BucketExposure, proj: BucketExposure) -> dict:
    return {
        "id": proj.id,
        "name": proj.name,
        "net": str(proj.net - cur.net),
        "long": str(proj.long - cur.long),
        "short": str(proj.short - cur.short),
        "net_mv": str(proj.net_mv - cur.net_mv),
    }


def _align_buckets(
    current: Sequence[BucketExposure], projected: Sequence[BucketExposure]
) -> list[dict]:
    by_cur = {b.id: b for b in current}
    by_proj = {b.id: b for b in projected}
    empty = BucketExposure("", "", ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, 0, 0)
    out = []
    for key in list(by_cur) + [k for k in by_proj if k not in by_cur]:
        c = by_cur.get(key, empty)
        p = by_proj.get(key, empty)
        row = _delta_bucket(
            c if c.id else BucketExposure(key, p.name, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, 0, 0),
            p if p.id else BucketExposure(key, c.name, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, 0, 0),
        )
        out.append(row)
    return out


def _map_sub(a: Mapping[str, Decimal], b: Mapping[str, Decimal]) -> dict[str, str]:
    keys = list(a) + [k for k in b if k not in a]
    return {k: str(b.get(k, ZERO) - a.get(k, ZERO)) for k in keys}


def _delta_books(current: BookExposures, projected: BookExposures) -> dict:
    def sub(a: Decimal | None, b: Decimal | None) -> str | None:
        if a is None or b is None:
            return None
        return str(b - a)

    return {
        "nav": str(projected.nav - current.nav),
        "gmv": str(projected.gmv - current.gmv),
        "nmv": str(projected.nmv - current.nmv),
        "long_mv": str(projected.long_mv - current.long_mv),
        "short_mv": str(projected.short_mv - current.short_mv),
        "net_pct_nav": sub(current.net_pct_nav, projected.net_pct_nav),
        "gross_pct_nav": sub(current.gross_pct_nav, projected.gross_pct_nav),
        "net_pct_gmv": str(projected.net_pct_gmv - current.net_pct_gmv),
        "n_long": projected.n_long - current.n_long,
        "n_short": projected.n_short - current.n_short,
        "dollar_beta": _map_sub(current.dollar_beta, projected.dollar_beta),
        "beta_gmv": _map_sub(current.beta_gmv, projected.beta_gmv),
        "beta_nav": _map_sub(current.beta_nav, projected.beta_nav),
        "factors": _align_buckets(current.factors, projected.factors),
        "sectors": _align_buckets(current.sectors, projected.sectors),
        "industries": _align_buckets(current.industries, projected.industries),
        "betas": _align_buckets(current.betas, projected.betas),
    }


def build_risk_snapshot(
    portfolio: Portfolio,
    universe: SecurityUniverse,
    *,
    targets: Sequence[TargetIntent] | None = None,
    flatten_missing_targets: bool = True,
) -> RiskSnapshot:
    current = snapshot_book(portfolio, universe)
    if targets is None:
        return RiskSnapshot(current=current)
    batch = build_trade_intent_batch(
        portfolio, targets, flatten_missing_targets=flatten_missing_targets
    )
    projected = project_orders(portfolio, batch.trade_intents)
    return RiskSnapshot(current=current, projected=snapshot_book(projected, universe))
