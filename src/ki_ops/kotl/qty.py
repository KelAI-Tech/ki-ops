"""Signed quantity and status rules (no Flex I/O)."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ki_ops.kotl.enums import OrderStatus

D = lambda v: v if isinstance(v, Decimal) else Decimal(str(v))

# Flex OrderStatus values we treat as dead for remaining-work purposes (refine after UAT).
_CANCELLED_STATUSES = frozenset({"CANCELLED", "REJECTED", "LOCATE_FAILED"})

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


def derive_status(
    sent_qty: Decimal,
    filled_qty: Decimal,
    *,
    flex_status: str | int | None = None,
) -> OrderStatus:
    """Map signed sent/filled qtys (+ optional Flex status) to OTL status."""
    label = flex_status_label(flex_status)
    if label and label in _CANCELLED_STATUSES:
        return OrderStatus.CANCELLED

    sent_abs = abs(D(sent_qty))
    filled_abs = abs(D(filled_qty))

    if filled_abs == 0:
        return OrderStatus.OPEN
    if filled_abs < sent_abs:
        return OrderStatus.PARTIAL
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
