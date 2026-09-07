"""Load Flex-like refresh fixtures (offline)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from ki_ops.kotl.models import WorkingOrder


def load_refresh_fixture(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "fills" not in data:
        raise ValueError(f"fixture missing 'fills' list: {path}")
    return data


def fixture_trade_date(fixture: dict[str, Any]) -> date | None:
    raw = fixture.get("trade_date")
    return date.fromisoformat(raw) if raw else None


class FixtureRefreshSource:
    """Return Flex order snapshots from a JSON fixture keyed by symbol / orderId."""

    def __init__(self, fixture: dict[str, Any] | str | Path) -> None:
        if isinstance(fixture, (str, Path)):
            self.fixture = load_refresh_fixture(fixture)
        else:
            self.fixture = fixture

    def fetch_orders(
        self,
        trade_date: str,
        *,
        stored: Sequence[WorkingOrder],
    ) -> list[dict]:
        """Build snapshots for *stored* working orders only."""
        fx_date = fixture_trade_date(self.fixture)
        if fx_date is not None and fx_date.isoformat() != trade_date:
            return []

        by_symbol = {str(row["symbol"]).upper(): row for row in self.fixture["fills"]}
        by_order_id = {
            str(row["orderId"]).upper(): row
            for row in self.fixture["fills"]
            if row.get("orderId")
        }

        snapshots: list[dict] = []
        for order in stored:
            fill = by_order_id.get(order.flex_order_id.upper()) or by_symbol.get(order.symbol.upper())
            if fill is None:
                continue
            snapshots.append(
                {
                    "orderId": order.flex_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": float(abs(order.sent_qty)),
                    "filledQuantity": float(fill.get("filledQuantity", 0)),
                    "status": fill.get("status"),
                    "weightedAvgPrice": fill.get("weightedAvgPrice"),
                    "fund": order.fund,
                    "positionGroup": order.position_group,
                    "tradeDate": trade_date,
                }
            )
        return snapshots
