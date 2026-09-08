"""Target mode: cumulative sends can never exceed the day's target book.

Pure arithmetic — no store, no gRPC, no files. The submit path
(:mod:`ki_ops.kotl.submit`) assembles the inputs (ledger rows or Flex order
rows) and applies the outputs to its payload/order lists.

Invariant enforced per symbol: ``already_sent + residual == intended delta``
with ``|residual| <= |delta|`` and ``sign(residual) == sign(delta)`` (or the
residual is zero). Overshoot — prior sends past today's delta, e.g. after the
target file was regenerated lower — clips to **zero** and is reported; no
corrective (reverse) order is ever generated automatically.

Symbols are joined on the full Flex payload symbol (``AAPL.US``, canonical
master spelling): pre-submit resolution runs *before* the guard, so today's
payloads, the ledger payloads, and live ``GetOrderInfo2`` rows all speak the
same vocabulary — a same-day ticker rename cannot split a symbol's history.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from ki_ops.kotl.kelai_refresh import flex_side_label
from ki_ops.kotl.models import Submit, WorkingOrder
from ki_ops.kotl.qty import D, signed_qty
from ki_ops.models import Order

SENT_SOURCES = ("ledger", "flex")

# Residual line reasons.
FRESH = "fresh"  # nothing sent yet — full delta goes out
PARTIAL = "partial"  # part of the delta already sent — residual top-up
COVERED = "covered"  # delta fully sent — nothing to do
OVERSHOOT = "overshoot"  # sent past today's delta — clipped to zero, warn


class TargetModeViolation(RuntimeError):
    """Residual larger than the intended delta (prior send in the *opposite*
    direction) — cannot happen when every send derives from the same target
    book, so the submit aborts rather than trade an amplified quantity."""


@dataclass(frozen=True)
class ResidualLine:
    symbol: str
    delta: Decimal  # signed intended trade (target − SOD)
    already_sent: Decimal  # signed quantity already sent today
    residual: Decimal  # signed quantity still to send (clipped)
    reason: str  # FRESH / PARTIAL / COVERED / OVERSHOOT


@dataclass(frozen=True)
class ResidualReport:
    """Per-symbol residual audit — printed, written as CSV, and applied."""

    lines: tuple[ResidualLine, ...]

    @property
    def covered(self) -> bool:
        """True when nothing is left to send (clean no-op re-run)."""
        return all(line.residual == 0 for line in self.lines)

    @property
    def fresh(self) -> bool:
        """True when nothing was sent yet (first run — behavior unchanged)."""
        return all(line.already_sent == 0 for line in self.lines)

    @property
    def overshoots(self) -> tuple[ResidualLine, ...]:
        return tuple(line for line in self.lines if line.reason == OVERSHOOT)

    def residual_by_symbol(self) -> dict[str, Decimal]:
        return {line.symbol: line.residual for line in self.lines}

    def format_table(self) -> str:
        header = "target mode — residual vs already-sent"
        if self.fresh:
            return f"{header}\n  nothing sent yet for this (trade_date, env) — full delta goes out"
        cols = ("symbol", "delta", "already_sent", "residual", "reason")
        table = [
            (l.symbol, str(l.delta), str(l.already_sent), str(l.residual), l.reason)
            for l in self.lines
            if l.reason != FRESH
        ]
        widths = [len(c) for c in cols]
        for line in table:
            for i, cell in enumerate(line):
                widths[i] = max(widths[i], len(cell))
        fmt = lambda cells: "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
        out = [header, fmt(cols), fmt(tuple("-" * w for w in widths))]
        out.extend(fmt(line) for line in table)
        fresh_count = sum(1 for l in self.lines if l.reason == FRESH)
        if fresh_count:
            out.append(f"(+ {fresh_count} fresh symbol(s) with nothing sent yet)")
        if self.overshoots:
            shown = ", ".join(l.symbol for l in self.overshoots[:10])
            out.append(
                f"WARNING: {len(self.overshoots)} symbol(s) already sent PAST today's "
                f"delta ({shown}) — clipped to zero, NO corrective order is generated; "
                "unwind manually if the excess is unwanted"
            )
        return "\n".join(out)

    def to_csv_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": l.symbol,
                "delta": str(l.delta),
                "already_sent": str(l.already_sent),
                "residual": str(l.residual),
                "reason": l.reason,
            }
            for l in self.lines
        ]


def compute_residuals(
    deltas: Mapping[str, Decimal],
    already_sent: Mapping[str, Decimal],
) -> ResidualReport:
    """Signed intended deltas − signed already-sent → clipped residuals.

    *deltas* keys missing from *already_sent* are fresh (full delta).
    *already_sent* keys missing from *deltas* mean the current book wants no
    trade for a symbol that was already traded today — an overshoot row with
    ``delta=0`` (clipped, warned, never reversed).
    """
    lines: list[ResidualLine] = []
    for symbol in sorted(set(deltas) | set(already_sent)):
        delta = D(deltas.get(symbol, 0))
        sent = D(already_sent.get(symbol, 0))
        if sent == 0:
            lines.append(ResidualLine(symbol, delta, sent, delta, FRESH))
            continue
        residual = delta - sent
        if residual == 0:
            lines.append(ResidualLine(symbol, delta, sent, Decimal("0"), COVERED))
        elif delta == 0 or (residual > 0) != (delta > 0):
            # Sign flip: prior sends passed today's delta — clip, never reverse.
            lines.append(ResidualLine(symbol, delta, sent, Decimal("0"), OVERSHOOT))
        elif abs(residual) > abs(delta):
            # sent has the opposite sign of delta — sends derived from the same
            # target/SOD can never do this; refuse to trade an amplified qty.
            raise TargetModeViolation(
                f"{symbol}: residual {residual} exceeds intended delta {delta} "
                f"(already_sent={sent} has the opposite direction) — the ledger "
                "or the book is inconsistent; refusing to submit"
            )
        else:
            lines.append(ResidualLine(symbol, delta, sent, residual, PARTIAL))
    return ResidualReport(lines=tuple(lines))


def apply_residuals(
    payloads: Sequence[dict],
    orders: Sequence[Order],
    report: ResidualReport,
) -> tuple[list[dict], list[Order]]:
    """Rescale/drop the parallel payload+order lists to the clipped residuals.

    Covered and overshoot symbols drop out entirely; partial symbols keep
    their side (the clip guarantees the residual sign matches the delta) with
    the quantity reduced to the residual.
    """
    residuals = report.residual_by_symbol()
    kept_payloads: list[dict] = []
    kept_orders: list[Order] = []
    for payload, order in zip(payloads, orders):
        residual = residuals.get(str(payload["symbol"]).upper())
        if residual is None or residual == 0:
            continue
        qty = abs(residual)
        if qty != abs(D(payload["quantity"])):
            payload = dict(payload)
            payload["quantity"] = float(qty)
            order = replace(order, quantity=qty)
        kept_payloads.append(payload)
        kept_orders.append(order)
    return kept_payloads, kept_orders


# ---------------------------------------------------------------------------
# already-sent sources
# ---------------------------------------------------------------------------


def sent_from_ledger(
    submits: Sequence[Submit],
    working_orders: Sequence[WorkingOrder],
    *,
    env: str,
    subtract_fills: bool = False,
) -> dict[str, Decimal]:
    """Per-symbol signed quantity already sent today, from the KOTL ledger.

    *working_orders* must already be filtered to the trade date; submits join
    through their working orders' ``submit_id`` (submits carry no trade date).
    Only orders Flex **accepted** count (per-order ``success`` from the
    ``CreateOrders`` results); rejected orders never made it to the market.
    Cancelled orders DO count — conservative: the cancelled remainder can only
    be resent by an operator, never automatically.

    *subtract_fills* is for a Flex-sourced SOD book (``ReplayPositions``
    reflects intraday fills, so the filled part of a sent order is already
    inside ``target − SOD``): subtract each order's signed fill so it is not
    double-counted. Ledger fills lag until a refresh runs; a stale zero only
    over-counts "sent", which clips the residual DOWN — never a double-trade.
    """
    submit_ids = {w.submit_id for w in working_orders}
    fills_by_id = {w.flex_order_id: w.filled_qty for w in working_orders}
    sent: dict[str, Decimal] = {}
    for submit in submits:
        if submit.env != env or submit.submit_id not in submit_ids:
            continue
        results = (submit.flex_response or {}).get("results") or []
        for i, payload in enumerate(submit.payload):
            result = results[i] if i < len(results) else {}
            if not result.get("success", True):
                continue
            symbol = str(payload.get("symbol") or "").upper()
            qty = signed_qty(payload.get("side") or "", payload.get("quantity") or 0)
            if subtract_fills:
                qty -= fills_by_id.get(str(result.get("orderId") or ""), Decimal("0"))
            sent[symbol] = sent.get(symbol, Decimal("0")) + qty
    return {symbol: qty for symbol, qty in sent.items() if qty != 0}


def is_kotl_row(row: Mapping[str, Any]) -> bool:
    """True for GetOrderInfo2 rows created by KOTL (submit_id stamped in notes)."""
    return "submit_id=" in str(row.get("notes") or "")


def sent_from_flex_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    subtract_fills: bool = False,
) -> dict[str, Decimal]:
    """Per-symbol signed sent quantity from live ``GetOrderInfo2`` rows.

    The ``--sent-source flex`` recovery path (lost/corrupted ledger): *rows*
    are today's fund-split-aggregated rows, filtered here to KOTL-stamped
    orders (:func:`is_kotl_row`) so manual/non-KOTL trades never count.
    ``REJECTED`` rows are excluded — Flex never worked them.
    """
    from ki_ops.kotl.qty import flex_status_label

    sent: dict[str, Decimal] = {}
    for row in rows:
        if not is_kotl_row(row):
            continue
        if flex_status_label(row.get("status")) == "REJECTED":
            continue
        symbol = str(row.get("symbol") or "").upper()
        side = flex_side_label(row.get("side")) or ""
        qty = signed_qty(side, row.get("quantity") or 0)
        if subtract_fills:
            qty -= signed_qty(side, row.get("filledQuantity") or 0)
        sent[symbol] = sent.get(symbol, Decimal("0")) + qty
    return {symbol: qty for symbol, qty in sent.items() if qty != 0}


# ---------------------------------------------------------------------------
# ledger vs Flex cross-check
# ---------------------------------------------------------------------------

MISSING_FROM_FLEX = "missing-from-flex"
QTY_MISMATCH = "qty-mismatch"
UNKNOWN_IN_FLEX = "unknown-in-flex"


@dataclass(frozen=True)
class CrosscheckIssue:
    order_id: str
    symbol: str
    ledger_qty: Decimal | None  # unsigned
    flex_qty: Decimal | None  # unsigned
    problem: str


@dataclass(frozen=True)
class CrosscheckReport:
    issues: tuple[CrosscheckIssue, ...]
    checked: int  # ledger orders compared

    @property
    def ok(self) -> bool:
        return not self.issues

    def format_table(self) -> str:
        header = f"ledger vs Flex cross-check — {self.checked} ledger order(s)"
        if not self.issues:
            return f"{header}: OK"
        cols = ("order_id", "symbol", "ledger_qty", "flex_qty", "problem")
        table = [
            (
                i.order_id,
                i.symbol,
                "" if i.ledger_qty is None else str(i.ledger_qty),
                "" if i.flex_qty is None else str(i.flex_qty),
                i.problem,
            )
            for i in self.issues
        ]
        widths = [len(c) for c in cols]
        for line in table:
            for j, cell in enumerate(line):
                widths[j] = max(widths[j], len(cell))
        fmt = lambda cells: "  ".join(c.ljust(widths[j]) for j, c in enumerate(cells))
        out = [header, fmt(cols), fmt(tuple("-" * w for w in widths))]
        out.extend(fmt(line) for line in table)
        return "\n".join(out)


def crosscheck_ledger_vs_flex(
    ledger_orders: Sequence[WorkingOrder],
    flex_rows: Sequence[Mapping[str, Any]],
) -> CrosscheckReport:
    """Two-way reconciliation before any send (fund-split rows pre-aggregated).

    - every ledger order must exist in Flex with the same unsigned quantity
      (``missing-from-flex`` / ``qty-mismatch``);
    - every KOTL-stamped Flex row must exist in the ledger
      (``unknown-in-flex`` — the lost-ledger-write double-send scenario).
      Non-KOTL rows (manual trades, other desks) are ignored.
    """
    flex_by_id = {str(r.get("orderId") or "").upper(): r for r in flex_rows}
    issues: list[CrosscheckIssue] = []
    ledger_ids = set()
    for order in ledger_orders:
        oid = order.flex_order_id.upper()
        ledger_ids.add(oid)
        row = flex_by_id.get(oid)
        ledger_qty = abs(order.sent_qty)
        if row is None:
            issues.append(
                CrosscheckIssue(order.flex_order_id, order.symbol, ledger_qty, None, MISSING_FROM_FLEX)
            )
            continue
        flex_qty = abs(D(row.get("quantity") or 0))
        if flex_qty != ledger_qty:
            issues.append(
                CrosscheckIssue(order.flex_order_id, order.symbol, ledger_qty, flex_qty, QTY_MISMATCH)
            )
    for oid, row in flex_by_id.items():
        if oid in ledger_ids or not is_kotl_row(row):
            continue
        issues.append(
            CrosscheckIssue(
                str(row.get("orderId") or ""),
                str(row.get("symbol") or "").upper(),
                None,
                abs(D(row.get("quantity") or 0)),
                UNKNOWN_IN_FLEX,
            )
        )
    return CrosscheckReport(issues=tuple(issues), checked=len(ledger_orders))


# ---------------------------------------------------------------------------
# audit CSV
# ---------------------------------------------------------------------------

RESIDUAL_CSV_FIELDS = ("symbol", "delta", "already_sent", "residual", "reason")


def write_residual_csv(report: ResidualReport, dest: str | Path) -> str:
    """Write the residual audit CSV to a local path or ``s3://`` URL."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RESIDUAL_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in report.to_csv_rows():
        writer.writerow(row)
    text = buf.getvalue()
    dest_text = str(dest)
    if dest_text.startswith("s3://"):
        import boto3

        from ki_ops.kotl.kelaidata_source import parse_s3_url

        bucket, key = parse_s3_url(dest_text)
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))
    else:
        local = Path(dest_text)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text, encoding="utf-8")
    return dest_text
