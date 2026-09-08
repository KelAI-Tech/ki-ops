"""End-of-day loop: refresh fills, flatness check, immutable snapshot, fills CSV.

``ki-ops kotl eod`` runs after the close: pull the latest fill state (fixture
JSON / kelai ``get_orders`` export for now — live GetOrderInfo2 comes later),
rebuild the sent/done/left report, decide flatness with a share tolerance, and
freeze the day under ``<data-dir>/eod/<trade_date>/``:

- ``working_orders.csv`` — copy of the day's ledger rows as refreshed
- ``report.json`` — the full status report
- ``eod_fills_<trade_date>.csv`` — ``symbol,side,filled_qty,avg_fill_px``
  (non-zero fills only) for next-morning SOD reconciliation

Snapshots are immutable: a second run for the same date refuses to overwrite.
Exit contract at the CLI: ``0`` flat, ``3`` not flat (distinct from the
pre-trade gate's ``2`` = blocked).
"""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from ki_ops.kotl.models import WorkingOrder
from ki_ops.kotl.refresh import FlexRefreshSource, refresh_working_orders
from ki_ops.kotl.refresh_source import load_refresh_source
from ki_ops.kotl.report import StatusReport, build_status_report
from ki_ops.kotl.store import (
    DEFAULT_DATA_DIR,
    WORKING_ORDER_FIELDS,
    KotlStore,
    _working_order_to_row,
)

FILLS_FIELDS = ("symbol", "side", "filled_qty", "avg_fill_px")


def write_fills_csv(orders: Sequence[WorkingOrder], dest: Path) -> Path:
    """Non-zero fills → ``symbol,side,filled_qty,avg_fill_px`` (signed, as stored)."""
    with dest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FILLS_FIELDS)
        writer.writeheader()
        for o in sorted(orders, key=lambda x: (x.symbol, x.flex_order_id)):
            if o.filled_qty == 0:
                continue
            writer.writerow(
                {
                    "symbol": o.symbol,
                    "side": o.side,
                    "filled_qty": str(o.filled_qty),
                    "avg_fill_px": str(o.avg_fill_px) if o.avg_fill_px is not None else "",
                }
            )
    return dest


def run_eod(
    store: KotlStore,
    *,
    trade_date: date,
    fixture: str | Path | None = None,
    tolerance: Decimal = Decimal("0"),
    eod_dir: str | Path | None = None,
    source: FlexRefreshSource | None = None,
) -> tuple[dict[str, Any], StatusReport]:
    """Refresh → report → immutable snapshot under ``<eod_dir>/<trade_date>/``.

    The refresh comes from *source* when given (e.g. a live
    ``LiveRefreshSource``), else from the *fixture* path. Returns
    ``(summary, report)``; the summary is the CLI's JSON stdout.
    """
    if source is None:
        if fixture is None:
            raise ValueError("run_eod needs a fixture path or a refresh source")
        source = load_refresh_source(fixture)
    refreshed = refresh_working_orders(store, trade_date, source)
    orders = store.load_working_orders(trade_date=trade_date)
    report = build_status_report(orders, trade_date=trade_date, flat_tolerance=tolerance)

    base = (
        Path(eod_dir)
        if eod_dir is not None
        else Path(getattr(store, "data_dir", DEFAULT_DATA_DIR)) / "eod"
    )
    snap_dir = base / trade_date.isoformat()
    report_path = snap_dir / "report.json"
    if report_path.exists():
        raise FileExistsError(
            f"EOD snapshot already exists: {report_path} — snapshots are immutable; "
            "move the folder aside if you really need to regenerate"
        )
    snap_dir.mkdir(parents=True, exist_ok=True)

    orders_path = snap_dir / "working_orders.csv"
    with orders_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=WORKING_ORDER_FIELDS)
        writer.writeheader()
        for o in sorted(orders, key=lambda x: (x.symbol, x.flex_order_id)):
            writer.writerow(_working_order_to_row(o))

    report_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    fills_path = write_fills_csv(orders, snap_dir / f"eod_fills_{trade_date.isoformat()}.csv")

    summary: dict[str, Any] = {
        "command": "kotl-eod",
        "trade_date": trade_date.isoformat(),
        "fixture": str(fixture) if fixture is not None else "live",
        "refreshed_count": len(refreshed),
        "flat": report.flat,
        "flat_tolerance": str(tolerance),
        "open_count": report.open_count,
        "partial_count": report.partial_count,
        "done_count": report.done_count,
        "cancelled_count": report.cancelled_count,
        "total_abs_leaves": str(report.total_abs_leaves),
        "eod_dir": str(snap_dir),
        "report_json": str(report_path),
        "working_orders_csv": str(orders_path),
        "fills_csv": str(fills_path),
    }
    return summary, report
