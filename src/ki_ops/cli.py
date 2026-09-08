"""CLI: ki-ops."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ki_ops.config import load_risk_settings
from ki_ops.engine import PreTradeEngine
from ki_ops.intents import (
    build_trade_intent_batch,
    load_sod_positions_csv,
    load_symbol_volatilities,
    load_target_intents_csv,
)
from ki_ops.models import Holding, Order, Side
from ki_ops.poc_data import DEFAULT_POC_DATA, load_poc_data_paths
from ki_ops.portfolio import portfolio_from_holdings

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOD = ROOT / "examples" / "sod_positions.csv"
DEFAULT_TARGETS = ROOT / "examples" / "target_intents.csv"
DEFAULT_UNIVERSE = ROOT / "examples" / "extras" / "security_master.csv"
DEFAULT_CONFIG = ROOT / "config" / "risk_management_small_book.yaml"
DEFAULT_POC_CONFIG = ROOT / "config" / "risk_management_poc.yaml"
DEFAULT_POC_ALPHA = ROOT / "examples" / (
    "df_combo_lseg_v2c_00233cb52db9baa05a20329d01af6420f88241854b6c66b3e9da066884abfae8"
    "_neut_C5_cap125_nosv.parquet"
)
DEFAULT_ALPHA_PANEL = DEFAULT_POC_ALPHA
DEFAULT_EMS_INTENTS = ROOT / "examples" / "Portfolio_20260813.csv"


def _poc_default_label(key: str) -> str:
    """Help-text label for a POC manifest default.

    Resolved lazily so importing the CLI (e.g. from an installed wheel without
    the repo's config/ and examples/ trees) never fails; the manifest is only
    required when a run-perturb command actually needs it.
    """
    try:
        path = getattr(load_poc_data_paths(DEFAULT_POC_DATA), key)
    except (FileNotFoundError, ValueError, AttributeError):
        return f"{key} in POC manifest"
    if path is None:
        return f"{key} in POC manifest (optional)"
    try:
        shown: Path = path.relative_to(ROOT)
    except ValueError:
        shown = path
    return f"{key} in POC manifest → {shown}"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ki-ops")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="check-rebalance with default example files")
    run.add_argument("sod_csv", type=Path, nargs="?", default=DEFAULT_SOD)
    run.add_argument("targets_csv", type=Path, nargs="?", default=DEFAULT_TARGETS)
    run.add_argument("--cash", default=None)
    run.add_argument("--daily-pnl", default="0")
    run.add_argument("--keep-unmentioned", action="store_true")

    for name, help_text in (
        ("derive-trades", "target − SOD"),
        ("check-rebalance", "derive + risk checks"),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("sod_csv", type=Path)
        s.add_argument("targets_csv", type=Path)
        s.add_argument("--cash", default=None)
        s.add_argument("--keep-unmentioned", action="store_true")
        if name == "check-rebalance":
            s.add_argument("--daily-pnl", default="0")

    legacy = sub.add_parser("check-orders")
    legacy.add_argument("orders_csv", type=Path)
    legacy.add_argument("--holding", action="append", default=[])
    legacy.add_argument("--cash", default="0")
    legacy.add_argument("--daily-pnl", default="0")

    poc = sub.add_parser(
        "poc-alpha",
        help="day-over-day risk checks + turnover from alpha dollar parquet (POC)",
    )
    poc.add_argument(
        "alpha_parquet",
        type=Path,
        nargs="?",
        default=DEFAULT_POC_ALPHA,
        help="wide panel: dates × security_id dollars",
    )
    poc.add_argument("--start", default=None, help="inclusive YYYY-MM-DD")
    poc.add_argument("--end", default=None, help="inclusive YYYY-MM-DD")
    poc.add_argument("--cash", default="0")
    poc.add_argument(
        "--config",
        dest="poc_config",
        default=str(DEFAULT_POC_CONFIG),
        help="risk YAML (defaults to config/risk_management_poc.yaml)",
    )
    poc.add_argument(
        "--report-csv",
        type=Path,
        default=None,
        help="write daily turnover/risk CSV (default: examples/alpha_panel_turnover.csv)",
    )

    def _add_poc_csv_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--poc-data",
            type=Path,
            default=DEFAULT_POC_DATA,
            help="POC manifest YAML: sod, trades, prices, security_master (default: config/poc_pos_and_px.yaml)",
        )
        parser.add_argument(
            "--sod",
            type=Path,
            default=None,
            help=f"SOD CSV (default: {_poc_default_label('sod')})",
        )
        parser.add_argument(
            "--trades",
            type=Path,
            default=None,
            help=f"trade-intent CSV (default: {_poc_default_label('trades')})",
        )
        parser.add_argument("--cash", default="0")
        parser.add_argument(
            "--config",
            dest="poc_config",
            default=str(DEFAULT_POC_CONFIG),
            help="risk YAML (defaults to config/risk_management_poc.yaml)",
        )
        parser.add_argument(
            "--prices",
            type=Path,
            default=None,
            help=f"Datastream2 px CSV (default: {_poc_default_label('prices')})",
        )
        parser.add_argument(
            "--adv",
            type=Path,
            default=None,
            help=f"ADV snapshot CSV (default: {_poc_default_label('adv')})",
        )
        parser.add_argument(
            "--ticker-mapping",
            type=Path,
            default=None,
            help=f"TICKER_MAPPING_DT interval CSV (default: {_poc_default_label('ticker_mapping')})",
        )
        parser.add_argument(
            "--as-of",
            default="2026-08-06",
            help="trade date for tradability / delist checks (YYYY-MM-DD)",
        )
        parser.add_argument(
            "--json-out",
            type=Path,
            default=None,
            help="write the same JSON artifact (with input/config hashes) to FILE",
        )

    rp = sub.add_parser(
        "run-perturb-baseline",
        help="POC baseline: full pre-trade gate on sod + trade intents (~24%% two-way TO)",
    )
    _add_poc_csv_args(rp)

    pt = sub.add_parser(
        "run-perturb-var-checks",
        help="scale trades above max_turnover, then run the full pre-trade gate",
    )
    _add_poc_csv_args(pt)
    pt.add_argument(
        "--target-turnover",
        default="0.26",
        help="scaled two-way turnover target (default 0.26 vs 0.25 cap)",
    )
    pt.add_argument(
        "--target-gmv",
        default="90000000",
        help="projected GMV after scaled trades (default 90000000)",
    )

    pz = sub.add_parser(
        "run-perturb-zero",
        help="zero all trade quantities, then run the full pre-trade gate (turnover 0)",
    )
    _add_poc_csv_args(pz)

    # Sidecar features (EMS / filled trades / risk snapshot) — not the POC path
    extras = sub.add_parser(
        "extras",
        help="optional sidecars: EMS, fills, risk snapshot, perturb email/Slack job",
    )
    ex = extras.add_subparsers(dest="extras_command", required=True)

    risk = ex.add_parser("risk-snapshot", help="factor / sector / beta exposures")
    risk.add_argument("sod_csv", type=Path, nargs="?", default=None)
    risk.add_argument("targets_csv", type=Path, nargs="?", default=None)
    risk.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    risk.add_argument("--cash", default=None)
    risk.add_argument("--keep-unmentioned", action="store_true")
    risk.add_argument("--current-only", action="store_true", help="skip target book")

    ex.add_parser("summarize-trades", help="summarize filled-trade CSV").add_argument(
        "trades_csv", type=Path
    )

    px = ex.add_parser(
        "check-ems",
        aliases=["approx-px"],
        help="EMS drop missing px: POC price = |notional| / |qty|",
    )
    px.add_argument("intents_csv", type=Path, nargs="?", default=DEFAULT_EMS_INTENTS)
    px.add_argument(
        "--alpha-parquet",
        type=Path,
        default=DEFAULT_ALPHA_PANEL,
        help="wide alpha dollar panel (dates × security_id); falls back to SOD CSV",
    )
    px.add_argument(
        "--as-of",
        default=None,
        help="trade-intent date YYYY-MM-DD (default: date in Portfolio_YYYYMMDD.csv)",
    )
    px.add_argument(
        "--id-map",
        type=Path,
        default=None,
        help="ticker→infocode CSV (default: SECURITY_MASTER_DT); or TICKER_MAPPING_DT intervals",
    )
    px.add_argument(
        "--out",
        type=Path,
        default=None,
        help="enriched intents CSV (default: next to the EMS file as *_with_px.csv)",
    )
    px.add_argument(
        "--config",
        dest="poc_config",
        default=str(DEFAULT_POC_CONFIG),
        help="risk YAML (defaults to config/risk_management_poc.yaml)",
    )

    np = ex.add_parser(
        "notify-perturbs",
        help="run the three POC perturbs and email stdout (Slack if a webhook is set)",
    )
    np.add_argument(
        "--to",
        default=None,
        help="comma-separated recipients (default: KI_OPS_EMAIL_TO or robert@kelaitech.com)",
    )
    np.add_argument(
        "--from-addr",
        default=None,
        help="From address (default: KI_OPS_SMTP_FROM or robert@kelaitech.com). Mail.app must have this account.",
    )
    np.add_argument("--skip-email", action="store_true", help="do not send email")
    np.add_argument("--skip-slack", action="store_true", help="do not post to Slack")
    np.add_argument(
        "--test-email",
        action="store_true",
        help="send a one-line test message; do not run the three perturbs",
    )
    np.add_argument(
        "--test-slack",
        action="store_true",
        help="post a one-line test message to Slack; do not run the three perturbs",
    )
    np.add_argument(
        "--dry-run",
        action="store_true",
        help="build the payload; do not send",
    )
    np.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="notify env file (default: config/notify.env)",
    )

    nc = ex.add_parser(
        "notify-config",
        help="show loaded email/Slack settings (password not printed)",
    )
    nc.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="notify env file (default: config/notify.env)",
    )

    from ki_ops.gate import register_gate_parser
    from ki_ops.kotl.cli import register_kotl_parser

    register_gate_parser(sub)
    register_kotl_parser(sub)

    return p


def _load_orders(path: Path) -> list[Order]:
    orders = []
    with path.open(encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            ts = row.get("timestamp")
            qty_raw = Decimal(row["quantity"])
            side_raw = (row.get("side") or "").upper()
            if side_raw in {"BUY", "SELL"}:
                side = Side(side_raw)
                qty = abs(qty_raw)
            else:
                side = Side.BUY if qty_raw >= 0 else Side.SELL
                qty = abs(qty_raw)
            orders.append(
                Order(
                    row.get("symbol") or row.get("infocode") or "",
                    side,
                    qty,
                    Decimal(row.get("price") or row.get("limit_price") or "1"),
                    datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(timezone.utc),
                    order_id=row.get("order_id") or None,
                )
            )
    return orders


def _check_rebalance(engine: PreTradeEngine, args) -> int:
    sod = load_sod_positions_csv(args.sod_csv, cash=getattr(args, "cash", None))
    targets = load_target_intents_csv(args.targets_csv)
    vols = load_symbol_volatilities(args.sod_csv, args.targets_csv)
    result = engine.evaluate_from_targets(
        sod,
        targets,
        realized_daily_pnl=Decimal(getattr(args, "daily_pnl", "0")),
        volatilities=vols,
        flatten_missing_targets=not getattr(args, "keep_unmentioned", False),
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.allowed else 2


def _poc_alpha(args) -> int:
    from ki_ops.alpha import (
        load_alpha_dollar_panel,
        run_alpha_panel_checks,
        summarize_alpha_days,
        write_turnover_csv,
    )

    settings = load_risk_settings(args.poc_config)
    engine = PreTradeEngine(settings=settings)
    panel = load_alpha_dollar_panel(args.alpha_parquet, start=args.start, end=args.end)
    days = run_alpha_panel_checks(panel, engine, cash=Decimal(args.cash))
    summary = summarize_alpha_days(days)

    report = args.report_csv
    if report is None:
        report = ROOT / "examples" / "alpha_panel_turnover.csv"
    write_turnover_csv(days, report)
    summary["report_csv"] = str(report)
    summary["alpha_parquet"] = str(args.alpha_parquet)
    summary["config"] = str(args.poc_config)
    summary["n_panel_dates"] = int(len(panel.index))
    summary["panel_start"] = str(panel.index.min().date())
    summary["panel_end"] = str(panel.index.max().date())

    print(json.dumps(summary, indent=2))
    return 0 if summary.get("n_blocked", 0) == 0 else 2


def _check_ems(args) -> int:
    from ki_ops.extras.ems_intents import (
        approximate_ems_prices_from_alpha,
        write_enriched_intents_csv,
    )

    poc = load_poc_data_paths(getattr(args, "poc_data", None))
    id_map = args.id_map if args.id_map is not None else poc.security_master
    enriched, summary, _prior = approximate_ems_prices_from_alpha(
        args.intents_csv,
        args.alpha_parquet,
        as_of=args.as_of,
        id_map_csv=id_map,
        sod_csv=poc.sod,
    )
    out = args.out
    if out is None:
        out = args.intents_csv.with_name(f"{args.intents_csv.stem}_with_px.csv")
    write_enriched_intents_csv(enriched, out)
    summary["out_csv"] = str(out)
    summary["config"] = str(args.poc_config)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _extras(args) -> int:
    cmd = args.extras_command
    if cmd in {"check-ems", "approx-px"}:
        return _check_ems(args)
    if cmd == "summarize-trades":
        from ki_ops.extras.trades import load_trades_csv, summarize_trades

        print(json.dumps(summarize_trades(load_trades_csv(args.trades_csv)), indent=2))
        return 0
    if cmd == "risk-snapshot":
        from ki_ops.extras.risk import build_risk_snapshot, load_security_master_csv

        sod_csv = args.sod_csv or DEFAULT_SOD
        sod = load_sod_positions_csv(sod_csv, cash=args.cash)
        universe = load_security_master_csv(args.universe)
        if args.current_only:
            targets = None
        elif args.targets_csv is not None:
            targets = load_target_intents_csv(args.targets_csv)
        elif args.sod_csv is None:
            targets = load_target_intents_csv(DEFAULT_TARGETS)
        else:
            targets = None
        snap = build_risk_snapshot(
            sod,
            universe,
            targets=targets,
            flatten_missing_targets=not args.keep_unmentioned,
        )
        print(json.dumps(snap.to_dict(), indent=2))
        return 0
    if cmd == "notify-perturbs":
        from ki_ops.extras.perturb_job import run_and_notify_perturbs

        summary = run_and_notify_perturbs(
            send_email=not args.skip_email,
            send_slack=not args.skip_slack,
            dry_run=args.dry_run,
            test_email=args.test_email,
            test_slack=args.test_slack,
            to=args.to,
            sender=args.from_addr,
            env_file=getattr(args, "env_file", None),
        )
        print(json.dumps(summary, indent=2, default=str))
        if args.dry_run or args.test_email or args.test_slack:
            return 0
        return int(summary.get("max_exit_code") or 0)
    if cmd == "notify-config":
        from ki_ops.extras.notify import describe_settings, load_notify_settings, notify_env_candidates

        cfg = load_notify_settings(env_file=getattr(args, "env_file", None))
        loaded = next((str(p) for p in notify_env_candidates(getattr(args, "env_file", None)) if p.is_file()), None)
        out = {"env_file": loaded, **describe_settings(cfg)}
        print(json.dumps(out, indent=2))
        return 0
    return 2


def _load_trade_time_prices(path: Path | None) -> dict[str, Decimal]:
    if path is None or not Path(path).is_file():
        return {}
    from ki_ops.alpha import load_infocode_price_map

    return load_infocode_price_map(path, field="close")


def _run_perturb(args) -> int:
    return _run_perturb_breach(args, scenario="baseline")


def _resolve_poc_paths(args) -> tuple[Path, Path, Path]:
    poc = load_poc_data_paths(getattr(args, "poc_data", None))
    sod = args.sod or poc.sod
    trades = args.trades or poc.trades
    prices = args.prices or poc.prices
    return sod, trades, prices


def _run_perturb_breach(args, *, scenario: str) -> int:
    from datetime import date

    from ki_ops.alpha import load_infocode_ticker_map, run_lseg_perturb
    from ki_ops.audit import write_json_out

    sod, trades, prices = _resolve_poc_paths(args)
    poc = load_poc_data_paths(getattr(args, "poc_data", None))
    as_of = date.fromisoformat(str(args.as_of))
    mapping = getattr(args, "ticker_mapping", None) or poc.ticker_mapping
    tickers = load_infocode_ticker_map(poc.security_master)
    if mapping:
        tickers.update(load_infocode_ticker_map(mapping, as_of=as_of))
    target = Decimal(getattr(args, "target_turnover", "0.26")) if scenario == "var-checks" else Decimal("0.26")
    out = run_lseg_perturb(
        _load_orders(trades),
        scenario=scenario,  # type: ignore[arg-type]
        sod_csv=sod,
        trades_csv=trades,
        cash=Decimal(args.cash),
        config_path=args.poc_config,
        target_turnover=target,
        target_gmv=Decimal(getattr(args, "target_gmv", "90000000")),
        prices_csv=prices,
        price_by_infocode=_load_trade_time_prices(prices),
        ticker_by_infocode=tickers,
        security_master_csv=poc.security_master,
        ticker_mapping_csv=mapping,
        adv_csv=getattr(args, "adv", None) or poc.adv,
        as_of=as_of,
    )
    from ki_ops.gate import format_verdict_json

    body = format_verdict_json(out)
    print(body, end="")
    if getattr(args, "json_out", None):
        write_json_out(args.json_out, out)
    return 0 if out["output"]["passed"] else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"

    if command == "poc-alpha":
        return _poc_alpha(args)

    if command == "extras":
        return _extras(args)

    if command == "gate":
        from ki_ops.gate import run_gate

        return run_gate(args)

    if command == "kotl":
        from ki_ops.kotl.cli import run_kotl

        return run_kotl(args)

    if command == "run-perturb-baseline":
        return _run_perturb(args)

    if command == "run-perturb-var-checks":
        return _run_perturb_breach(args, scenario="var-checks")

    if command == "run-perturb-zero":
        return _run_perturb_breach(args, scenario="zero-turnover")

    settings = load_risk_settings(args.config)
    engine = PreTradeEngine(settings=settings)

    if command == "derive-trades":
        sod = load_sod_positions_csv(args.sod_csv, cash=args.cash)
        targets = load_target_intents_csv(args.targets_csv)
        batch = build_trade_intent_batch(
            sod, targets, flatten_missing_targets=not args.keep_unmentioned
        )
        print(json.dumps(batch.to_dict(), indent=2))
        return 0

    if command in {"run", "check-rebalance"}:
        if command == "run" and not hasattr(args, "sod_csv"):
            args.sod_csv = DEFAULT_SOD
            args.targets_csv = DEFAULT_TARGETS
            args.cash = None
            args.daily_pnl = "0"
            args.keep_unmentioned = False
        return _check_rebalance(engine, args)

    if command == "check-orders":
        holdings = []
        for spec in args.holding:
            sym, qty, px = spec.split(":")
            holdings.append(Holding(sym, Decimal(qty), Decimal(px)))
        result = engine.evaluate(
            portfolio_from_holdings(holdings, cash=Decimal(args.cash)),
            _load_orders(args.orders_csv),
            realized_daily_pnl=Decimal(args.daily_pnl),
        )
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.allowed else 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
