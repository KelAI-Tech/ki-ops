"""Portfolio helpers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping

from ki_ops.models import Holding, Order, Portfolio, Side, Trade


def portfolio_from_holdings(
    holdings: Iterable[Holding] | Mapping[str, Holding],
    *,
    cash: Decimal | float | int | str = 0,
    as_of: datetime | None = None,
) -> Portfolio:
    by_symbol = (
        {s.upper(): h for s, h in holdings.items()}
        if isinstance(holdings, Mapping)
        else {h.symbol: h for h in holdings}
    )
    return Portfolio(holdings=by_symbol, cash=Decimal(str(cash)), as_of=as_of)


def _apply(holdings: dict[str, Holding], symbol: str, signed_qty: Decimal, price: Decimal) -> None:
    cur = holdings.get(symbol)
    new_qty = (cur.quantity if cur else Decimal("0")) + signed_qty
    if new_qty == 0:
        holdings.pop(symbol, None)
    else:
        holdings[symbol] = Holding(
            symbol=symbol,
            quantity=new_qty,
            market_price=price,
            cost_basis=cur.cost_basis if cur else None,
        )


def apply_trades(portfolio: Portfolio, trades: Iterable[Trade]) -> Portfolio:
    holdings = dict(portfolio.holdings)
    cash = portfolio.cash
    as_of = portfolio.as_of
    for t in sorted(trades, key=lambda x: x.timestamp):
        signed = t.quantity if t.side is Side.BUY else -t.quantity
        _apply(holdings, t.symbol, signed, t.price)
        cash += -(t.notional + t.fees) if t.side is Side.BUY else (t.notional - t.fees)
        as_of = t.timestamp
    return Portfolio(holdings=holdings, cash=cash, as_of=as_of)


def project_orders(portfolio: Portfolio, orders: Iterable[Order]) -> Portfolio:
    holdings = dict(portfolio.holdings)
    cash = portfolio.cash
    as_of = portfolio.as_of
    for o in orders:
        _apply(holdings, o.symbol, o.signed_quantity(), o.limit_price)
        cash += -o.notional if o.side is Side.BUY else o.notional
        as_of = o.timestamp or as_of
    return Portfolio(holdings=holdings, cash=cash, as_of=as_of)


def turnover_ratio(portfolio: Portfolio, orders: Iterable[Order]) -> Decimal:
    """One-way turnover with shorts:

    (abs buy notional + abs sell notional) / 2 / (Σ|position MV| + cash)

    Buys and sells both enter the notional sum (covers, short opens, long trims),
    then ÷2 so a full book replace (exit + enter) reads as 100%, not 200%.
    Denominator is gross exposure, not net NAV — so a dollar-neutral book does
    not collapse the base to cash-only and inflate turnover.
    """
    buy_amt = sum((abs(o.notional) for o in orders if o.side is Side.BUY), Decimal("0"))
    sell_amt = sum((abs(o.notional) for o in orders if o.side is Side.SELL), Decimal("0"))
    traded = buy_amt + sell_amt
    base = portfolio.gross_exposure
    if base <= 0:
        return Decimal("0") if traded == 0 else Decimal("Infinity")
    return (traded / Decimal("2")) / base
