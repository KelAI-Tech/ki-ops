"""Core models: holdings, trades, orders, portfolio."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _utc(ts: datetime | None = None) -> datetime:
    ts = ts or datetime.now(timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Holding:
    """Position lot. ``quantity`` may be negative (short)."""

    symbol: str
    quantity: Decimal
    market_price: Decimal
    cost_basis: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "quantity", D(self.quantity))
        object.__setattr__(self, "market_price", D(self.market_price))
        if self.cost_basis is not None:
            object.__setattr__(self, "cost_basis", D(self.cost_basis))

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.market_price


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    timestamp: datetime
    trade_id: str | None = None
    fees: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "side", self.side if isinstance(self.side, Side) else Side(str(self.side).upper()))
        object.__setattr__(self, "quantity", D(self.quantity))
        object.__setattr__(self, "price", D(self.price))
        object.__setattr__(self, "fees", D(self.fees))
        object.__setattr__(self, "timestamp", _utc(self.timestamp))

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    timestamp: datetime | None = None
    order_id: str | None = None
    display_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "side", self.side if isinstance(self.side, Side) else Side(str(self.side).upper()))
        object.__setattr__(self, "quantity", D(self.quantity))
        object.__setattr__(self, "limit_price", D(self.limit_price))
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        if self.display_label:
            object.__setattr__(self, "display_label", self.display_label.upper())

    @property
    def notional(self) -> Decimal:
        """Order value for size/turnover: abs(quantity) × trade-time price."""
        return abs(self.quantity) * self.limit_price

    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side is Side.BUY else -self.quantity

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "display_label": self.display_label or self.side.value,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "order_id": self.order_id,
        }


@dataclass(frozen=True)
class Portfolio:
    holdings: Mapping[str, Holding] = field(default_factory=dict)
    cash: Decimal = Decimal("0")
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "holdings", {k.upper(): v for k, v in self.holdings.items()})
        object.__setattr__(self, "cash", D(self.cash))

    @property
    def total_value(self) -> Decimal:
        """Net equity + cash (shorts reduce equity)."""
        equity = sum((h.market_value for h in self.holdings.values()), Decimal("0"))
        return equity + self.cash

    @property
    def nmv(self) -> Decimal:
        """Σ signed position MV, cash excluded (longs − |shorts|)."""
        return sum((h.market_value for h in self.holdings.values()), Decimal("0"))

    @property
    def gmv(self) -> Decimal:
        """Σ|position MV|, cash excluded.

        Used as the denominator for turnover and position concentration —
        matching kelaisim, where GMV is positions-only.
        """
        return sum((abs(h.market_value) for h in self.holdings.values()), Decimal("0"))

    @property
    def net_exposure(self) -> Decimal:
        """|NMV| / GMV. Zero when GMV is zero."""
        if self.gmv <= 0:
            return Decimal("0")
        return abs(self.nmv) / self.gmv

    @property
    def gmv_plus_cash(self) -> Decimal:
        """Σ|position MV| + cash — total deployed capital.

        Used only by the MAX_PORTFOLIO_VALUE cap (and reported as
        ``projected_portfolio_value``); risk ratios use :attr:`gmv`.
        """
        return self.gmv + self.cash

    def get(self, symbol: str) -> Holding | None:
        return self.holdings.get(symbol.upper())

    def qty(self, symbol: str) -> Decimal:
        h = self.get(symbol)
        return h.quantity if h else Decimal("0")
