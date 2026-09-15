"""Poll Flex (or fixtures) and update working orders.

GFD session expiry: KOTL orders are good-for-day, so when the refresh runs
after the trade date's NYSE close (plus a buffer —
:func:`ki_ops.kotl.market_hours.session_expired`), an order the snapshot
still shows not DONE derives ``cancelled`` with ``cancel_status`` set to the
KOTL literal ``EXPIRED_SESSION``, releasing its leaves. Without this, orders
purged by Flex's ~17:45 ET EOD sweep (live-observed PROD 2026-09-14:
never-routed orders stay queryable as ``TRADABLE / UNFINALIZED`` and
``CancelOrders`` refuses them) would sit ``open`` in the ledger forever with
phantom leaves. The rule never fires during the live session, so a refresh
of stuck prior-date rows (e.g. ``kotl refresh --trade-date 2026-09-14``) is
all it takes to repair them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence

from ki_ops.kotl.market_hours import session_expired
from ki_ops.kotl.models import WorkingOrder, _utc
from ki_ops.kotl.store import KotlStore


class FlexRefreshSource(Protocol):
    def fetch_orders(self, trade_date: str, *, stored: Sequence[WorkingOrder]) -> list[dict]: ...


def merge_order_snapshots(
    stored: Sequence[WorkingOrder],
    snapshots: Sequence[dict],
    *,
    trade_date: date,
    last_seen_at: datetime | None = None,
) -> list[WorkingOrder]:
    """Stored rows updated from *snapshots* (matched by ``orderId``), pure.

    Returns only the rows that had a matching snapshot; rows without one are
    omitted (their ledger state is already the freshest known). No store I/O —
    the refresh persists the result, ``kotl fills --live`` merges it for
    display only.

    Session expiry is evaluated at the snapshot observation instant:
    *last_seen_at* when given (tests inject it), else the market-hours clock.
    Past *trade_date*'s close + buffer, non-DONE orders flip to
    ``cancelled`` / ``EXPIRED_SESSION`` (see module docstring); merging a
    still-open session's snapshots leaves them untouched.
    """
    by_id = {str(row["orderId"]): row for row in snapshots}
    updated: list[WorkingOrder] = []
    seen_at = _utc(last_seen_at)
    expired = session_expired(trade_date, now=last_seen_at)
    for row in stored:
        snap = by_id.get(row.flex_order_id)
        if snap is None:
            continue
        updated.append(
            row.with_flex_update(
                unsigned_filled_qty=snap.get("filledQuantity", 0),
                flex_status=snap.get("status"),
                # Flex reports weightedAvgPrice=0 for an unfilled order —
                # treat it as "no average yet" so it never overwrites a real
                # px (with_flex_update keeps the stored value on None).
                avg_fill_px=snap.get("weightedAvgPrice") or None,
                last_seen_at=seen_at,
                flex_batch_id=snap.get("batchId"),
                finalization_status=snap.get("finalizationStatus"),
                cancel_status=snap.get("cancelStatus"),
                rejection_reason=snap.get("rejectionReason"),
                session_expired=expired,
            )
        )
    return updated


def refresh_working_orders(
    store: KotlStore,
    trade_date: date,
    source: FlexRefreshSource,
    *,
    last_seen_at: datetime | None = None,
) -> list[WorkingOrder]:
    """Update stored rows for *trade_date* from *source* snapshots (matched by ``orderId``).

    Session expiry semantics are :func:`merge_order_snapshots`'s; the refresh
    persists whatever it derives.
    """
    stored = store.load_working_orders(trade_date=trade_date)
    if not stored:
        return []

    snapshots = source.fetch_orders(trade_date.isoformat(), stored=stored)
    updated = merge_order_snapshots(
        stored, snapshots, trade_date=trade_date, last_seen_at=last_seen_at
    )
    if updated:
        store.upsert_working_orders(updated)
    return updated
