"""Signed quantity and status rules (no Flex I/O)."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ki_ops.kotl.enums import OrderStatus

D = lambda v: v if isinstance(v, Decimal) else Decimal(str(v))

# Flex OrderStatus values we treat as dead for remaining-work purposes (refine after UAT).
_CANCELLED_STATUSES = frozenset({"CANCELLED", "REJECTED", "LOCATE_FAILED"})

#: KOTL-derived ``cancel_status`` marker for GFD session expiry — NOT a Flex
#: label. The ledger's cancel_status column normally stores Flex CancelStatus
#: labels (it is a free string); this literal records that KOTL itself
#: expired the order because its trade session closed with the remainder
#: unfilled. Needed because Flex's EOD sweep purges never-routed orders from
#: the working set while their queryable records stay TRADABLE/UNFINALIZED
#: forever and CancelOrders refuses them (live-observed PROD 2026-09-14).
EXPIRED_SESSION = "EXPIRED_SESSION"

_ORDER_STATUS = {
    0: "STAGED",
    2: "TRADABLE",
    3: "CANCELLED",
    4: "PARTIALLY_FILLED",
    5: "FILLED",
    6: "REJECTED",
    7: "LOCATE_FAILED",
}


#: Orders.proto CancelStatus — the cancel WORKFLOW, independent of OrderStatus.
#: Only CANCELED (4) is the confirmed terminal ack; REQUESTED/PENDING mean the
#: cancel window is still open and in-flight executions can still land.
_CANCEL_STATUS = {
    0: "CANCEL_ORIGINAL",
    1: "CANCEL_REQUESTED",
    2: "CANCEL_PENDING",
    3: "CANCEL_REJECTED",
    4: "CANCELED",
}

#: Orders.proto FinalizationStatus — a separate, REVERSIBLE workflow on top of
#: OrderStatus (FinalizeOrders has a rollback RPC). A risk-failed order sits
#: UNFINALIZED: parked and revivable, NOT dead.
_FINALIZATION_STATUS = {
    0: "UNFINALIZED",
    1: "FINALIZED",
    2: "FINALIZATION_COMPLIANCE_FAILED",
}


def cancel_status_label(status) -> str | None:
    """Proto enum int, numeric string, or name → CancelStatus label."""
    return _enum_label(status, _CANCEL_STATUS)


def finalization_status_label(status) -> str | None:
    """Proto enum int, numeric string, or name → FinalizationStatus label."""
    return _enum_label(status, _FINALIZATION_STATUS)


def _enum_label(status, mapping: dict[int, str]) -> str | None:
    if status is None or status == "":
        return None
    if isinstance(status, int):
        return mapping.get(status, str(status))
    text = str(status).strip().upper()
    if text.lstrip("-").isdigit():
        return mapping.get(int(text), text)
    return text


def flex_status_label(status) -> str | None:
    """Proto enum int, numeric string, or name → status label."""
    if status is None or status == "":
        return None
    if isinstance(status, int):
        return _ORDER_STATUS.get(status, str(status))
    text = str(status).strip()
    if text.isdigit():
        return _ORDER_STATUS.get(int(text), text)
    return text.upper()


def side_sign(side: str) -> Decimal:
    """BUY → +1; SELL / SHORT / other → −1."""
    return Decimal("1") if str(side).upper() == "BUY" else Decimal("-1")


def signed_qty(side: str, unsigned_qty) -> Decimal:
    """Convert Flex unsigned quantity + side to signed shares."""
    return side_sign(side) * abs(D(unsigned_qty))


def is_cancelled_flex_status(status) -> bool:
    """True when the Flex lifecycle label alone marks the order dead."""
    label = flex_status_label(status)
    return label is not None and label in _CANCELLED_STATUSES


def derive_status(
    sent_qty: Decimal,
    filled_qty: Decimal,
    *,
    flex_status: str | int | None = None,
    session_expired: bool = False,
) -> OrderStatus:
    """Map signed sent/filled qtys (+ optional Flex status) to OTL status.

    *session_expired* (GFD expiry — the caller decides via
    :func:`ki_ops.kotl.market_hours.session_expired`): the order's trade
    session is over, so any unfilled remainder can never execute — an order
    that would otherwise be OPEN/PARTIAL derives CANCELLED (expired),
    releasing its leaves. A fully filled order stays DONE, and fills already
    booked are untouched either way (post-close they are final).
    """
    label = flex_status_label(flex_status)
    if label and label in _CANCELLED_STATUSES:
        return OrderStatus.CANCELLED

    sent_abs = abs(D(sent_qty))
    filled_abs = abs(D(filled_qty))

    if filled_abs == 0:
        return OrderStatus.CANCELLED if session_expired else OrderStatus.OPEN
    if filled_abs < sent_abs:
        return OrderStatus.CANCELLED if session_expired else OrderStatus.PARTIAL
    return OrderStatus.DONE


def leaves_qty(
    sent_qty: Decimal,
    filled_qty: Decimal,
    status: OrderStatus,
) -> Decimal:
    """Outstanding signed qty; zero when done or cancelled (no remaining work)."""
    sent_qty = D(sent_qty)
    filled_qty = D(filled_qty)
    if status in (OrderStatus.DONE, OrderStatus.CANCELLED):
        return Decimal("0")
    return sent_qty - filled_qty


def is_flat(leaves: Iterable[Decimal], *, tolerance: Decimal = Decimal("0")) -> bool:
    """True when sum(abs(leaves)) <= tolerance (end-of-VWAP flat check)."""
    total = sum((abs(D(x)) for x in leaves), Decimal("0"))
    return total <= D(tolerance)
