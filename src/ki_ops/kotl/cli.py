"""CLI handlers for ``ki-ops kotl``."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ki_ops.kotl.refresh import refresh_working_orders
from ki_ops.kotl.refresh_source import load_refresh_source
from ki_ops.kotl.report import build_status_report, format_status_table
from ki_ops.kotl.store import DEFAULT_DATA_DIR, KotlStore
from ki_ops.kotl.submit import submit_rebalance_csv

# Exit codes (0 ok, 2 pre-trade gate blocked, 3 EOD not flat are taken):
EXIT_RECON_DIVERGENCE = 4
EXIT_SUBMIT_REFUSED = 5
EXIT_UNRESOLVED_SECURITIES = 6

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOD = ROOT / "examples" / "sod_positions.csv"
DEFAULT_TARGETS = ROOT / "examples" / "target_intents.csv"
DEFAULT_FIXTURE = ROOT / "examples" / "kotl" / "refresh_partial.json"


def _add_store_args(parser) -> None:
    parser.add_argument(
        "--store",
        choices=("csv", "mysql"),
        default="csv",
        help="ledger backend (default csv under --data-dir; mysql needs ki-ops[db])",
    )
    parser.add_argument(
        "--db-secret",
        default=None,
        help="Secrets Manager secret id with MySQL creds (e.g. dev/kelaidb); "
        "KOTL_DB_HOST/PORT/USER/PASSWORD/SCHEMA env vars take priority",
    )
    parser.add_argument(
        "--db-schema",
        default=None,
        help="MySQL schema (e.g. kelai, kelai_canary); overrides env/secret",
    )


def _add_live_source_args(parser) -> None:
    parser.add_argument(
        "--source",
        choices=("fixture", "live"),
        default="fixture",
        help="fill source: fixture file (default) or live Flex GetOrderInfo2",
    )
    parser.add_argument(
        "--flex-env",
        choices=("UAT", "PROD"),
        default="UAT",
        help="Flex environment for --source live (default UAT)",
    )


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
        help="submit from kelaidata shares trade file (S3 CSV) + ds2 H5 prices "
        "(fake by default; --flex-env UAT/PROD goes live)",
    )
    sk.add_argument("--trade-date", type=date.fromisoformat, required=True)
    sk.add_argument(
        "--shares",
        default=None,
        help="shares trade file, s3:// or local (default: "
        "s3://kelaitrading/portfolio/shares/[<strategy-id>/]Portfolio_<YYYYMMDD>.csv)",
    )
    sk.add_argument(
        "--ds2",
        default=None,
        help="ds2 H5, s3:// or local (default: s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5)",
    )
    sk.add_argument(
        "--strategy-id",
        default=None,
        help="pipeline strategy subfolder (e.g. USATop2000_neutralized); also "
        "routes the trade file to s3://kelaitrading/trades/<strategy-id>/…",
    )
    sk.add_argument(
        "--sod-source",
        choices=("flex", "prior-target", "csv", "flat"),
        default=None,
        help="SOD book source: flex = live ReplayPositions (with recon guard), "
        "prior-target = yesterday's Portfolio_*.csv, csv = --sod file, flat = no book; "
        "legacy --sod/--assume-flat-sod map to csv/flat",
    )
    sod_group = sk.add_mutually_exclusive_group(required=False)
    sod_group.add_argument("--sod", type=Path, default=None, help="SOD CSV (ticker, quantity, market_price)")
    sod_group.add_argument(
        "--assume-flat-sod",
        action="store_true",
        help="no SOD book: trade the full target file from flat",
    )
    sk.add_argument(
        "--flex-env",
        choices=("FAKE", "UAT", "PROD"),
        default="FAKE",
        help="FAKE (default, offline adapter) or UAT/PROD via the live gRPC adapter",
    )
    sk.add_argument(
        "--dry-run",
        action="store_true",
        help="build + print orders and write the trade file; no gRPC, no ledger write",
    )
    sk.add_argument(
        "--force",
        action="store_true",
        help="allow a second live submission attempt for the same (trade-date, env) "
        "past the once-a-day claim — target mode still caps the send to the "
        "residual (target − already-sent), so a forced re-run can never resend "
        "what already went out",
    )
    sk.add_argument(
        "--sent-source",
        choices=("ledger", "flex"),
        default="ledger",
        help="where 'already sent today' comes from for the target-mode residual: "
        "the KOTL ledger (default, cross-checked against live Flex orders) or "
        "flex (recovery when the ledger lost a write: recompute from live "
        "GetOrderInfo2, KOTL-stamped orders only; the target cap still applies)",
    )
    sk.add_argument(
        "--recon-max-shares",
        type=Decimal,
        default=None,
        help="flex SOD recon: abort when total |Flex − prior target| shares exceed N "
        "(default 0 = any divergence aborts; env KOTL_RECON_MAX_SHARES)",
    )
    sk.add_argument(
        "--recon-max-names",
        type=int,
        default=None,
        help="flex SOD recon: abort when more than N names diverge "
        "(default 0; env KOTL_RECON_MAX_NAMES)",
    )
    sk.add_argument(
        "--max-orders",
        type=int,
        default=None,
        help="refuse before CreateOrders above this order count "
        "(default 5000; env KOTL_MAX_ORDERS)",
    )
    sk.add_argument(
        "--max-gross-notional",
        type=Decimal,
        default=None,
        help="refuse before CreateOrders above this gross $ notional at ds2 prices "
        "(default 100000000; env KOTL_MAX_GROSS_NOTIONAL)",
    )
    sk.add_argument(
        "--unresolved",
        choices=("block", "skip"),
        default="block",
        help="symbols missing from the Flex security master (pre-submit "
        "SecurityService lookup, live envs): block the submit (default, exit 6) "
        "or skip them and submit resolved names only; the unresolved list is "
        "always written as unresolved_<submit_id>.csv next to the trade file",
    )
    sk.add_argument(
        "--sedol-source",
        default=None,
        help="book SEDOL map for pre-submit lookup (SEDOL is FlexTrade's "
        "preferred identifier): 'snowflake' (default; daily kelai security "
        "master KELAI.LSEG[_CANARY].SECURITY_MASTER_DT, schema by env), a CSV "
        "path/s3:// URL with infocode,sedol columns, or 'none' for "
        "symbol-only resolution (env KOTL_SEDOL_SOURCE)",
    )
    sk.add_argument(
        "--trade-file-out",
        default=None,
        help="trade file destination, local path or s3:// URL (default: "
        "s3://kelaitrading/trades/<strategy-id>/<yyyymmdd>/trades_<submit_id>.csv "
        "when --strategy-id is given, else <data-dir>/trades/<yyyymmdd>/…)",
    )
    sk.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    sk.add_argument("--cache-dir", type=Path, default=None, help="S3 download cache (default: data/kotl/cache)")
    _add_store_args(sk)

    rf = ks.add_parser(
        "refresh",
        help="refresh from KOTL fills JSON / kelai get_orders export, or live GetOrderInfo2",
    )
    rf.add_argument("--trade-date", type=date.fromisoformat, required=True)
    rf.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    rf.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    _add_live_source_args(rf)
    _add_store_args(rf)

    st = ks.add_parser("status", help="sent / done / left report")
    st.add_argument("--trade-date", type=date.fromisoformat, required=True)
    st.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    st.add_argument("--json", action="store_true", help="JSON instead of table")
    _add_store_args(st)

    eod = ks.add_parser(
        "eod",
        help="end-of-day: refresh, flatness check, immutable snapshot + fills CSV (exit 3 when not flat)",
    )
    eod.add_argument("--trade-date", type=date.fromisoformat, required=True)
    eod.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="refresh source: KOTL fills JSON or kelai get_orders export (--source live "
        "uses GetOrderInfo2 instead)",
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
    _add_live_source_args(eod)
    _add_store_args(eod)


def _build_store(args):
    if getattr(args, "store", "csv") == "mysql":
        from ki_ops.kotl.mysql_store import MysqlKotlStore

        return MysqlKotlStore.from_env_or_secret(
            db_secret=getattr(args, "db_secret", None),
            db_schema=getattr(args, "db_schema", None),
        )
    return KotlStore(args.data_dir)


def _build_refresh_source(args):
    """fixture path (default) vs live GetOrderInfo2."""
    if getattr(args, "source", "fixture") == "live":
        from ki_ops.kotl.flex_live import LiveRefreshSource, load_flex_config

        return LiveRefreshSource(load_flex_config(flex_env=getattr(args, "flex_env", "UAT"))), None
    return load_refresh_source(args.fixture), args.fixture


def run_kotl(args) -> int:
    store = _build_store(args)
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
        from ki_ops.kotl.submit import (
            ReconDivergenceError,
            SubmitRefusedError,
            UnresolvedSecuritiesError,
            submit_kelai_shares,
        )

        flex_env = getattr(args, "flex_env", "FAKE")
        adapter = None
        if flex_env in ("UAT", "PROD"):
            from ki_ops.kotl.flex_live import LiveFlexAdapter, load_flex_config

            adapter = LiveFlexAdapter(load_flex_config(flex_env=flex_env))

        try:
            submit = submit_kelai_shares(
                store,
                trade_date=args.trade_date,
                shares_file=args.shares,
                ds2_h5=args.ds2,
                sod_csv=args.sod,
                assume_flat_sod=args.assume_flat_sod,
                sod_source=getattr(args, "sod_source", None),
                strategy_id=getattr(args, "strategy_id", None),
                env=flex_env,
                adapter=adapter,
                cache_dir=args.cache_dir,
                recon_max_shares=getattr(args, "recon_max_shares", None),
                recon_max_names=getattr(args, "recon_max_names", None),
                dry_run=getattr(args, "dry_run", False),
                force=getattr(args, "force", False),
                max_orders=getattr(args, "max_orders", None),
                max_gross_notional=getattr(args, "max_gross_notional", None),
                trade_file_out=getattr(args, "trade_file_out", None),
                unresolved=getattr(args, "unresolved", "block"),
                sedol_source=getattr(args, "sedol_source", None),
                sent_source=getattr(args, "sent_source", "ledger"),
            )
        except ReconDivergenceError as exc:
            print(f"RECON BLOCKED: {exc}")
            return EXIT_RECON_DIVERGENCE
        except UnresolvedSecuritiesError as exc:
            print(f"SUBMIT BLOCKED: {exc}")
            return EXIT_UNRESOLVED_SECURITIES
        except SubmitRefusedError as exc:
            print(f"SUBMIT REFUSED: {exc}")
            return EXIT_SUBMIT_REFUSED

        print(
            json.dumps(
                {
                    "submit_id": submit.submit_id,
                    "trade_date": args.trade_date.isoformat(),
                    "env": flex_env,
                    "dry_run": getattr(args, "dry_run", False),
                    "target_covered": bool(
                        (submit.flex_response or {}).get("target_covered")
                    ),
                    "shares_file": args.shares or "s3 default",
                    "order_count": len(submit.payload),
                    "flex_order_ids": list(submit.flex_order_ids),
                },
                indent=2,
            )
        )
        return 0

    if cmd == "refresh":
        source, fixture = _build_refresh_source(args)
        updated = refresh_working_orders(store, args.trade_date, source)
        print(
            json.dumps(
                {
                    "trade_date": args.trade_date.isoformat(),
                    "updated_count": len(updated),
                    "source": getattr(args, "source", "fixture"),
                    "fixture": str(fixture) if fixture is not None else "live",
                },
                indent=2,
            )
        )
        return 0

    if cmd == "eod":
        from ki_ops.kotl.eod import run_eod

        source, fixture = _build_refresh_source(args)
        summary, report = run_eod(
            store,
            trade_date=args.trade_date,
            fixture=fixture,
            source=source,
            tolerance=Decimal(args.tolerance),
            eod_dir=args.eod_dir or (Path(args.data_dir) / "eod" if getattr(args, "store", "csv") == "mysql" else None),
        )
        if hasattr(store, "append_eod_snapshot"):
            store.append_eod_snapshot(args.trade_date, summary)
            summary["eod_snapshot_store"] = "mysql"
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
