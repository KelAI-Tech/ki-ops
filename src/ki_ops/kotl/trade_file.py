"""Trade file: submitted orders rendered as CSV + human-readable table.

One row per order sent (or planned, on ``--dry-run``):

``trade_date, symbol, side, quantity, order_type, algo, broker, fund,
position_group, submit_id, flex_order_id, status, dry_run, filled_qty,
final_status, finalization_status, cancel_status, rejection_reason``

``status`` is the CreateOrders gateway verdict at submit time (``submitted``
/ ``rejected``) — a Flex risk "rejected" order is still booked UNFINALIZED
and can be revived, so it is NOT the final word. The trailing disposition
columns start empty and are back-filled from the ledger by
:func:`apply_ledger_dispositions` (``kotl refresh --trade-file-out``):
``final_status`` is the order's live disposition (``filled`` / ``partial`` /
``working`` / ``unfinalized`` / ``cancel_pending`` / ``cancelled`` …), and
``filled_qty`` the unsigned filled quantity.

Written locally or to S3 (``boto3 put_object``). Default destination when a
``--strategy-id`` is given:
``s3://kelaitrading/trades/{strategy_id}/{yyyymmdd}/trades_{submit_id}.csv``
(dry runs get a ``_dryrun`` filename suffix on top of the ``dry_run`` column).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from ki_ops.kotl.kelaidata_source import parse_s3_url

TRADE_FILE_FIELDS = (
    "trade_date",
    "symbol",
    "side",
    "quantity",
    "order_type",
    "algo",
    "broker",
    "fund",
    "position_group",
    "submit_id",
    "flex_order_id",
    "status",
    "dry_run",
    # Back-filled from the ledger by apply_ledger_dispositions (empty at
    # submit time — dispositions only exist once the refresh has synced Flex).
    "filled_qty",
    "final_status",
    "finalization_status",
    "cancel_status",
    "rejection_reason",
)

TRADES_S3_TEMPLATE = "s3://kelaitrading/trades/{strategy_id}/{yyyymmdd}/{name}"


def build_trade_file_rows(
    *,
    trade_date: date,
    submit_id: str,
    payloads: Sequence[dict],
    results: Sequence[dict] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """One row per payload; *results* (adapter output) aligned by position."""
    rows: list[dict[str, Any]] = []
    for i, payload in enumerate(payloads):
        result = results[i] if results is not None else None
        if dry_run:
            status = "dry_run"
        elif result is None:
            status = "unknown"
        else:
            status = "submitted" if result.get("success", True) else "rejected"
        rows.append(
            {
                "trade_date": trade_date.isoformat(),
                "symbol": str(payload.get("symbol") or ""),
                "side": str(payload.get("side") or ""),
                "quantity": payload.get("quantity"),
                "order_type": str(payload.get("orderType") or ""),
                "algo": str(payload.get("algo") or ""),
                "broker": str(payload.get("broker") or ""),
                "fund": str(payload.get("fund") or ""),
                "position_group": str(payload.get("positionGroup") or ""),
                "submit_id": submit_id,
                "flex_order_id": str(result.get("orderId") or "") if result else "",
                "status": status,
                "dry_run": "true" if dry_run else "false",
            }
        )
    return rows


def format_trade_table(rows: Sequence[dict[str, Any]]) -> str:
    cols = ("symbol", "side", "quantity", "order_type", "algo", "broker", "flex_order_id", "status")
    table = [[str(r.get(c, "")) for c in cols] for r in rows]
    widths = [len(c) for c in cols]
    for line in table:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells) -> str:
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(cells))

    out = [fmt(cols), fmt(["-" * w for w in widths])]
    out.extend(fmt(line) for line in table)
    return "\n".join(out)


def default_trade_file_dest(
    *,
    trade_date: date,
    submit_id: str,
    strategy_id: str | None = None,
    data_dir: str | Path | None = None,
    dry_run: bool = False,
) -> str:
    yyyymmdd = trade_date.strftime("%Y%m%d")
    name = f"trades_{submit_id}{'_dryrun' if dry_run else ''}.csv"
    if strategy_id:
        return TRADES_S3_TEMPLATE.format(strategy_id=strategy_id, yyyymmdd=yyyymmdd, name=name)
    from ki_ops.kotl.store import DEFAULT_DATA_DIR

    base = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    return str(base / "trades" / yyyymmdd / name)


def render_trade_file_csv(rows: Sequence[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=TRADE_FILE_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in TRADE_FILE_FIELDS})
    return buf.getvalue()


def write_trade_file(
    rows: Sequence[dict[str, Any]],
    dest: str | Path,
    *,
    s3_client=None,
) -> str:
    """Write the trade file CSV to a local path or ``s3://`` URL; returns *dest*."""
    text = render_trade_file_csv(rows)
    dest_text = str(dest)
    if dest_text.startswith("s3://"):
        bucket, key = parse_s3_url(dest_text)
        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3")
        s3_client.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))
    else:
        local = Path(dest_text)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text, encoding="utf-8")
    return dest_text


def read_trade_file(dest: str | Path, *, s3_client=None) -> list[dict[str, str]]:
    """Read a trade file CSV back from a local path or ``s3://`` URL."""
    dest_text = str(dest)
    if dest_text.startswith("s3://"):
        bucket, key = parse_s3_url(dest_text)
        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3")
        text = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    else:
        text = Path(dest_text).read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def final_disposition(order) -> str:
    """The order's live disposition from ledger state (a ``WorkingOrder``).

    Collapses KOTL's fill status with Flex's independent workflow dimensions
    (finalization / cancel) into one operator-facing word. The key nuance: a
    Flex risk "rejected" order is booked UNFINALIZED — parked and revivable,
    not dead — so it reads ``unfinalized`` here, never ``cancelled``. Only a
    cancel workflow acked terminal (``cancelStatus=CANCELED``) or an order
    with no live claim reads ``cancelled``.
    """
    from ki_ops.kotl.enums import OrderStatus

    finalization = (order.finalization_status or "").upper()
    cancel = (order.cancel_status or "").upper()
    parked = finalization.startswith("UNFINALIZED") or finalization == "FINALIZATION_COMPLIANCE_FAILED"

    if order.status is OrderStatus.DONE:
        return "filled"
    if order.status is OrderStatus.CANCELLED:
        if cancel == "CANCELED":
            return "cancelled"
        if cancel in ("CANCEL_REQUESTED", "CANCEL_PENDING"):
            return "cancel_pending"
        if cancel == "CANCEL_REJECTED":
            return "cancel_rejected"
        # No cancel workflow: an UNFINALIZED order in the CANCELLED bucket is
        # the parked risk-reject — alive and revivable.
        return "unfinalized" if parked else "cancelled"
    base = "partial" if order.status is OrderStatus.PARTIAL else "working"
    return f"{base}_unfinalized" if parked else base


def apply_ledger_dispositions(
    dest: str | Path,
    orders,
    *,
    s3_client=None,
) -> tuple[str, int]:
    """Back-fill an existing trade file with ledger dispositions.

    Joins *orders* (``WorkingOrder`` rows) on ``flex_order_id`` and rewrites
    *dest* in place with ``filled_qty``, ``final_status`` and the raw Flex
    workflow columns updated; rows without a ledger match are left as-is.
    Returns ``(dest, matched_row_count)``.
    """
    rows = read_trade_file(dest, s3_client=s3_client)
    by_id = {str(o.flex_order_id): o for o in orders}
    matched = 0
    for row in rows:
        order = by_id.get(str(row.get("flex_order_id") or ""))
        if order is None:
            continue
        matched += 1
        row["filled_qty"] = str(abs(order.filled_qty))
        row["final_status"] = final_disposition(order)
        row["finalization_status"] = order.finalization_status or ""
        row["cancel_status"] = order.cancel_status or ""
        row["rejection_reason"] = order.rejection_reason or ""
    write_trade_file(rows, dest, s3_client=s3_client)
    return str(dest), matched
