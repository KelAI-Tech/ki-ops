"""Tests for kelai get_orders / GetOrderInfo2 fixture loader."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ki_ops.kotl.kelai_refresh import (
    KelaiRefreshSource,
    flex_side_label,
    kelai_row_to_snapshot,
    load_kelai_orders_fixture,
)
from ki_ops.kotl.qty import flex_status_label
from ki_ops.kotl.refresh import refresh_working_orders
from ki_ops.kotl.refresh_source import load_refresh_source
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import submit_rebalance_csv

ROOT = Path(__file__).resolve().parents[2]
SOD = ROOT / "examples" / "sod_positions.csv"
TARGETS = ROOT / "examples" / "target_intents.csv"
KELAI_FIXTURE = ROOT / "examples" / "kotl" / "get_order_info2_sample.json"
FILLS_FIXTURE = ROOT / "examples" / "kotl" / "refresh_partial.json"
TD = date(2026, 8, 6)


def test_flex_enum_labels():
    assert flex_side_label(0) == "BUY"
    assert flex_side_label(1) == "SELL"
    assert flex_side_label("0") == "BUY"
    assert flex_status_label(4) == "PARTIALLY_FILLED"
    assert flex_status_label("5") == "FILLED"
    assert flex_status_label("tradable") == "TRADABLE"


def test_load_kelai_orders_json():
    rows, fx_date = load_kelai_orders_fixture(KELAI_FIXTURE)
    assert fx_date == TD
    assert len(rows) == 3
    assert rows[0]["symbol"] == "AAPL.US"


def test_kelai_row_to_snapshot_uses_account_target_fills():
    snap = kelai_row_to_snapshot(
        {
            "symbol": "AAPL.US",
            "batchId": "B9",
            "side": 1,
            "quantity": 18,
            "filledQuantity_acc_tgt": 10,
            "status": 4,
            "weightedAvgPrice_st": 191.2,
            "fund_acc_tgt": "KELAI",
            "positionGroup_acc_tgt": "DEFAULT",
        },
        order_id="OID-1",
        trade_date="2026-08-06",
    )
    assert snap["orderId"] == "OID-1"
    assert snap["batchId"] == "B9"
    assert snap["side"] == "SELL"
    assert snap["filledQuantity"] == 10.0
    assert snap["status"] == "PARTIALLY_FILLED"
    assert snap["weightedAvgPrice"] == 191.2


def test_kelai_refresh_by_symbol(tmp_path):
    store = KotlStore(tmp_path)
    submit_rebalance_csv(
        store,
        SOD,
        TARGETS,
        trade_date=TD,
        submitted_at=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )

    rows, _ = load_kelai_orders_fixture(KELAI_FIXTURE)
    updated = refresh_working_orders(
        store,
        TD,
        KelaiRefreshSource(rows, trade_date=TD),
        last_seen_at=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
    )
    assert len(updated) == 3

    after = {r.symbol: r for r in store.load_working_orders(trade_date=TD)}
    assert after["AAPL.US"].filled_qty == Decimal("-10")
    assert after["AAPL.US"].status.value == "partial"
    assert after["AMD.US"].status.value == "done"
    assert after["AVGO.US"].status.value == "open"

    # weightedAvgPrice lands in avg_fill_px; an unfilled order (no price in
    # the snapshot) keeps NULL instead of storing a bogus 0.
    assert after["AAPL.US"].avg_fill_px == Decimal("191.2")
    assert after["AMD.US"].avg_fill_px == Decimal("162.5")
    assert after["AVGO.US"].avg_fill_px is None

    # batchId from the snapshot backfills/overrides flex_batch_id; rows whose
    # snapshot has no batchId keep the id stamped at submit time.
    assert after["AAPL.US"].flex_batch_id == "FLEX-BATCH-42"
    assert after["AMD.US"].flex_batch_id is not None
    assert after["AMD.US"].flex_batch_id.startswith("FAKE-BATCH-")


def test_same_day_same_symbol_orders_update_independently(tmp_path):
    # Two working orders in the SAME name on the SAME day (first send + a
    # --force residual top-up). Each joins to its own GetOrderInfo2 row by
    # flex_order_id; fills never bleed across orders, and a missing row means
    # NO update for that order — the symbol fallback must not fire while the
    # symbol is ambiguous (two stored orders in the name).
    from ki_ops.kotl.models import WorkingOrder

    store = KotlStore(tmp_path)
    ts = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    store.upsert_working_orders(
        [
            WorkingOrder.from_submit_line(
                submit_id="sub-a",
                flex_order_id="SUB-A-1",
                trade_date=TD,
                symbol="AAPL.US",
                side="BUY",
                fund="KELAI",
                position_group="G",
                unsigned_sent_qty=50,
                submitted_at=ts,
            ),
            WorkingOrder.from_submit_line(
                submit_id="sub-b",
                flex_order_id="SUB-B-1",
                trade_date=TD,
                symbol="AAPL.US",
                side="BUY",
                fund="KELAI",
                position_group="G",
                unsigned_sent_qty=30,
                submitted_at=ts,
            ),
        ]
    )

    rows = [
        {"orderId": "SUB-A-1", "symbol": "AAPL.US", "side": 0, "quantity": 50,
         "filledQuantity": 50, "status": 5, "weightedAvgPrice": 191.0},
        {"orderId": "SUB-B-1", "symbol": "AAPL.US", "side": 0, "quantity": 30,
         "filledQuantity": 10, "status": 4, "weightedAvgPrice": 192.5},
    ]
    updated = refresh_working_orders(store, TD, KelaiRefreshSource(rows, trade_date=TD))
    after = {o.flex_order_id: o for o in updated}
    assert after["SUB-A-1"].filled_qty == Decimal("50")
    assert after["SUB-A-1"].status.value == "done"
    assert after["SUB-A-1"].avg_fill_px == Decimal("191.0")
    assert after["SUB-B-1"].filled_qty == Decimal("10")
    assert after["SUB-B-1"].status.value == "partial"
    assert after["SUB-B-1"].leaves_qty == Decimal("20")
    assert after["SUB-B-1"].avg_fill_px == Decimal("192.5")  # per-order, not shared

    # Snapshot covering only ONE of the two orders: the other stays untouched
    # (no cross-order symbol match).
    updated = refresh_working_orders(
        store,
        TD,
        KelaiRefreshSource(
            [{"orderId": "SUB-B-1", "symbol": "AAPL.US", "side": 0, "quantity": 30,
              "filledQuantity": 30, "status": 5, "weightedAvgPrice": 192.7}],
            trade_date=TD,
        ),
    )
    assert [o.flex_order_id for o in updated] == ["SUB-B-1"]
    a_after = store.get_working_order("SUB-A-1")
    assert a_after.filled_qty == Decimal("50")  # untouched by SUB-B's row


def test_load_refresh_source_auto_detect():
    from ki_ops.kotl.fixture_refresh import FixtureRefreshSource

    assert isinstance(load_refresh_source(FILLS_FIXTURE), FixtureRefreshSource)
    assert isinstance(load_refresh_source(KELAI_FIXTURE), KelaiRefreshSource)


def test_kelai_csv_fixture(tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "orderId,symbol,side,quantity,filledQuantity,status,tradeDate\n"
        "OID-X,AAPL.US,1,18,10,4,2026-08-06\n",
        encoding="utf-8",
    )
    rows, fx_date = load_kelai_orders_fixture(csv_path)
    assert len(rows) == 1
    assert fx_date == TD
    assert rows[0]["orderId"] == "OID-X"
