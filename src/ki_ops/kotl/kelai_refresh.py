"""KelAI ``get_orders`` / ``GetOrderInfo2`` fixture loader (offline)."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from ki_ops.kotl.models import WorkingOrder
from ki_ops.kotl.qty import flex_status_label

# Brooklyn Orders.proto enums (MarketSide).
_MARKET_SIDE = {
    0: "BUY",
    1: "SELL",
    2: "COVER",
    3: "SHORT",
}


def flex_side_label(side: Any) -> str | None:
    if side is None or side == "":
        return None
    if isinstance(side, int):
        return _MARKET_SIDE.get(side, str(side))
    text = str(side).strip()
    if text.isdigit():
        return _MARKET_SIDE.get(int(text), text)
    return text.upper()


def _parse_trade_date(raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if len(text) == 10 and text[4] == "-":
        return date.fromisoformat(text)
    if len(text) == 10 and text[2] == "/":
        # Flex UI style MM/DD/YYYY
        month, day, year = text.split("/")
        return date(int(year), int(month), int(day))
    return None


def _rows_from_json(path: Path) -> tuple[list[dict[str, Any]], date | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        orders = data.get("orders") or data.get("records") or data.get("rows")
        if orders is None:
            raise ValueError(f"JSON fixture missing orders list: {path}")
        fx_date = _parse_trade_date(data.get("trade_date") or data.get("tradeDate"))
        return list(orders), fx_date
    raise ValueError(f"unsupported JSON fixture shape: {path}")


def _rows_from_csv(path: Path) -> tuple[list[dict[str, Any]], date | None]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [{k: (v or "").strip() for k, v in row.items()} for row in reader]
    if not rows:
        return [], None
    if "orderId" not in rows[0] and "orderid" not in {k.lower() for k in rows[0]}:
        raise ValueError(f"CSV fixture missing orderId column: {path}")
    trade_dates = {_parse_trade_date(r.get("tradeDate") or r.get("trade_date")) for r in rows}
    trade_dates.discard(None)
    fx_date = next(iter(trade_dates)) if len(trade_dates) == 1 else None
    return rows, fx_date


def load_kelai_orders_fixture(path: str | Path) -> tuple[list[dict[str, Any]], date | None]:
    """Load kelai ``get_orders`` export as JSON (records) or CSV."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return _rows_from_csv(path)
    return _rows_from_json(path)


def kelai_row_to_snapshot(row: dict[str, Any], *, order_id: str, trade_date: str) -> dict[str, Any]:
    """Normalize one kelai/GetOrderInfo2 row to KOTL refresh snapshot fields."""
    filled = row.get("filledQuantity")
    if filled in (None, "") and row.get("filledQuantity_acc_tgt") not in (None, ""):
        filled = row.get("filledQuantity_acc_tgt")

    return {
        "orderId": order_id,
        "batchId": str(row.get("batchId") or "") or None,
        "symbol": str(row.get("symbol") or "").upper(),
        "side": flex_side_label(row.get("side")) or row.get("side"),
        "quantity": float(row.get("quantity") or 0),
        "filledQuantity": float(filled or 0),
        "status": flex_status_label(row.get("status")),
        "weightedAvgPrice": row.get("weightedAvgPrice") or row.get("weightedAvgPrice_st"),
        "fund": row.get("fund_acc_tgt") or row.get("fund"),
        "positionGroup": row.get("positionGroup_acc_tgt") or row.get("positionGroup"),
        "tradeDate": trade_date,
    }


class KelaiRefreshSource:
    """Refresh from kelai ``get_orders`` JSON/CSV (GetOrderInfo2 flatten shape)."""

    def __init__(self, fixture: str | Path | list[dict[str, Any]], *, trade_date: date | None = None) -> None:
        if isinstance(fixture, list):
            self.rows = fixture
            self.fixture_trade_date = trade_date
        else:
            self.rows, self.fixture_trade_date = load_kelai_orders_fixture(fixture)

    def fetch_orders(
        self,
        trade_date: str,
        *,
        stored: Sequence[WorkingOrder],
    ) -> list[dict]:
        if self.fixture_trade_date is not None and self.fixture_trade_date.isoformat() != trade_date:
            return []

        by_order_id = {
            str(row.get("orderId") or "").upper(): row
            for row in self.rows
            if row.get("orderId")
        }
        by_symbol = {str(row.get("symbol") or "").upper(): row for row in self.rows}

        snapshots: list[dict] = []
        for order in stored:
            row = by_order_id.get(order.flex_order_id.upper()) or by_symbol.get(order.symbol.upper())
            if row is None:
                continue
            snapshots.append(
                kelai_row_to_snapshot(row, order_id=order.flex_order_id, trade_date=trade_date)
            )
        return snapshots
