"""Tests for KOTL domain models (offline)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.models import Submit, WorkingOrder


def test_submit_generates_submit_id():
    submit = Submit.new(env="UAT", ok=True, flex_order_ids=["ORD-1"])
    assert submit.submit_id
    assert submit.flex_order_ids == ("ORD-1",)
    assert submit.env == "UAT"


def test_working_order_from_submit_line_buy():
    ts = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)
    row = WorkingOrder.from_submit_line(
        submit_id="sub-1",
        flex_order_id="ORD-1",
        trade_date=date(2026, 8, 6),
        symbol="dash.us",
        side="BUY",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        unsigned_sent_qty=123,
        submitted_at=ts,
    )
    assert row.symbol == "DASH.US"
    assert row.sent_qty == Decimal("123")
    assert row.filled_qty == Decimal("0")
    assert row.leaves_qty == Decimal("123")
    assert row.status == OrderStatus.OPEN


def test_working_order_refresh_partial_then_done():
    base = WorkingOrder.from_submit_line(
        submit_id="sub-1",
        flex_order_id="ORD-1",
        trade_date=date(2026, 8, 6),
        symbol="DASH.US",
        side="BUY",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        unsigned_sent_qty=123,
    )
    partial = base.with_flex_update(unsigned_filled_qty=50)
    assert partial.status == OrderStatus.PARTIAL
    assert partial.filled_qty == Decimal("50")
    assert partial.leaves_qty == Decimal("73")

    done = partial.with_flex_update(unsigned_filled_qty=123)
    assert done.status == OrderStatus.DONE
    assert done.leaves_qty == Decimal("0")


def test_working_order_from_flex_snapshot_sell():
    row = WorkingOrder.from_flex_snapshot(
        submit_id="sub-2",
        trade_date=date(2026, 8, 6),
        flex_order_id="ORD-2",
        symbol="AAPL.US",
        side="SELL",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        unsigned_sent_qty=200,
        unsigned_filled_qty=80,
        flex_status="PARTIALLY_FILLED",
    )
    assert row.sent_qty == Decimal("-200")
    assert row.filled_qty == Decimal("-80")
    assert row.leaves_qty == Decimal("-120")
    assert row.status == OrderStatus.PARTIAL
