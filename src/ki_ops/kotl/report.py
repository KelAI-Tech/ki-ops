"""Sent / done / left status report."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from ki_ops.kotl.models import WorkingOrder
from ki_ops.kotl.qty import is_flat


@dataclass(frozen=True)
class StatusLine:
    flex_order_id: str
    symbol: str
    side: str
    sent_qty: Decimal
    filled_qty: Decimal
    leaves_qty: Decimal
    status: str
    submit_id: str


@dataclass(frozen=True)
class StatusReport:
    trade_date: date
    lines: tuple[StatusLine, ...]
    total_abs_leaves: Decimal
    flat: bool
    open_count: int
    partial_count: int
    done_count: int
    cancelled_count: int

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["trade_date"] = self.trade_date.isoformat()
        payload["lines"] = [
            {
                **asdict(line),
                "sent_qty": str(line.sent_qty),
                "filled_qty": str(line.filled_qty),
                "leaves_qty": str(line.leaves_qty),
            }
            for line in self.lines
        ]
        payload["total_abs_leaves"] = str(self.total_abs_leaves)
        return payload


def build_status_report(
    orders: Sequence[WorkingOrder],
    *,
    trade_date: date,
    flat_tolerance: Decimal = Decimal("0"),
) -> StatusReport:
    lines = tuple(
        StatusLine(
            flex_order_id=o.flex_order_id,
            symbol=o.symbol,
            side=o.side,
            sent_qty=o.sent_qty,
            filled_qty=o.filled_qty,
            leaves_qty=o.leaves_qty,
            status=o.status.value,
            submit_id=o.submit_id,
        )
        for o in sorted(orders, key=lambda x: (x.symbol, x.flex_order_id))
    )
    leaves = [line.leaves_qty for line in lines]
    counts = {s: 0 for s in ("open", "partial", "done", "cancelled")}
    for line in lines:
        counts[line.status] = counts.get(line.status, 0) + 1

    total_abs = sum((abs(x) for x in leaves), Decimal("0"))
    return StatusReport(
        trade_date=trade_date,
        lines=lines,
        total_abs_leaves=total_abs,
        flat=is_flat(leaves, tolerance=flat_tolerance),
        open_count=counts["open"],
        partial_count=counts["partial"],
        done_count=counts["done"],
        cancelled_count=counts["cancelled"],
    )


def format_status_table(report: StatusReport) -> str:
    header = f"KOTL status — {report.trade_date.isoformat()}"
    cols = ("symbol", "side", "sent", "done", "left", "status")
    rows = [
        (
            line.symbol,
            line.side,
            str(line.sent_qty),
            str(line.filled_qty),
            str(line.leaves_qty),
            line.status,
        )
        for line in report.lines
    ]
    widths = [len(c) for c in cols]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [
        header,
        fmt_row(cols),
        fmt_row(tuple("-" * w for w in widths)),
    ]
    lines.extend(fmt_row(r) for r in rows)
    lines.append("")
    lines.append(
        f"summary: open={report.open_count} partial={report.partial_count} "
        f"done={report.done_count} cancelled={report.cancelled_count} "
        f"total_abs_leaves={report.total_abs_leaves} flat={report.flat}"
    )
    return "\n".join(lines)
