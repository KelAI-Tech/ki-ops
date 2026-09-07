"""CSV persistence for submits and working orders."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.models import Submit, WorkingOrder

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = ROOT / "data" / "kotl"

SUBMITS_FILE = "submits.csv"
WORKING_ORDERS_FILE = "working_orders.csv"

SUBMIT_FIELDS = (
    "submit_id",
    "submitted_at",
    "env",
    "ok",
    "flex_order_ids",
    "payload_json",
    "flex_response_json",
)

WORKING_ORDER_FIELDS = (
    "flex_order_id",
    "submit_id",
    "trade_date",
    "symbol",
    "side",
    "fund",
    "position_group",
    "sent_qty",
    "filled_qty",
    "leaves_qty",
    "status",
    "last_seen_at",
    "avg_fill_px",
    "flex_batch_id",
    "broker",
    "algo",
    "order_type",
)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _parse_ts(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    return _utc(datetime.fromisoformat(text))


def _parse_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def _d(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


class KotlStore:
    """Read/write ``submits.csv`` and ``working_orders.csv`` under *data_dir*."""

    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    @property
    def submits_path(self) -> Path:
        return self.data_dir / SUBMITS_FILE

    @property
    def working_orders_path(self) -> Path:
        return self.data_dir / WORKING_ORDERS_FILE

    def append_submit(self, submit: Submit) -> None:
        self._ensure_dir()
        write_header = not self.submits_path.exists()
        with self.submits_path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SUBMIT_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(_submit_to_row(submit))

    def load_submits(self) -> list[Submit]:
        if not self.submits_path.exists():
            return []
        with self.submits_path.open(encoding="utf-8", newline="") as fh:
            return [_submit_from_row(row) for row in csv.DictReader(fh)]

    def load_working_orders(
        self,
        *,
        trade_date: date | None = None,
        submit_id: str | None = None,
    ) -> list[WorkingOrder]:
        rows = self._load_all_working_orders()
        if trade_date is not None:
            rows = [r for r in rows if r.trade_date == trade_date]
        if submit_id is not None:
            rows = [r for r in rows if r.submit_id == submit_id]
        return rows

    def get_working_order(self, flex_order_id: str) -> WorkingOrder | None:
        for row in self._load_all_working_orders():
            if row.flex_order_id == flex_order_id:
                return row
        return None

    def upsert_working_orders(self, orders: Iterable[WorkingOrder]) -> None:
        """Insert or replace rows keyed by ``flex_order_id``."""
        self._ensure_dir()
        by_id = {r.flex_order_id: r for r in self._load_all_working_orders()}
        for order in orders:
            by_id[order.flex_order_id] = order
        self._write_working_orders(by_id.values())

    def _load_all_working_orders(self) -> list[WorkingOrder]:
        if not self.working_orders_path.exists():
            return []
        with self.working_orders_path.open(encoding="utf-8", newline="") as fh:
            return [_working_order_from_row(row) for row in csv.DictReader(fh)]

    def _write_working_orders(self, orders: Sequence[WorkingOrder]) -> None:
        self._ensure_dir()
        sorted_orders = sorted(orders, key=lambda o: (o.trade_date, o.flex_order_id))
        with self.working_orders_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=WORKING_ORDER_FIELDS)
            writer.writeheader()
            for order in sorted_orders:
                writer.writerow(_working_order_to_row(order))

    def _ensure_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def _submit_to_row(submit: Submit) -> dict[str, str]:
    return {
        "submit_id": submit.submit_id,
        "submitted_at": submit.submitted_at.isoformat(),
        "env": submit.env,
        "ok": "true" if submit.ok else "false",
        "flex_order_ids": "|".join(submit.flex_order_ids),
        "payload_json": json.dumps(list(submit.payload), sort_keys=True),
        "flex_response_json": json.dumps(submit.flex_response, sort_keys=True)
        if submit.flex_response is not None
        else "",
    }


def _submit_from_row(row: dict[str, str]) -> Submit:
    payload_raw = row.get("payload_json") or "[]"
    response_raw = row.get("flex_response_json") or ""
    flex_ids = [x for x in (row.get("flex_order_ids") or "").split("|") if x]
    return Submit(
        submit_id=row["submit_id"],
        submitted_at=_parse_ts(row["submitted_at"]),
        env=row["env"],
        ok=(row.get("ok") or "").lower() == "true",
        flex_order_ids=tuple(flex_ids),
        payload=tuple(json.loads(payload_raw)),
        flex_response=json.loads(response_raw) if response_raw else None,
    )


def _working_order_to_row(order: WorkingOrder) -> dict[str, str]:
    return {
        "flex_order_id": order.flex_order_id,
        "submit_id": order.submit_id,
        "trade_date": order.trade_date.isoformat(),
        "symbol": order.symbol,
        "side": order.side,
        "fund": order.fund,
        "position_group": order.position_group,
        "sent_qty": str(order.sent_qty),
        "filled_qty": str(order.filled_qty),
        "leaves_qty": str(order.leaves_qty),
        "status": order.status.value,
        "last_seen_at": order.last_seen_at.isoformat(),
        "avg_fill_px": str(order.avg_fill_px) if order.avg_fill_px is not None else "",
        "flex_batch_id": order.flex_batch_id or "",
        "broker": order.broker or "",
        "algo": order.algo or "",
        "order_type": order.order_type or "",
    }


def _working_order_from_row(row: dict[str, str]) -> WorkingOrder:
    avg = _d(row.get("avg_fill_px"))
    return WorkingOrder(
        flex_order_id=row["flex_order_id"],
        submit_id=row["submit_id"],
        trade_date=_parse_date(row["trade_date"]),
        symbol=row["symbol"],
        side=row["side"],
        fund=row["fund"],
        position_group=row["position_group"],
        sent_qty=Decimal(row["sent_qty"]),
        filled_qty=Decimal(row["filled_qty"]),
        leaves_qty=Decimal(row["leaves_qty"]),
        status=OrderStatus(row["status"]),
        last_seen_at=_parse_ts(row["last_seen_at"]),
        avg_fill_px=avg,
        flex_batch_id=row.get("flex_batch_id") or None,
        broker=row.get("broker") or None,
        algo=row.get("algo") or None,
        order_type=row.get("order_type") or None,
    )
