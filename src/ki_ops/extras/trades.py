"""Trade CSV loaders."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ki_ops.models import Side, Trade


def _ts(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_trades_csv(path: str | Path) -> list[Trade]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        trades = []
        for raw in reader:
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            trades.append(
                Trade(
                    row["symbol"],
                    Side(row["side"].upper()),
                    Decimal(row["quantity"]),
                    Decimal(row["price"]),
                    _ts(row["timestamp"]),
                    trade_id=row.get("trade_id") or None,
                    fees=Decimal(row.get("fees") or "0"),
                )
            )
        return trades


def summarize_trades(trades: Sequence[Trade]) -> dict:
    buys = [t for t in trades if t.side is Side.BUY]
    sells = [t for t in trades if t.side is Side.SELL]
    return {
        "count": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "gross_notional": str(sum((t.notional for t in trades), Decimal("0"))),
        "buy_notional": str(sum((t.notional for t in buys), Decimal("0"))),
        "sell_notional": str(sum((t.notional for t in sells), Decimal("0"))),
        "symbols": sorted({t.symbol for t in trades}),
    }
