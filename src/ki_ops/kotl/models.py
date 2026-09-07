"""KOTL domain records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable
from uuid import uuid4

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.qty import derive_status, leaves_qty, signed_qty

D = lambda v: v if isinstance(v, Decimal) else Decimal(str(v))


def _utc(ts: datetime | None = None) -> datetime:
    ts = ts or datetime.now(timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Submit:
    """One send attempt (KOTL-generated submit_id)."""

    submit_id: str
    submitted_at: datetime
    env: str
    ok: bool
    flex_order_ids: tuple[str, ...] = ()
    payload: tuple[dict, ...] = ()
    flex_response: dict | None = None

    @classmethod
    def new(
        cls,
        *,
        env: str,
        ok: bool,
        flex_order_ids: Iterable[str] = (),
        payload: Iterable[dict] = (),
        flex_response: dict | None = None,
        submitted_at: datetime | None = None,
    ) -> Submit:
        return cls(
            submit_id=str(uuid4()),
            submitted_at=_utc(submitted_at),
            env=env,
            ok=ok,
            flex_order_ids=tuple(flex_order_ids),
            payload=tuple(payload),
            flex_response=flex_response,
        )


@dataclass(frozen=True)
class WorkingOrder:
    """One Flex parent order in the OTL book."""

    flex_order_id: str
    submit_id: str
    trade_date: date
    symbol: str
    side: str
    fund: str
    position_group: str
    sent_qty: Decimal
    filled_qty: Decimal
    leaves_qty: Decimal
    status: OrderStatus
    last_seen_at: datetime
    avg_fill_px: Decimal | None = None
    flex_batch_id: str | None = None
    broker: str | None = None
    algo: str | None = None
    order_type: str | None = None

    @classmethod
    def from_submit_line(
        cls,
        *,
        submit_id: str,
        flex_order_id: str,
        trade_date: date,
        symbol: str,
        side: str,
        fund: str,
        position_group: str,
        unsigned_sent_qty,
        submitted_at: datetime | None = None,
    ) -> WorkingOrder:
        """Create a new working order right after submit (filled = 0)."""
        sent = signed_qty(side, unsigned_sent_qty)
        ts = _utc(submitted_at)
        return cls(
            flex_order_id=flex_order_id,
            submit_id=submit_id,
            trade_date=trade_date,
            symbol=symbol.upper(),
            side=str(side).upper(),
            fund=fund,
            position_group=position_group,
            sent_qty=sent,
            filled_qty=Decimal("0"),
            leaves_qty=sent,
            status=OrderStatus.OPEN,
            last_seen_at=ts,
        )

    @classmethod
    def from_flex_snapshot(
        cls,
        *,
        submit_id: str,
        trade_date: date,
        flex_order_id: str,
        symbol: str,
        side: str,
        fund: str,
        position_group: str,
        unsigned_sent_qty,
        unsigned_filled_qty,
        flex_status: str | None = None,
        avg_fill_px=None,
        last_seen_at: datetime | None = None,
        flex_batch_id: str | None = None,
        broker: str | None = None,
        algo: str | None = None,
        order_type: str | None = None,
    ) -> WorkingOrder:
        """Build from a Flex-like row (unsigned qtys + side)."""
        sent = signed_qty(side, unsigned_sent_qty)
        filled = signed_qty(side, unsigned_filled_qty)
        status = derive_status(sent, filled, flex_status=flex_status)
        leaves = leaves_qty(sent, filled, status)
        return cls(
            flex_order_id=flex_order_id,
            submit_id=submit_id,
            trade_date=trade_date,
            symbol=symbol.upper(),
            side=str(side).upper(),
            fund=fund,
            position_group=position_group,
            sent_qty=sent,
            filled_qty=filled,
            leaves_qty=leaves,
            status=status,
            last_seen_at=_utc(last_seen_at),
            avg_fill_px=D(avg_fill_px) if avg_fill_px is not None else None,
            flex_batch_id=flex_batch_id,
            broker=broker,
            algo=algo,
            order_type=order_type,
        )

    def with_flex_update(
        self,
        *,
        unsigned_filled_qty,
        flex_status: str | None = None,
        avg_fill_px=None,
        last_seen_at: datetime | None = None,
    ) -> WorkingOrder:
        """Return a copy with refreshed fill state (refresh path)."""
        filled = signed_qty(self.side, unsigned_filled_qty)
        status = derive_status(self.sent_qty, filled, flex_status=flex_status)
        leaves = leaves_qty(self.sent_qty, filled, status)
        return WorkingOrder(
            flex_order_id=self.flex_order_id,
            submit_id=self.submit_id,
            trade_date=self.trade_date,
            symbol=self.symbol,
            side=self.side,
            fund=self.fund,
            position_group=self.position_group,
            sent_qty=self.sent_qty,
            filled_qty=filled,
            leaves_qty=leaves,
            status=status,
            last_seen_at=_utc(last_seen_at),
            avg_fill_px=D(avg_fill_px) if avg_fill_px is not None else self.avg_fill_px,
            flex_batch_id=self.flex_batch_id,
            broker=self.broker,
            algo=self.algo,
            order_type=self.order_type,
        )
