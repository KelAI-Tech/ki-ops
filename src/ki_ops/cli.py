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
DEFAULT_CONFIG = ROOT / "config" / "risk_management.yaml"
DEFAULT_POC_CONFIG = ROOT / "config" / "risk_management_poc.yaml"
DEFAULT_POC_ALPHA = ROOT / "examples" / (
    "df_combo_lseg_v2c_00233cb52db9baa05a20329d01af6420f88241854b6c66b3e9da066884abfae8"
    "_neut_C5_cap125_nosv.parquet"
)
DEFAULT_ALPHA_PANEL = DEFAULT_POC_ALPHA
DEFAULT_EMS_INTENTS = ROOT / "examples" / "extras" / "Portfolio_20260806.csv"


def _poc_default_label(key: str) -> str:
    """Help-text label for a POC manifest default.

    Resolved lazily so importing the CLI (e.g. from an installed wheel without
    the repo's config/ and examples/ trees) never fails; the manifest is only
    required when a run-perturb command actually needs it.
    """
    try:
        path = getattr(load_poc_data_paths(DEFAULT_POC_DATA), key)
    except (FileNotFoundError, ValueError):
        return f"{key} in POC manifest"
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
            help="POC manifest YAML: sod, trades, prices, ticker_map (default: config/poc_pos_and_px.yaml)",
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

    rp = sub.add_parser(
        "run-perturb",
        aliases=["perturb"],
        help="POC baseline: sod_lseg_20260805.csv + trade_intents_lseg_20260806.csv (~24% two-way TO)",
    )
    _add_poc_csv_args(rp)

    pt = sub.add_parser(
        "run-perturb-turnover",
        aliases=["perturb-turnover", "perturb-to"],
        help="same POC CSVs; scale trades to breach max_turnover (two-way)",
    )
    _add_poc_csv_args(pt)
    pt.add_argument(
        "--target-turnover",
        default="0.52",
        help="scaled two-way turnover target (default 0.52 vs 0.50 cap)",
    )
    pt.add_argument(
        "--target-gmv",
        default="90000000",
        help="projected GMV after scaled trades (default 90000000)",
    )
    pt.add_argument(
        "--scaled-trades",
        type=Path,
        default=None,
        help="write scaled trade-intent CSV (default: <trades>_scaled.csv)",
    )

    pz = sub.add_parser(
        "run-perturb-zero",
        aliases=["perturb-zero", "perturb-z"],
        help="same POC CSVs; zero all trade quantities (turnover 0)",
    )
    _add_poc_csv_args(pz)
    pz.add_argument(
        "--scaled-trades",
        type=Path,
        default=None,
        help="write zeroed trade-intent CSV (default: <trades>_zero.csv)",
    )

    # Sidecar features (EMS / filled trades / risk snapshot) — not the POC path
    extras = sub.add_parser("extras", help="optional sidecars: EMS, filled trades, risk snapshot")
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
        help="EMS drop vs alpha-parquet SOD; approx px from SOD $ / qty",
    )
    px.add_argument("intents_csv", type=Path, nargs="?", default=DEFAULT_EMS_INTENTS)
    px.add_argument(
        "--alpha-parquet",
        type=Path,
        default=DEFAULT_ALPHA_PANEL,
        help="wide alpha dollar panel used as SOD (dates × security_id)",
    )
    px.add_argument(
        "--as-of",
        default="2026-08-06",
        help="trade-intent date YYYY-MM-DD (SOD = last parquet date before this)",
    )
    px.add_argument(
        "--id-map",
        type=Path,
        default=None,
        help="CSV with security_id,symbol to join tickers to parquet SOD names",
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
        evaluate_ems_against_alpha_sod,
        write_enriched_intents_csv,
    )

    settings = load_risk_settings(args.poc_config)
    engine = PreTradeEngine(settings=settings)
    summary = evaluate_ems_against_alpha_sod(
        args.intents_csv,
        args.alpha_parquet,
        engine,
        as_of=args.as_of,
        id_map_csv=args.id_map,
    )
    enriched = summary.pop("_enriched")
    out = args.out
    if out is None:
        out = args.intents_csv.with_name(f"{args.intents_csv.stem}_with_px.csv")
    write_enriched_intents_csv(enriched, out)
    summary["out_csv"] = str(out)
    summary["config"] = str(args.poc_config)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("passed") else 2


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
    from ki_ops.alpha import load_infocode_ticker_map, run_lseg_perturb

    sod, trades, prices = _resolve_poc_paths(args)
    poc = load_poc_data_paths(getattr(args, "poc_data", None))
    target = Decimal(getattr(args, "target_turnover", "0.52")) if scenario == "max-turnover" else Decimal("0.52")
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
        ticker_by_infocode=load_infocode_ticker_map(poc.ticker_map),
        ticker_map_csv=poc.ticker_map,
        scaled_trades_csv=getattr(args, "scaled_trades", None),
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("passed") else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"

    if command == "poc-alpha":
        return _poc_alpha(args)

    if command == "extras":
        return _extras(args)

    if command in {"run-perturb", "perturb"}:
        return _run_perturb(args)

    if command in {"run-perturb-turnover", "perturb-turnover", "perturb-to"}:
        return _run_perturb_breach(args, scenario="max-turnover")

    if command in {"run-perturb-zero", "perturb-zero", "perturb-z"}:
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
