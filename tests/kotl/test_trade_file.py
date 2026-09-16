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
    # Gateway reason lands in the file at submit time (refresh may enrich it
    # later); an unexplained failure records the bare marker.
    assert rows[0]["rejection_reason"] == ""
    assert rows[1]["rejection_reason"] == "create rejected"


def test_build_rows_records_gateway_description():
    results = [
        {"orderId": "FLEX-1", "success": True},
        {
            "orderId": "FLEX-2",
            "success": False,
            "description": "Error: Missing Beta for security MSFT.US",
        },
    ]
    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=results
    )
    assert rows[1]["status"] == "rejected"
    assert rows[1]["rejection_reason"] == "Error: Missing Beta for security MSFT.US"


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
    # Bare "create rejected" is a true rejection — no calc-warning marker.
    assert "calc-warning" not in table
    assert table.splitlines()[0].startswith("symbol")


def test_format_table_marks_exposure_calc_warnings():
    results = [
        {"orderId": "FLEX-1", "success": True},
        {
            "orderId": "FLEX-2",
            "success": False,
            "description": (
                "Error: Calc failed for 13.9187% (298/2141) of securities. "
                "See exception report email."
            ),
        },
    ]
    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=results
    )
    table = format_trade_table(rows)
    assert "rejected (calc-warning)" in table
    # The CSV keeps the plain gateway verdict.
    assert rows[1]["status"] == "rejected"


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


# --- ledger dispositions ----------------------------------------------------


def _wo(order_id: str, *, filled="0", status=None, finalization=None, cancel=None, reason=None):
    from datetime import datetime, timezone

    from ki_ops.kotl.enums import OrderStatus
    from ki_ops.kotl.models import WorkingOrder

    return WorkingOrder(
        flex_order_id=order_id,
        submit_id="sub-1",
        trade_date=TD,
        symbol="AAPL.US",
        side="BUY",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        sent_qty=Decimal("30"),
        filled_qty=Decimal(filled),
        leaves_qty=Decimal("30") - Decimal(filled),
        status=status or OrderStatus.OPEN,
        last_seen_at=datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc),
        finalization_status=finalization,
        cancel_status=cancel,
        rejection_reason=reason,
    )


def test_final_disposition_mapping():
    from ki_ops.kotl.enums import OrderStatus
    from ki_ops.kotl.trade_file import final_disposition

    assert final_disposition(_wo("A", filled="30", status=OrderStatus.DONE)) == "filled"
    assert final_disposition(_wo("B", filled="10", status=OrderStatus.PARTIAL)) == "partial"
    assert final_disposition(_wo("C", status=OrderStatus.OPEN, finalization="FINALIZED")) == "working"
    # Risk-rejected order: KOTL bucket says CANCELLED but Flex holds it
    # UNFINALIZED with no cancel workflow — parked and revivable.
    assert (
        final_disposition(
            _wo("D", status=OrderStatus.CANCELLED, finalization="UNFINALIZED", cancel="CANCEL_ORIGINAL")
        )
        == "unfinalized"
    )
    # Explicit terminal cancel ack is the only confirmed-dead cancel state.
    assert (
        final_disposition(_wo("E", status=OrderStatus.CANCELLED, cancel="CANCELED")) == "cancelled"
    )
    assert (
        final_disposition(_wo("F", status=OrderStatus.CANCELLED, cancel="CANCEL_PENDING"))
        == "cancel_pending"
    )
    assert (
        final_disposition(_wo("G", status=OrderStatus.PARTIAL, filled="10", finalization="UNFINALIZED"))
        == "partial_unfinalized"
    )


def test_apply_ledger_dispositions_backfills_rows(tmp_path):
    from ki_ops.kotl.enums import OrderStatus
    from ki_ops.kotl.trade_file import apply_ledger_dispositions, read_trade_file

    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=RESULTS
    )
    dest = tmp_path / "trades.csv"
    write_trade_file(rows, dest)

    orders = [
        _wo("FLEX-1", filled="30", status=OrderStatus.DONE, finalization="FINALIZED"),
        _wo(
            "FLEX-2",
            status=OrderStatus.CANCELLED,
            finalization="UNFINALIZED",
            cancel="CANCEL_ORIGINAL",
            reason="risk: restricted list",
        ),
    ]
    written, matched = apply_ledger_dispositions(dest, orders)
    assert matched == 2

    updated = {r["flex_order_id"]: r for r in read_trade_file(written)}
    assert updated["FLEX-1"]["final_status"] == "filled"
    assert updated["FLEX-1"]["filled_qty"] == "30"
    # Gateway verdict column is untouched…
    assert updated["FLEX-2"]["status"] == "rejected"
    # …but the disposition shows the truth: parked, not dead.
    assert updated["FLEX-2"]["final_status"] == "unfinalized"
    assert updated["FLEX-2"]["rejection_reason"] == "risk: restricted list"


def test_apply_ledger_dispositions_leaves_unmatched_rows(tmp_path):
    from ki_ops.kotl.trade_file import apply_ledger_dispositions, read_trade_file

    rows = build_trade_file_rows(
        trade_date=TD, submit_id="sub-1", payloads=PAYLOADS, results=RESULTS
    )
    dest = tmp_path / "trades.csv"
    write_trade_file(rows, dest)

    _, matched = apply_ledger_dispositions(dest, [])
    assert matched == 0
    updated = read_trade_file(dest)
    assert all(r["final_status"] == "" for r in updated)
    assert [r["status"] for r in updated] == ["submitted", "rejected"]
