"""Trade file rendering, local write, and S3 upload (stubbed client)."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal

from ki_ops.kotl.trade_file import (
    TRADE_FILE_FIELDS,
    build_trade_file_rows,
    default_trade_file_dest,
    format_trade_table,
    render_trade_file_csv,
    write_trade_file,
)

TD = date(2026, 8, 6)

PAYLOADS = [
    {
        "symbol": "AAPL.US",
        "quantity": 30.0,
        "side": "BUY",
        "orderType": "MARKET",
        "algo": "VWAP_AMRS",
        "broker": "KEL-GS-EQ-LT",
        "fund": "KELAI",
        "positionGroup": "USATop2000_strategy_v1",
    },
    {
        "symbol": "MSFT.US",
        "quantity": 30.0,
        "side": "SELL",
        "orderType": "MARKET",
        "algo": "VWAP_AMRS",
        "broker": "KEL-GS-EQ-LT",
        "fund": "KELAI",
        "positionGroup": "USATop2000_strategy_v1",
    },
]

RESULTS = [
    {"orderId": "FLEX-1", "success": True},
    {"orderId": "FLEX-2", "success": False},
]


def test_build_rows_with_results():
    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=RESULTS
    )
    assert [r["flex_order_id"] for r in rows] == ["FLEX-1", "FLEX-2"]
    assert [r["status"] for r in rows] == ["submitted", "rejected"]
    assert all(r["dry_run"] == "false" for r in rows)
    assert rows[0]["trade_date"] == "2026-08-06"
    assert rows[0]["position_group"] == "USATop2000_strategy_v1"


def test_build_rows_dry_run():
    rows = build_trade_file_rows(trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, dry_run=True)
    assert all(r["status"] == "dry_run" for r in rows)
    assert all(r["dry_run"] == "true" for r in rows)
    assert all(r["flex_order_id"] == "" for r in rows)


def test_format_table_readable():
    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=RESULTS
    )
    table = format_trade_table(rows)
    assert "AAPL.US" in table
    assert "FLEX-2" in table
    assert "rejected" in table
    assert table.splitlines()[0].startswith("symbol")


def test_write_local(tmp_path):
    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=RESULTS
    )
    dest = tmp_path / "trades" / "20260806" / "trades_sub-1.csv"
    written = write_trade_file(rows, dest)
    assert written == str(dest)
    with dest.open(newline="") as fh:
        parsed = list(csv.DictReader(fh))
    assert tuple(parsed[0].keys()) == TRADE_FILE_FIELDS
    assert parsed[0]["symbol"] == "AAPL.US"
    assert parsed[0]["quantity"] == "30.0"
    assert parsed[1]["status"] == "rejected"


def test_write_s3_with_injected_client():
    calls = []

    class StubS3:
        def put_object(self, *, Bucket, Key, Body):
            calls.append((Bucket, Key, Body))

    rows = build_trade_file_rows(trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, dry_run=True)
    dest = "s3://kelaitrading/trades/USATop2000_neutralized/20260806/trades_sub-1_dryrun.csv"
    written = write_trade_file(rows, dest, s3_client=StubS3())
    assert written == dest
    assert len(calls) == 1
    bucket, key, body = calls[0]
    assert bucket == "kelaitrading"
    assert key == "trades/USATop2000_neutralized/20260806/trades_sub-1_dryrun.csv"
    assert body.decode("utf-8") == render_trade_file_csv(rows)


def test_default_dest_strategy_vs_local(tmp_path):
    s3 = default_trade_file_dest(
        trade_date=TD, submit_id="sub-1", strategy_id="USATop2000_neutralized"
    )
    assert s3 == "s3://kelaitrading/trades/USATop2000_neutralized/20260806/trades_sub-1.csv"

    dry = default_trade_file_dest(
        trade_date=TD, submit_id="sub-1", strategy_id="USATop2000_neutralized", dry_run=True
    )
    assert dry.endswith("trades_sub-1_dryrun.csv")

    local = default_trade_file_dest(trade_date=TD, submit_id="sub-1", data_dir=tmp_path)
    assert local == str(tmp_path / "trades" / "20260806" / "trades_sub-1.csv")
