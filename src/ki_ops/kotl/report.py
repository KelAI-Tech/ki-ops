"""Sent / done / left status report."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from ki_ops.kotl.flex_reasons import is_exposure_calc_warning, rejection_kind
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


def fills_rows(orders: Sequence[WorkingOrder]) -> list[WorkingOrder]:
    """Orders sorted for the fills view: most recently updated first."""
    return sorted(orders, key=lambda o: (o.last_seen_at, o.symbol), reverse=True)


def fills_to_dicts(orders: Sequence[WorkingOrder]) -> list[dict]:
    return [
        {
            "flex_order_id": o.flex_order_id,
            "submit_id": o.submit_id,
            "trade_date": o.trade_date.isoformat(),
            "symbol": o.symbol,
            "side": o.side,
            "sent_qty": str(o.sent_qty),
            "filled_qty": str(o.filled_qty),
            "leaves_qty": str(o.leaves_qty),
            "avg_fill_px": str(o.avg_fill_px) if o.avg_fill_px is not None else None,
            "status": o.status.value,
            "finalization_status": o.finalization_status,
            "cancel_status": o.cancel_status,
            "rejection_reason": o.rejection_reason,
            # calc_warning: Flex exposure-calc data gap (retryable) vs a
            # true rejection — see ki_ops.kotl.flex_reasons.
            "rejection_kind": rejection_kind(o.rejection_reason),
            "last_seen_at": o.last_seen_at.isoformat(),
        }
        for o in fills_rows(orders)
    ]


def format_fills_table(
    orders: Sequence[WorkingOrder],
    *,
    trade_date: date,
    header_note: str = "",
) -> str:
    """Fills view: one row per working order, most recently updated first."""
    header = f"KOTL fills — {trade_date.isoformat()}"
    if header_note:
        header = f"{header} ({header_note})"
    cols = ("symbol", "side", "sent", "filled", "left", "avg_px", "status", "last_update")
    rows = [
        (
            o.symbol,
            o.side,
            str(o.sent_qty),
            str(o.filled_qty),
            str(o.leaves_qty),
            str(o.avg_fill_px) if o.avg_fill_px is not None else "-",
            o.status.value,
            o.last_seen_at.strftime("%Y-%m-%d %H:%M:%SZ"),
        )
        for o in fills_rows(orders)
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

    counts = {s: 0 for s in ("open", "partial", "done", "cancelled")}
    total_abs_filled = Decimal("0")
    for o in orders:
        counts[o.status.value] = counts.get(o.status.value, 0) + 1
        total_abs_filled += abs(o.filled_qty)
    lines.append("")
    summary = (
        f"summary: orders={len(orders)} open={counts['open']} "
        f"partial={counts['partial']} done={counts['done']} "
        f"cancelled={counts['cancelled']} total_abs_filled={total_abs_filled}"
    )
    create_rejected = [o for o in orders if o.rejection_reason]
    if create_rejected:
        calc_warnings = sum(
            1 for o in create_rejected if is_exposure_calc_warning(o.rejection_reason)
        )
        summary += (
            f" create_rejected={len(create_rejected)} "
            f"(calc_warnings={calc_warnings}, "
            f"true_rejections={len(create_rejected) - calc_warnings})"
        )
    lines.append(summary)
    return "\n".join(lines)


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
