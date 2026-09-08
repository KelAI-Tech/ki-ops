"""Trade file: submitted orders rendered as CSV + human-readable table.

One row per order sent (or planned, on ``--dry-run``):

``trade_date, symbol, side, quantity, order_type, algo, broker, fund,
position_group, submit_id, flex_order_id, status, dry_run``

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
