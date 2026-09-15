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
EXIT_MARKET_CLOSED = 7
# snapshot-book: the nightly book moved in a way the ledger does not explain
# (manual trades? missed fills?) — the snapshot is still recorded, but someone
# should look.
EXIT_BOOK_AUDIT_DRIFT = 8

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


def _add_submit_kelai_args(parser, *, resend: bool = False) -> None:
    """Shared options of ``submit-kelai`` and ``resend``.

    ``resend`` runs the same pipeline scoped to specific tickers with force
    implied, so it drops ``--force`` (always on) and ``--sent-source`` (a
    forced live run always recomputes already-sent from live Flex state) and
    flips the ``--unresolved`` default to ``skip`` (a retried name that is
    STILL missing from the master should not block the rest of the scope).
    """
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--shares",
        default=None,
        help="shares trade file, s3:// or local (default: "
        "s3://kelaitrading/portfolio/shares/[<strategy-id>/]Portfolio_<YYYYMMDD>.csv)",
    )
    parser.add_argument(
        "--ds2",
        default=None,
        help="ds2 H5, s3:// or local (default: s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5)",
    )
    parser.add_argument(
        "--strategy-id",
        default=None,
        help="pipeline strategy subfolder (e.g. USATop2000_neutralized); also "
        "routes the trade file to s3://kelaitrading/trades/<strategy-id>/…",
    )
    parser.add_argument(
        "--sod-source",
        choices=("flex", "prior-target", "csv", "flat"),
        default=None,
        help="SOD book source: flex = live ReplayPositions (with recon guard), "
        "prior-target = yesterday's Portfolio_*.csv, csv = --sod file, flat = no book; "
        "legacy --sod/--assume-flat-sod map to csv/flat",
    )
    sod_group = parser.add_mutually_exclusive_group(required=False)
    sod_group.add_argument("--sod", type=Path, default=None, help="SOD CSV (ticker, quantity, market_price)")
    sod_group.add_argument(
        "--assume-flat-sod",
        action="store_true",
        help="no SOD book: trade the full target file from flat",
    )
    parser.add_argument(
        "--flex-env",
        choices=("FAKE", "UAT", "PROD"),
        default="FAKE",
        help="FAKE (default, offline adapter) or UAT/PROD via the live gRPC adapter",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build + print orders and write the trade file; no gRPC, no ledger write",
    )
    if not resend:
        parser.add_argument(
            "--force",
            action="store_true",
            help="allow a second live submission attempt for the same (trade-date, env) "
            "past the once-a-day claim — target mode still caps the send to the "
            "residual (target − already-sent), so a forced re-run can never resend "
            "what already went out. On a live env the already-sent source is always "
            "flex (live GetOrderInfo2 state, overriding --sent-source): working "
            "orders count in full, and cancelled orders count only their final "
            "fills so the confirmed-dead remainder can be resent",
        )
    parser.add_argument(
        "--no-route",
        action="store_true",
        help="safe live test (FlexTrade-confirmed): send every order with blank "
        "broker/algo and NO_AUTOMATION — Flex accepts and books them but nothing "
        "goes to the street (no fills, no PnL). Still a real submit: claim taken, "
        "ledger written, and target mode counts the staged orders as sent, so "
        "routed trading for this (trade-date, env) is consumed for the day; "
        "cancel the staged orders in the Flex UI or let GFD expire them",
    )
    parser.add_argument(
        "--allow-outside-market-hours",
        action="store_true",
        help="override the NYSE market-hours gate (live submits are otherwise "
        "refused outside trading days 03:00 ET to the close, exit 7) — "
        "deliberate testing only",
    )
    if not resend:
        parser.add_argument(
            "--sent-source",
            choices=("ledger", "flex"),
            default="ledger",
            help="where 'already sent today' comes from for the target-mode residual: "
            "the KOTL ledger (default, cross-checked against live Flex orders) or "
            "flex (recovery when the ledger lost a write: recompute from live "
            "GetOrderInfo2, KOTL-stamped orders only; the target cap still applies). "
            "--force on a live env always uses flex regardless of this flag",
        )
    parser.add_argument(
        "--recon-max-shares",
        type=Decimal,
        default=None,
        help="flex SOD recon: abort when total |Flex − prior target| shares exceed N "
        "(default 0 = any divergence aborts; env KOTL_RECON_MAX_SHARES)",
    )
    parser.add_argument(
        "--recon-max-names",
        type=int,
        default=None,
        help="flex SOD recon: abort when more than N names diverge "
        "(default 0; env KOTL_RECON_MAX_NAMES)",
    )
    parser.add_argument(
        "--max-orders",
        type=int,
        default=None,
        help="refuse before CreateOrders above this order count "
        "(default 5000; env KOTL_MAX_ORDERS)",
    )
    parser.add_argument(
        "--max-gross-notional",
        type=Decimal,
        default=None,
        help="refuse before CreateOrders above this gross $ notional at ds2 prices "
        "(default 100000000; env KOTL_MAX_GROSS_NOTIONAL)",
    )
    parser.add_argument(
        "--unresolved",
        choices=("block", "skip"),
        default="skip" if resend else "block",
        help="symbols missing from the Flex security master (pre-submit "
        "SecurityService lookup, live envs): block the submit (exit 6) "
        "or skip them and submit resolved names only; the unresolved list is "
        "always written as unresolved_<submit_id>.csv next to the trade file"
        + (
            " (default skip: a retried name still missing from the master "
            "does not block the rest of the scope)"
            if resend
            else " (default block)"
        ),
    )
    parser.add_argument(
        "--sedol-source",
        default=None,
        help="book SEDOL map for pre-submit lookup (SEDOL is FlexTrade's "
        "preferred identifier): 'snowflake' (default; daily kelai security "
        "master KELAI.LSEG[_CANARY].SECURITY_MASTER_DT, schema by env), a CSV "
        "path/s3:// URL with infocode,sedol columns, or 'none' for "
        "symbol-only resolution (env KOTL_SEDOL_SOURCE)",
    )
    parser.add_argument(
        "--trade-file-out",
        default=None,
        help="trade file destination, local path or s3:// URL (default: "
        "s3://kelaitrading/trades/<strategy-id>/<yyyymmdd>/trades_<submit_id>.csv "
        "when --strategy-id is given, else <data-dir>/trades/<yyyymmdd>/…)",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--cache-dir", type=Path, default=None, help="S3 download cache (default: data/kotl/cache)")
    _add_store_args(parser)


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
    _add_submit_kelai_args(sk)

    rs = ks.add_parser(
        "resend",
        help="re-send specific tickers for a day already submitted (forced, "
        "residual-capped): unresolved names skipped earlier "
        "(--retry-unresolved) or an operator-picked --ticker after a cancel",
    )
    rs.add_argument(
        "--ticker",
        action="append",
        default=None,
        metavar="TICKER",
        help="re-send this ds2 ticker only (repeatable; AAPL and AAPL.US both "
        "match) — every named ticker must have a residual trade today, or "
        "the resend refuses (exit 5)",
    )
    rs.add_argument(
        "--retry-unresolved",
        default=None,
        metavar="CSV",
        help="unresolved_<submit_id>.csv (local or s3://) written by a prior "
        "submit — re-send its tickers now that FlexTrade seeded the master; "
        "tickers that dropped out of today's target only warn",
    )
    _add_submit_kelai_args(rs, resend=True)

    sb = ks.add_parser(
        "snapshot-book",
        help="nightly portfolio snapshot: live Flex book (ReplayPositions) → store; "
        "next morning's SOD recon baseline. Audits book vs previous snapshot + "
        "ledger fills (exit 8 on unexplained drift; snapshot recorded either way)",
    )
    sb.add_argument(
        "--flex-env",
        choices=("UAT", "PROD"),
        default="UAT",
        help="Flex environment to snapshot (default UAT)",
    )
    sb.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="book as-of date (default: today in America/New_York — run after the close)",
    )
    sb.add_argument(
        "--audit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="check book == previous snapshot + ledger fills since (refreshing "
        "the ledger from live GetOrderInfo2 first); --no-audit skips",
    )
    sb.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + audit but record nothing",
    )
    sb.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    _add_store_args(sb)

    rf = ks.add_parser(
        "refresh",
        help="refresh from KOTL fills JSON / kelai get_orders export, or live GetOrderInfo2",
    )
    rf.add_argument("--trade-date", type=date.fromisoformat, required=True)
    rf.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    rf.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    rf.add_argument(
        "--trade-file-out",
        default=None,
        help="existing trade file (local path or s3://) to back-fill with ledger "
        "dispositions after the refresh: filled_qty + final_status (filled/partial/"
        "working/unfinalized/cancel_pending/cancelled) + raw Flex workflow columns "
        "— the create-time `status` column is the gateway verdict only",
    )
    _add_live_source_args(rf)
    _add_store_args(rf)

    st = ks.add_parser("status", help="sent / done / left report")
    st.add_argument("--trade-date", type=date.fromisoformat, required=True)
    st.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    st.add_argument("--json", action="store_true", help="JSON instead of table")
    _add_store_args(st)

    fl = ks.add_parser(
        "fills",
        help="latest fills from the KOTL ledger "
        "(env-aware: canary/prod via KI_OPS_ENV, default canary)",
    )
    fl.add_argument(
        "--ki-env",
        choices=("canary", "prod"),
        default=None,
        help="ops environment (default: KI_OPS_ENV env var, else canary); picks "
        "the ledger db secret (kelai/kotl/db-canary|db-prod) and the Flex env for --live",
    )
    fl.add_argument(
        "--trade-date",
        type=date.fromisoformat,
        default=None,
        help="default: latest trade date in the ledger",
    )
    fl.add_argument(
        "--ticker",
        default=None,
        help="show one symbol only (AAPL and AAPL.US both match)",
    )
    fl.add_argument("--limit", type=int, default=None, help="show at most N rows")
    fl.add_argument("--json", action="store_true", help="JSON instead of table")
    fl.add_argument(
        "--no-pager",
        action="store_true",
        help="never page through less (tables longer than the terminal page by default)",
    )
    fl.add_argument(
        "--live",
        action="store_true",
        help="merge live Flex GetOrderInfo2 state over the ledger rows for "
        "display (read-only — the ledger is not written; use kotl refresh for that)",
    )
    fl.add_argument(
        "--flex-env",
        choices=("UAT", "PROD"),
        default=None,
        help="Flex environment for --live (default: KOTL_FLEX_ENV, else the "
        "KI_OPS_ENV preset: canary→UAT, prod→PROD)",
    )
    fl.add_argument(
        "--store",
        choices=("csv", "mysql"),
        default=None,
        help="ledger backend (default: mysql with the env's db secret; "
        "csv reads --data-dir for local use)",
    )
    fl.add_argument(
        "--db-secret",
        default=None,
        help="Secrets Manager secret id (default: the KI_OPS_ENV preset's; "
        "KOTL_DB_* env vars take priority as usual)",
    )
    fl.add_argument("--db-schema", default=None, help="MySQL schema (default: kotl)")
    fl.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

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


def _build_fills_store(args, ops_env):
    """Fills default to the env preset's MySQL ledger; ``--store csv`` opts out.

    Precedence: explicit flags > ``KOTL_DB_*`` env vars (inside
    ``resolve_db_params``: ``KOTL_DB_HOST`` bypasses the secret entirely) >
    the ``KI_OPS_ENV`` preset.
    """
    import os

    if getattr(args, "store", None) == "csv":
        return KotlStore(args.data_dir)
    from ki_ops.kotl.mysql_store import MysqlKotlStore

    db_secret = getattr(args, "db_secret", None) or ops_env.kotl_db_secret
    db_schema = (
        getattr(args, "db_schema", None)
        or os.environ.get("KOTL_DB_SCHEMA")
        or ops_env.kotl_db_schema
    )
    return MysqlKotlStore.from_env_or_secret(db_secret=db_secret, db_schema=db_schema)


def _emit_paged(text: str, *, no_pager: bool = False) -> None:
    """Print, paging through ``less`` when interactive and taller than the terminal."""
    import shutil
    import subprocess
    import sys

    if no_pager or not sys.stdout.isatty():
        print(text)
        return
    if text.count("\n") + 2 <= shutil.get_terminal_size().lines:
        print(text)
        return
    try:
        subprocess.run(["less", "-RS"], input=text.encode("utf-8"), check=False)
    except (FileNotFoundError, OSError):
        print(text)


def _run_fills(args) -> int:
    from ki_ops.kotl.flex_map import flex_symbol
    from ki_ops.kotl.report import fills_rows, fills_to_dicts, format_fills_table
    from ki_ops.opsenv import resolve_flex_env, resolve_ops_env

    ops_env = resolve_ops_env(getattr(args, "ki_env", None))
    store = _build_fills_store(args, ops_env)

    trade_date = args.trade_date or store.latest_trade_date()
    if trade_date is None:
        if args.json:
            print(json.dumps({"env": ops_env.name, "trade_date": None, "fills": []}, indent=2))
        else:
            print(f"KOTL fills (env={ops_env.name}): ledger is empty — nothing to show")
        return 0

    orders = store.load_working_orders(trade_date=trade_date)

    note = f"env={ops_env.name}"
    if args.live:
        from ki_ops.kotl.flex_live import LiveRefreshSource, load_flex_config
        from ki_ops.kotl.refresh import merge_order_snapshots

        flex_env = resolve_flex_env(getattr(args, "flex_env", None), ops_env)
        source = LiveRefreshSource(load_flex_config(flex_env=flex_env))
        snapshots = source.fetch_orders(trade_date.isoformat(), stored=orders)
        by_id = {o.flex_order_id: o for o in orders}
        for updated in merge_order_snapshots(orders, snapshots):
            by_id[updated.flex_order_id] = updated
        orders = list(by_id.values())
        note = f"{note}, live flex {flex_env}"

    if args.ticker:
        want = flex_symbol(args.ticker)
        orders = [o for o in orders if flex_symbol(o.symbol) == want]
        note = f"{note}, ticker {want}"

    rows = fills_to_dicts(orders)
    if args.limit is not None and args.limit >= 0:
        rows = rows[: args.limit]

    if args.json:
        print(
            json.dumps(
                {
                    "env": ops_env.name,
                    "trade_date": trade_date.isoformat(),
                    "live": bool(args.live),
                    "count": len(rows),
                    "fills": rows,
                },
                indent=2,
            )
        )
        return 0

    shown = orders
    if args.limit is not None and args.limit >= 0:
        shown = fills_rows(orders)[: args.limit]
    _emit_paged(
        format_fills_table(shown, trade_date=trade_date, header_note=note),
        no_pager=getattr(args, "no_pager", False),
    )
    return 0


def _build_refresh_source(args):
    """fixture path (default) vs live GetOrderInfo2."""
    if getattr(args, "source", "fixture") == "live":
        from ki_ops.kotl.flex_live import LiveRefreshSource, load_flex_config

        return LiveRefreshSource(load_flex_config(flex_env=getattr(args, "flex_env", "UAT"))), None
    return load_refresh_source(args.fixture), args.fixture


def run_kotl(args) -> int:
    cmd = args.kotl_command
    if cmd == "fills":
        # fills resolves its own store (env-preset MySQL default, not csv)
        return _run_fills(args)

    store = _build_store(args)

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

    if cmd in ("submit-kelai", "resend"):
        from ki_ops.kotl.submit import (
            MarketClosedError,
            ReconDivergenceError,
            SubmitRefusedError,
            UnresolvedSecuritiesError,
            submit_kelai_shares,
        )

        resend = cmd == "resend"
        only_tickers = list(getattr(args, "ticker", None) or [])
        retry_unresolved = getattr(args, "retry_unresolved", None)
        if resend and not only_tickers and not retry_unresolved:
            print(
                "resend needs a scope: pass --ticker TICKER (repeatable) and/or "
                "--retry-unresolved <unresolved_<submit_id>.csv>"
            )
            return 2
        if resend:
            print(
                "RESEND: forced re-submit scoped to "
                + ", ".join(only_tickers + ([str(retry_unresolved)] if retry_unresolved else []))
                + " — target mode caps every send to the residual; working and "
                "unfinalized orders stay fully protected, only never-sent "
                "quantity and confirmed-cancelled remainders go out"
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
                force=getattr(args, "force", False) or resend,
                max_orders=getattr(args, "max_orders", None),
                max_gross_notional=getattr(args, "max_gross_notional", None),
                trade_file_out=getattr(args, "trade_file_out", None),
                unresolved=getattr(args, "unresolved", "block"),
                sedol_source=getattr(args, "sedol_source", None),
                sent_source=getattr(args, "sent_source", "ledger"),
                allow_outside_market_hours=getattr(
                    args, "allow_outside_market_hours", False
                ),
                no_route=getattr(args, "no_route", False),
                only_tickers=only_tickers or None,
                retry_unresolved=retry_unresolved,
            )
        except ReconDivergenceError as exc:
            print(f"RECON BLOCKED: {exc}")
            return EXIT_RECON_DIVERGENCE
        except MarketClosedError as exc:
            print(f"SUBMIT BLOCKED: {exc}")
            return EXIT_MARKET_CLOSED
        except UnresolvedSecuritiesError as exc:
            print(f"SUBMIT BLOCKED: {exc}")
            return EXIT_UNRESOLVED_SECURITIES
        except SubmitRefusedError as exc:
            print(f"SUBMIT REFUSED: {exc}")
            return EXIT_SUBMIT_REFUSED

        summary = {
            "submit_id": submit.submit_id,
            "trade_date": args.trade_date.isoformat(),
            "env": flex_env,
            "dry_run": getattr(args, "dry_run", False),
            "no_route": getattr(args, "no_route", False),
            "target_covered": bool(
                (submit.flex_response or {}).get("target_covered")
            ),
            "shares_file": args.shares or "s3 default",
            "order_count": len(submit.payload),
            "flex_order_ids": list(submit.flex_order_ids),
        }
        if resend:
            summary["resend"] = True
            summary["scope"] = {
                "tickers": only_tickers,
                "retry_unresolved": retry_unresolved,
            }
        print(json.dumps(summary, indent=2))
        return 0

    if cmd == "snapshot-book":
        from zoneinfo import ZoneInfo

        from ki_ops.kotl.book_snapshot import snapshot_flex_book
        from ki_ops.kotl.flex_live import LiveRefreshSource, load_flex_config

        flex_env = getattr(args, "flex_env", "UAT")
        flex_config = load_flex_config(flex_env=flex_env)
        as_of = args.as_of or datetime.now(ZoneInfo("America/New_York")).date()
        summary = snapshot_flex_book(
            store,
            env=flex_env,
            as_of=as_of,
            flex_config=flex_config,
            refresh_source=LiveRefreshSource(flex_config) if args.audit else None,
            audit=args.audit,
            dry_run=getattr(args, "dry_run", False),
        )
        print(json.dumps(summary, indent=2))
        audit = summary.get("audit")
        return EXIT_BOOK_AUDIT_DRIFT if audit is not None and not audit["ok"] else 0

    if cmd == "refresh":
        source, fixture = _build_refresh_source(args)
        updated = refresh_working_orders(store, args.trade_date, source)
        summary = {
            "trade_date": args.trade_date.isoformat(),
            "updated_count": len(updated),
            "source": getattr(args, "source", "fixture"),
            "fixture": str(fixture) if fixture is not None else "live",
        }
        if getattr(args, "trade_file_out", None):
            from ki_ops.kotl.trade_file import apply_ledger_dispositions

            orders = store.load_working_orders(trade_date=args.trade_date)
            written, matched = apply_ledger_dispositions(args.trade_file_out, orders)
            summary["trade_file"] = written
            summary["trade_file_rows_updated"] = matched
        print(json.dumps(summary, indent=2))
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
