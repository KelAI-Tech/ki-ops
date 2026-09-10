"""Offline Flex adapter for KOTL development (no gRPC)."""

from __future__ import annotations

from uuid import uuid4


class FakeFlexAdapter:
    """Simulates ``CreateOrders`` — assigns deterministic fake ``orderId`` values."""

    def __init__(self, *, id_prefix: str = "FAKE") -> None:
        self.id_prefix = id_prefix
        self._sent: list[dict] = []

    @property
    def sent_batches(self) -> tuple[tuple[dict, ...], ...]:
        return tuple(tuple(batch) for batch in self._sent)

    def create_orders(self, order_list: list[dict]) -> list[dict]:
        """Return one create result per input order (same fake batchId per call)."""
        self._sent.append(list(order_list))
        batch_id = f"{self.id_prefix}-BATCH-{len(self._sent)}-{uuid4().hex[:6].upper()}"
        results = []
        for i, order in enumerate(order_list, start=1):
            symbol = str(order.get("symbol", "UNKNOWN")).replace(".", "-")
            order_id = f"{self.id_prefix}-{symbol}-{i}-{uuid4().hex[:6].upper()}"
            results.append(
                {
                    "success": True,
                    "orderId": order_id,
                    "batchId": batch_id,
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "quantity": order.get("quantity"),
                }
            )
        return results

    def fetch_orders(self, trade_date: str) -> list[dict]:
        """Optional stub for refresh tests — returns last submit as open orders."""
        if not self._sent:
            return []
        rows = []
        for batch in self._sent:
            for order in batch:
                rows.append(
                    {
                        "orderId": order.get("_fake_order_id"),  # populated by refresh helper if needed
                        "symbol": order.get("symbol"),
                        "side": order.get("side"),
                        "quantity": order.get("quantity"),
                        "filledQuantity": 0,
                        "status": "TRADABLE",
                        "fund": order.get("fund"),
                        "positionGroup": order.get("positionGroup"),
                        "tradeDate": trade_date,
                    }
                )
        return rows
