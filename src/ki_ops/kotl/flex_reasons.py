"""Classify FlexTrade CreateOrders rejection messages.

FlexTrade's pre-trade exposure rules run an aggregate calculation across the
whole basket plus existing positions and open orders. Securities with missing
analytics inputs (price, delta, volume, Beta, ...) make that calculation emit
*warnings* — e.g.::

    Error: Calc failed for 13.9187% (298/2141) of securities. See exception
    report email.
    Error: Missing Beta for security MOMO.US
    Error: Missing ExposurePrice for security MPW.US
    Error: PVLA.US has invalid Avg Volume (90D) = NaN

The account is configured to tolerate 0.00% calc errors, so a single warning
forces the exposure rule — and with it the order — to fail. Per FlexTrade,
these are data-availability warnings (typically pre-market, before the day's
analytics are loaded), not order-level compliance verdicts: the same order
usually goes through on an intraday resend. Everything else — including a
bare ``create rejected`` (gateway ``success=false`` with no description) —
stays a true rejection.
"""

from __future__ import annotations

CALC_WARNING = "calc_warning"
TRUE_REJECTION = "rejection"

# Case-insensitive markers of the exposure-calc warning families observed on
# the Flex PROD gateway (see module docstring for full examples).
_CALC_WARNING_MARKERS = (
    "calc failed for",
    "missing beta for security",
    "missing exposureprice for security",
    "invalid avg volume",
)


def is_exposure_calc_warning(reason: str | None) -> bool:
    """True when *every* part of *reason* is a known calc-warning message.

    Gateway reasons can join several messages with ``;`` — a mix of a calc
    warning and anything unrecognized is conservatively a true rejection.
    """
    if not reason:
        return False
    parts = [p.strip() for p in str(reason).split(";") if p.strip()]
    if not parts:
        return False
    return all(
        any(marker in part.lower() for marker in _CALC_WARNING_MARKERS) for part in parts
    )


def rejection_kind(reason: str | None) -> str | None:
    """``calc_warning`` / ``rejection`` for a recorded reason; None without one."""
    if not reason:
        return None
    return CALC_WARNING if is_exposure_calc_warning(reason) else TRUE_REJECTION


def rejection_reason_from_result(result: dict) -> str | None:
    """The reason recorded for a failed CreateOrders *result* (None when ok).

    Prefers the gateway ``description``, falls back to the joined ``issues``
    list, and marks an unexplained failure as the bare ``create rejected``.
    """
    if result.get("success", True):
        return None
    issues = "; ".join(str(i) for i in (result.get("issues") or []) if str(i))
    return str(result.get("description") or "") or issues or "create rejected"
