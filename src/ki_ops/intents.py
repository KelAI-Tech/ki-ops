"""SOD positions, target intents, and trade intents (target − SOD)."""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from ki_ops.models import D, Holding, Order, Portfolio, Side
from ki_ops.portfolio import portfolio_from_holdings


@dataclass(frozen=True)
class TargetIntent:
    symbol: str
    quantity: Decimal
    market_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "quantity", D(self.quantity))
        if self.market_price is not None:
            object.__setattr__(self, "market_price", D(self.market_price))


@dataclass(frozen=True)
class TradeIntentBatch:
    sod: Portfolio
    targets: tuple[TargetIntent, ...]
    trade_intents: tuple[Order, ...]

    def to_dict(self) -> dict:
        return {
            "sod_positions": [
                {"symbol": h.symbol, "quantity": str(h.quantity), "market_price": str(h.market_price)}
                for h in self.sod.holdings.values()
            ],
            "sod_cash": str(self.sod.cash),
            "targets": [
                {
                    "symbol": t.symbol,
                    "quantity": str(t.quantity),
                    "market_price": None if t.market_price is None else str(t.market_price),
                }
                for t in self.targets
            ],
            "trade_intents": [o.to_dict() for o in self.trade_intents],
        }


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        out = []
        for raw in reader:
            row = {str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in raw.items()}
            if any(row.values()):
                out.append(row)
        return out


def display_label(side: Side, sell_qty: Decimal, current_qty: Decimal) -> str:
    if side is Side.BUY:
        return "BUY"
    long_qty = current_qty if current_qty > 0 else Decimal("0")
    return "SHORT_SELL" if sell_qty > long_qty else "SELL"


def annotate_display_labels(portfolio: Portfolio, orders: Sequence[Order]) -> list[Order]:
    inv = {s: h.quantity for s, h in portfolio.holdings.items()}
    labeled = []
    for o in orders:
        cur = inv.get(o.symbol, Decimal("0"))
        label = o.display_label or display_label(o.side, o.quantity, cur)
        labeled.append(replace(o, display_label=label))
        inv[o.symbol] = cur + o.signed_quantity()
    return labeled


def load_symbol_volatilities(*paths: str | Path) -> dict[str, Decimal]:
    """Read optional ``volatility`` column from SOD/target CSVs (later files win)."""
    vols: dict[str, Decimal] = {}
    for path in paths:
        for row in _rows(Path(path)):
            sym = (row.get("symbol") or "").upper()
            raw = row.get("volatility") or row.get("vol")
            if sym and raw:
                vols[sym] = Decimal(raw)
    return vols


def load_sod_positions_csv(path: str | Path, *, cash=None) -> Portfolio:
    rows = _rows(Path(path))
    holdings = []
    file_cash = None
    for row in rows:
        if row.get("cash") and file_cash is None:
            file_cash = Decimal(row["cash"])
        sym = row["symbol"].upper()
        if sym in {"CASH", "__CASH__"}:
            file_cash = Decimal(row["quantity"])
            continue
        holdings.append(
            Holding(
                sym,
                Decimal(row["quantity"]),
                Decimal(row["market_price"]),
                Decimal(row["cost_basis"]) if row.get("cost_basis") else None,
            )
        )
    resolved = Decimal(str(cash)) if cash is not None else (file_cash or Decimal("0"))
    return portfolio_from_holdings(holdings, cash=resolved)


def load_target_intents_csv(path: str | Path) -> list[TargetIntent]:
    targets = []
    for row in _rows(Path(path)):
        price = row.get("market_price") or row.get("price") or None
        targets.append(
            TargetIntent(row["symbol"], Decimal(row["quantity"]), Decimal(price) if price else None)
        )
    return targets


def derive_trade_intents(
    sod: Portfolio,
    targets: Iterable[TargetIntent],
    *,
    timestamp: datetime | None = None,
    flatten_missing_targets: bool = True,
) -> list[Order]:
    by_tgt = {t.symbol: t for t in targets}
    symbols = set(sod.holdings) | set(by_tgt)
    ts = timestamp or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    orders = []
    for symbol in sorted(symbols):
        sod_qty = sod.qty(symbol)
        if symbol in by_tgt:
            tgt = by_tgt[symbol]
            target_qty, price = tgt.quantity, tgt.market_price
        elif flatten_missing_targets:
            target_qty, price = Decimal("0"), None
        else:
            continue

        if price is None:
            h = sod.get(symbol)
            if h is None:
                raise ValueError(f"No market_price for new symbol {symbol}")
            price = h.market_price

        delta = target_qty - sod_qty
        if delta == 0:
            continue
        side = Side.BUY if delta > 0 else Side.SELL
        qty = abs(delta)
        orders.append(
            Order(
                symbol,
                side,
                qty,
                price,
                ts,
                order_id=f"intent-{symbol}-{side.value.lower()}",
                display_label=display_label(side, qty, sod_qty),
            )
        )
    return orders


def build_trade_intent_batch(
    sod: Portfolio,
    targets: Sequence[TargetIntent],
    *,
    timestamp: datetime | None = None,
    flatten_missing_targets: bool = True,
) -> TradeIntentBatch:
    return TradeIntentBatch(
        sod=sod,
        targets=tuple(targets),
        trade_intents=tuple(
            derive_trade_intents(
                sod, targets, timestamp=timestamp, flatten_missing_targets=flatten_missing_targets
            )
        ),
    )
