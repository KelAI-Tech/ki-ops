"""CLI handlers for ``ki-ops kotl``."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from ki_ops.kotl.refresh import refresh_working_orders
from ki_ops.kotl.refresh_source import load_refresh_source
from ki_ops.kotl.report import build_status_report, format_status_table
from ki_ops.kotl.store import DEFAULT_DATA_DIR, KotlStore
from ki_ops.kotl.submit import submit_rebalance_csv

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOD = ROOT / "examples" / "sod_positions.csv"
DEFAULT_TARGETS = ROOT / "examples" / "target_intents.csv"
DEFAULT_FIXTURE = ROOT / "examples" / "kotl" / "refresh_partial.json"


def register_kotl_parser(sub) -> None:
    kotl = sub.add_parser("kotl", help="Order Tracking Ledger (sent / done / left)")
    ks = kotl.add_subparsers(dest="kotl_command", required=True)

    sr = ks.add_parser("submit-rebalance", help="offline fake submit from SOD + targets CSV")
    sr.add_argument("--sod", type=Path, default=DEFAULT_SOD)
    sr.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    sr.add_argument("--trade-date", type=date.fromisoformat, required=True)
    sr.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    rf = ks.add_parser("refresh", help="refresh from KOTL fills JSON or kelai get_orders export")
    rf.add_argument("--trade-date", type=date.fromisoformat, required=True)
    rf.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    rf.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    st = ks.add_parser("status", help="sent / done / left report")
    st.add_argument("--trade-date", type=date.fromisoformat, required=True)
    st.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    st.add_argument("--json", action="store_true", help="JSON instead of table")


def run_kotl(args) -> int:
    store = KotlStore(args.data_dir)
    cmd = args.kotl_command

    if cmd == "submit-rebalance":
        submit = submit_rebalance_csv(
            store,
            args.sod,
            args.targets,
            trade_date=args.trade_date,
        )
        print(
            json.dumps(
                {
                    "submit_id": submit.submit_id,
                    "trade_date": args.trade_date.isoformat(),
                    "order_count": len(submit.flex_order_ids),
                    "flex_order_ids": list(submit.flex_order_ids),
                },
                indent=2,
            )
        )
        return 0

    if cmd == "refresh":
        updated = refresh_working_orders(
            store,
            args.trade_date,
            load_refresh_source(args.fixture),
        )
        print(
            json.dumps(
                {
                    "trade_date": args.trade_date.isoformat(),
                    "updated_count": len(updated),
                    "fixture": str(args.fixture),
                },
                indent=2,
            )
        )
        return 0

    if cmd == "status":
        orders = store.load_working_orders(trade_date=args.trade_date)
        report = build_status_report(orders, trade_date=args.trade_date)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(format_status_table(report))
        return 0

    raise SystemExit(f"unknown kotl command: {cmd}")
