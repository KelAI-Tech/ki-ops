"""CLI handlers for ``ki-ops kotl``."""

from __future__ import annotations

import argparse
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

    sk = ks.add_parser(
        "submit-kelai",
        help="fake submit from kelaidata shares trade file (S3 CSV) + ds2 H5 prices",
    )
    sk.add_argument("--trade-date", type=date.fromisoformat, required=True)
    sk.add_argument(
        "--shares",
        default=None,
        help="shares trade file, s3:// or local "
        "(default: s3://kelaitrading/portfolio/shares/Portfolio_<YYYYMMDD>.csv)",
    )
    sk.add_argument(
        "--ds2",
        default=None,
        help="ds2 H5, s3:// or local (default: s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5)",
    )
    sod_group = sk.add_mutually_exclusive_group(required=True)
    sod_group.add_argument("--sod", type=Path, default=None, help="SOD CSV (ticker, quantity, market_price)")
    sod_group.add_argument(
        "--assume-flat-sod",
        action="store_true",
        help="no SOD book: trade the full target file from flat",
    )
    sk.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    sk.add_argument("--cache-dir", type=Path, default=None, help="S3 download cache (default: data/kotl/cache)")

    rf = ks.add_parser("refresh", help="refresh from KOTL fills JSON or kelai get_orders export")
    rf.add_argument("--trade-date", type=date.fromisoformat, required=True)
    rf.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    rf.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    st = ks.add_parser("status", help="sent / done / left report")
    st.add_argument("--trade-date", type=date.fromisoformat, required=True)
    st.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    st.add_argument("--json", action="store_true", help="JSON instead of table")

    eod = ks.add_parser(
        "eod",
        help="end-of-day: refresh, flatness check, immutable snapshot + fills CSV (exit 3 when not flat)",
    )
    eod.add_argument("--trade-date", type=date.fromisoformat, required=True)
    eod.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="refresh source: KOTL fills JSON or kelai get_orders export (live GetOrderInfo2 later)",
    )
    eod.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    eod.add_argument(
        "--tolerance",
        default="0",
        help="share tolerance for flatness: flat when sum(|leaves|) <= N (default 0)",
    )
    eod.add_argument("--json", action="store_true", help="summary JSON only (default: table + summary)")
    eod.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="email/Slack the summary via the notify-perturbs plumbing (config/notify.env)",
    )
    eod.add_argument(
        "--eod-dir",
        type=Path,
        default=None,
        help="snapshot root (default: <data-dir>/eod); one immutable folder per trade date",
    )


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

    if cmd == "submit-kelai":
        from ki_ops.kotl.submit import submit_kelai_shares

        submit = submit_kelai_shares(
            store,
            trade_date=args.trade_date,
            shares_file=args.shares,
            ds2_h5=args.ds2,
            sod_csv=args.sod,
            assume_flat_sod=args.assume_flat_sod,
            cache_dir=args.cache_dir,
        )
        print(
            json.dumps(
                {
                    "submit_id": submit.submit_id,
                    "trade_date": args.trade_date.isoformat(),
                    "shares_file": args.shares or "s3 default",
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

    if cmd == "eod":
        from decimal import Decimal

        from ki_ops.kotl.eod import run_eod

        summary, report = run_eod(
            store,
            trade_date=args.trade_date,
            fixture=args.fixture,
            tolerance=Decimal(args.tolerance),
            eod_dir=args.eod_dir,
        )
        if args.notify:
            from ki_ops.extras.notify import dispatch, load_notify_settings

            subject = (
                f"ki-ops kotl eod {args.trade_date.isoformat()}: flat={summary['flat']} "
                f"total_abs_leaves={summary['total_abs_leaves']}"
            )
            try:
                summary["notify"] = dispatch(
                    settings=load_notify_settings(),
                    subject=subject,
                    body=json.dumps(summary, indent=2),
                )
            except Exception as exc:  # notify must never flip the EOD verdict
                summary["notify"] = {"error": str(exc)}
        if not args.json:
            print(format_status_table(report))
        print(json.dumps(summary, indent=2))
        return 0 if summary["flat"] else 3

    if cmd == "status":
        orders = store.load_working_orders(trade_date=args.trade_date)
        report = build_status_report(orders, trade_date=args.trade_date)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(format_status_table(report))
        return 0

    raise SystemExit(f"unknown kotl command: {cmd}")
