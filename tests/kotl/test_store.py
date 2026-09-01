"""Tests for KOTL CSV store (offline)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.models import Submit, WorkingOrder
from ki_ops.kotl.store import KotlStore


def _order(
    flex_order_id: str,
    *,
    submit_id: str = "sub-1",
    trade_date: date = date(2026, 8, 6),
    filled: str = "0",
    leaves: str = "123",
    status: OrderStatus = OrderStatus.OPEN,
) -> WorkingOrder:
    ts = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    return WorkingOrder(
        flex_order_id=flex_order_id,
        submit_id=submit_id,
        trade_date=trade_date,
        symbol="DASH.US",
        side="BUY",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        sent_qty=Decimal("123"),
        filled_qty=Decimal(filled),
        leaves_qty=Decimal(leaves),
        status=status,
        last_seen_at=ts,
    )


def test_append_and_load_submit(tmp_path):
    store = KotlStore(tmp_path)
    submit = Submit.new(
        env="UAT",
        ok=True,
        flex_order_ids=["ORD-1", "ORD-2"],
        payload=[{"symbol": "DASH.US", "quantity": 123}],
        flex_response={"status": "ok"},
        submitted_at=datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc),
    )
    store.append_submit(submit)

    loaded = store.load_submits()
    assert len(loaded) == 1
    assert loaded[0].submit_id == submit.submit_id
    assert loaded[0].flex_order_ids == ("ORD-1", "ORD-2")
    assert loaded[0].payload == ({"symbol": "DASH.US", "quantity": 123},)
    assert loaded[0].flex_response == {"status": "ok"}


def test_upsert_working_orders_idempotent(tmp_path):
    store = KotlStore(tmp_path)
    store.upsert_working_orders([_order("ORD-1")])

    updated = _order(
        "ORD-1",
        filled="50",
        leaves="73",
        status=OrderStatus.PARTIAL,
    )
    store.upsert_working_orders([updated])

    rows = store.load_working_orders()
    assert len(rows) == 1
    assert rows[0].filled_qty == Decimal("50")
    assert rows[0].status == OrderStatus.PARTIAL


def test_upsert_multiple_orders_and_filter(tmp_path):
    store = KotlStore(tmp_path)
    store.upsert_working_orders(
        [
            _order("ORD-1", trade_date=date(2026, 8, 6)),
            _order("ORD-2", trade_date=date(2026, 8, 7), submit_id="sub-2"),
        ]
    )

    assert len(store.load_working_orders(trade_date=date(2026, 8, 6))) == 1
    assert store.get_working_order("ORD-2").submit_id == "sub-2"


def test_empty_store_returns_empty_lists(tmp_path):
    store = KotlStore(tmp_path)
    assert store.load_submits() == []
    assert store.load_working_orders() == []
    assert store.get_working_order("missing") is None
