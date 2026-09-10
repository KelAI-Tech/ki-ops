"""Poll Flex (or fixtures) and update working orders."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence

from ki_ops.kotl.models import WorkingOrder, _utc
from ki_ops.kotl.store import KotlStore


class FlexRefreshSource(Protocol):
    def fetch_orders(self, trade_date: str, *, stored: Sequence[WorkingOrder]) -> list[dict]: ...


def refresh_working_orders(
    store: KotlStore,
    trade_date: date,
    source: FlexRefreshSource,
    *,
    last_seen_at: datetime | None = None,
) -> list[WorkingOrder]:
    """Update stored rows for *trade_date* from *source* snapshots (matched by ``orderId``)."""
    stored = store.load_working_orders(trade_date=trade_date)
    if not stored:
        return []

    snapshots = source.fetch_orders(trade_date.isoformat(), stored=stored)
    by_id = {str(row["orderId"]): row for row in snapshots}

    updated: list[WorkingOrder] = []
    seen_at = _utc(last_seen_at)
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
            )
        )

    if updated:
        store.upsert_working_orders(updated)
    return updated
