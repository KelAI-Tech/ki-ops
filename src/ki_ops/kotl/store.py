"""CSV persistence for submits and working orders."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Protocol, Sequence

# A full-portfolio submit serializes ~2k orders into one payload_json cell,
# far past the stdlib's 128 KiB default field cap (hit live 2026-09-08).
csv.field_size_limit(sys.maxsize)

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.models import Submit, WorkingOrder


class KotlStoreProtocol(Protocol):
    """Minimal ledger surface used by submit/refresh/eod/report.

    ``KotlStore`` (CSV, default) and ``MysqlKotlStore`` both satisfy it.
    """

    def append_submit(self, submit: Submit) -> None: ...

    def load_submits(self) -> list[Submit]: ...

    def load_working_orders(
        self,
        *,
        trade_date: date | None = None,
        submit_id: str | None = None,
    ) -> list[WorkingOrder]: ...

    def get_working_order(self, flex_order_id: str) -> WorkingOrder | None: ...

    def upsert_working_orders(self, orders: Iterable[WorkingOrder]) -> None: ...

    def claim_submission(self, trade_date: date, env: str, submit_id: str) -> str | None: ...

    def record_book_snapshot(
        self, as_of: date, env: str, book: "dict[str, Decimal]"
    ) -> bool: ...

    def load_book_snapshot(self, as_of: date, env: str) -> "dict[str, Decimal] | None": ...

    def load_latest_book_snapshot(
        self, env: str, *, before: date | None = None
    ) -> "tuple[date, dict[str, Decimal]] | None": ...

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = ROOT / "data" / "kotl"

SUBMITS_FILE = "submits.csv"
WORKING_ORDERS_FILE = "working_orders.csv"
CLAIMS_DIR = "claims"
BOOK_SNAPSHOTS_DIR = "book_snapshots"

SUBMIT_FIELDS = (
    "submit_id",
    "submitted_at",
    "env",
    "ok",
    "flex_order_ids",
    "payload_json",
    "flex_response_json",
    "trade_date",
    "claim_submit_id",
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
        if self.submits_path.exists() and self._submits_header() != list(SUBMIT_FIELDS):
            # Legacy header (pre trade_date/claim_submit_id): rewrite the file
            # under the current header before appending; old rows load their
            # missing fields as None either way.
            rows = self.load_submits()
            with self.submits_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=SUBMIT_FIELDS)
                writer.writeheader()
                for old in rows:
                    writer.writerow(_submit_to_row(old))
        write_header = not self.submits_path.exists()
        with self.submits_path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SUBMIT_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(_submit_to_row(submit))

    def _submits_header(self) -> list[str]:
        with self.submits_path.open(encoding="utf-8", newline="") as fh:
            return next(csv.reader(fh), [])

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

    def claim_submission(self, trade_date: date, env: str, submit_id: str) -> str | None:
        """Once-a-day submission claim: ``None`` when this call won the claim,
        else the ``submit_id`` that already holds it.

        ``O_CREAT|O_EXCL`` on a per-``(trade_date, env)`` file — atomic on a
        local filesystem; the MySQL store is the concurrency-safe live backend
        (this fallback is documented as single-host only).
        """
        claims = self.data_dir / CLAIMS_DIR
        claims.mkdir(parents=True, exist_ok=True)
        path = claims / f"{env.upper()}_{trade_date.strftime('%Y%m%d')}.claim"
        try:
            with path.open("x", encoding="utf-8") as fh:
                fh.write(submit_id)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip() or "unknown"
        return None

    # --- portfolio book snapshots -------------------------------------------
    #
    # One JSON file per (as_of, env): the full signed Flex position book
    # (canonical Flex symbols, e.g. WM.US / BF/B.US) captured nightly after
    # the close by ``kotl snapshot-book``. It is the next morning's recon
    # baseline: the live book must equal the last snapshot before any send.

    def _book_snapshot_path(self, as_of: date, env: str) -> Path:
        return (
            self.data_dir
            / BOOK_SNAPSHOTS_DIR
            / f"{env.upper()}_{as_of.strftime('%Y%m%d')}.json"
        )

    def record_book_snapshot(self, as_of: date, env: str, book: dict[str, Decimal]) -> bool:
        """Persist the book as of the *as_of* close — **first-wins**.

        Returns ``True`` when this call recorded it, ``False`` when a snapshot
        for ``(as_of, env)`` already existed (idempotent nightly re-runs).
        ``O_CREAT|O_EXCL``; same single-host atomicity note as
        :meth:`claim_submission`. An empty book is a legitimate observation
        and stores as ``{}`` — distinct from "no snapshot"
        (:meth:`load_book_snapshot` → ``None``).
        """
        path = self._book_snapshot_path(as_of, env)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "captured_at": _utc(datetime.now(timezone.utc)).isoformat(),
                "positions": {
                    str(sym).upper(): str(qty) for sym, qty in sorted(book.items())
                },
            },
            sort_keys=True,
        )
        try:
            with path.open("x", encoding="utf-8") as fh:
                fh.write(payload)
        except FileExistsError:
            return False
        return True

    def load_book_snapshot(self, as_of: date, env: str) -> dict[str, Decimal] | None:
        path = self._book_snapshot_path(as_of, env)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(sym).upper(): Decimal(qty)
            for sym, qty in (data.get("positions") or {}).items()
        }

    def load_latest_book_snapshot(
        self, env: str, *, before: date | None = None
    ) -> tuple[date, dict[str, Decimal]] | None:
        """Most recent snapshot for *env*, optionally strictly before *before*
        (a submit for trade date T wants the book as of the last close < T).
        Returns ``(as_of, book)`` or ``None``."""
        folder = self.data_dir / BOOK_SNAPSHOTS_DIR
        if not folder.exists():
            return None
        prefix = f"{env.upper()}_"
        best: date | None = None
        for path in folder.glob(f"{prefix}*.json"):
            stem = path.stem[len(prefix):]
            try:
                as_of = datetime.strptime(stem, "%Y%m%d").date()
            except ValueError:
                continue
            if before is not None and as_of >= before:
                continue
            if best is None or as_of > best:
                best = as_of
        if best is None:
            return None
        book = self.load_book_snapshot(best, env)
        return (best, book if book is not None else {})

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
        "trade_date": submit.trade_date.isoformat() if submit.trade_date is not None else "",
        "claim_submit_id": submit.claim_submit_id or "",
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
        trade_date=_parse_date(row["trade_date"]) if row.get("trade_date") else None,
        claim_submit_id=row.get("claim_submit_id") or None,
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
