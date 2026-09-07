"""Tests for offline KOTL submit flow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ki_ops.kotl.fake_flex import FakeFlexAdapter
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import submit_flex_orders, submit_rebalance_csv

ROOT = Path(__file__).resolve().parents[2]
SOD = ROOT / "examples" / "sod_positions.csv"
TARGETS = ROOT / "examples" / "target_intents.csv"


def test_submit_flex_orders_writes_store(tmp_path):
    store = KotlStore(tmp_path)
    adapter = FakeFlexAdapter(id_prefix="TEST")
    ts = datetime(2026, 8, 6, 14, 30, tzinfo=timezone.utc)

    submit = submit_flex_orders(
        store,
        [
            {
                "symbol": "AAPL.US",
                "quantity": 18,
                "side": "SELL",
                "fund": "KELAI",
                "positionGroup": "USATop2000_strategy_v1",
                "orderType": "MARKET",
            }
        ],
        env="FAKE",
        trade_date=date(2026, 8, 6),
        adapter=adapter,
        submitted_at=ts,
    )

    assert submit.ok
    assert len(submit.flex_order_ids) == 1
    assert submit.flex_order_ids[0].startswith("TEST-AAPL-US-1-")

    rows = store.load_working_orders(trade_date=date(2026, 8, 6))
    assert len(rows) == 1
    assert rows[0].sent_qty == Decimal("-18")
    assert rows[0].leaves_qty == Decimal("-18")
    assert rows[0].submit_id == submit.submit_id

    submits = store.load_submits()
    assert len(submits) == 1
    assert len(adapter.sent_batches) == 1


def test_submit_rebalance_csv_example_book(tmp_path):
    store = KotlStore(tmp_path)
    adapter = FakeFlexAdapter()

    submit = submit_rebalance_csv(
        store,
        SOD,
        TARGETS,
        trade_date=date(2026, 8, 6),
        adapter=adapter,
        submitted_at=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert submit.ok
    assert len(submit.flex_order_ids) == 14

    rows = store.load_working_orders(submit_id=submit.submit_id)
    assert len(rows) == 14

    aapl = next(r for r in rows if r.symbol == "AAPL.US")
    assert aapl.side == "SELL"
    assert aapl.sent_qty == Decimal("-18")
    assert aapl.fund == "KELAI"
    assert aapl.algo == "VWAP_AMRS"


def test_submit_flex_orders_empty_raises(tmp_path):
    store = KotlStore(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        submit_flex_orders(store, [])
