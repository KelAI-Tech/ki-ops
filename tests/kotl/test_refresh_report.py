"""Tests for KOTL refresh + status report."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ki_ops.kotl.fixture_refresh import FixtureRefreshSource
from ki_ops.kotl.report import build_status_report, format_status_table
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import submit_rebalance_csv
from ki_ops.kotl.refresh import refresh_working_orders

ROOT = Path(__file__).resolve().parents[2]
SOD = ROOT / "examples" / "sod_positions.csv"
TARGETS = ROOT / "examples" / "target_intents.csv"
FIXTURE = ROOT / "examples" / "kotl" / "refresh_partial.json"
TD = date(2026, 8, 6)


def test_refresh_partial_fixture_updates_store(tmp_path):
    store = KotlStore(tmp_path)
    submit_rebalance_csv(
        store,
        SOD,
        TARGETS,
        trade_date=TD,
        submitted_at=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )

    before = {r.symbol: r for r in store.load_working_orders(trade_date=TD)}
    assert before["AAPL.US"].leaves_qty == Decimal("-18")

    updated = refresh_working_orders(
        store,
        TD,
        FixtureRefreshSource(FIXTURE),
        last_seen_at=datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc),
    )
    assert len(updated) == 14

    after = {r.symbol: r for r in store.load_working_orders(trade_date=TD)}
    assert after["AAPL.US"].filled_qty == Decimal("-10")
    assert after["AAPL.US"].leaves_qty == Decimal("-8")
    assert after["AAPL.US"].status.value == "partial"
    assert after["AMD.US"].status.value == "done"
    assert after["AMD.US"].leaves_qty == Decimal("0")
    assert after["AVGO.US"].status.value == "open"


def test_status_report_and_table(tmp_path):
    store = KotlStore(tmp_path)
    submit_rebalance_csv(store, SOD, TARGETS, trade_date=TD)
    refresh_working_orders(store, TD, FixtureRefreshSource(FIXTURE))

    orders = store.load_working_orders(trade_date=TD)
    report = build_status_report(orders, trade_date=TD)
    assert report.open_count >= 1
    assert report.partial_count >= 1
    assert report.done_count >= 1
    assert not report.flat

    text = format_status_table(report)
    assert "AAPL.US" in text
    assert "total_abs_leaves=" in text

    payload = report.to_dict()
    assert payload["trade_date"] == "2026-08-06"
    assert len(payload["lines"]) == 14
