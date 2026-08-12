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
from ki_ops.portfolio import portfolio_from_holdings
from ki_ops.trades import load_trades_csv, summarize_trades

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOD = ROOT / "examples" / "sod_positions.csv"
DEFAULT_TARGETS = ROOT / "examples" / "target_intents.csv"
DEFAULT_CONFIG = ROOT / "config" / "risk_management.yaml"


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

    sub.add_parser("summarize-trades").add_argument("trades_csv", type=Path)

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
    return p


def _load_orders(path: Path) -> list[Order]:
    orders = []
    with path.open(encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            ts = row.get("timestamp")
            orders.append(
                Order(
                    row["symbol"],
                    Side(row["side"].upper()),
                    Decimal(row["quantity"]),
                    Decimal(row.get("price") or row.get("limit_price") or "0"),
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"
    settings = load_risk_settings(args.config)
    engine = PreTradeEngine(settings=settings)

    if command == "summarize-trades":
        print(json.dumps(summarize_trades(load_trades_csv(args.trades_csv)), indent=2))
        return 0

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
