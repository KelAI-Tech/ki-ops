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
