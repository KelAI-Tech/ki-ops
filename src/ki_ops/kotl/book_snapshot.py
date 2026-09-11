"""Nightly portfolio snapshot: live Flex book → store, plus a ledger audit.

``ki-ops kotl snapshot-book`` runs every night after the close:

1. **refresh** the ledger's fill state from live ``GetOrderInfo2`` for every
   date in the audit window (so the audit never fires on stale zero fills);
2. fetch the full signed position book via ``ReplayPositions``
   (:func:`ki_ops.kotl.flex_live.fetch_flex_positions`);
3. **record** it first-wins under ``(as_of, env)``
   (``store.record_book_snapshot``) — this is the next morning's SOD recon
   baseline in :func:`ki_ops.kotl.submit.submit_kelai_shares`;
4. **audit**: ``book − (previous snapshot + ledger fills since)`` should be
   zero. Drift means the book moved in a way the ledger doesn't know —
   manual Flex trades, fills the refresh missed, corporate actions — and is
   the intraday alarm (CLI exit 8). The snapshot is recorded either way: it
   is the factual book, and tomorrow's recon must compare against reality.

Everything is keyed by canonical Flex symbols (``WM.US``, ``BF/B.US``) — the
same vocabulary ``ReplayPositions`` returns and working orders store, so no
ticker normalization is involved anywhere in the snapshot/recon/audit loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ki_ops.kotl.flex_map import FlexOrderDefaults
from ki_ops.kotl.models import WorkingOrder

AUDIT_ROWS_SHOWN = 50


def ledger_fills(store, env: str, dates: list[date]) -> dict[str, Decimal]:
    """Signed per-symbol fills of *env*'s KOTL orders across *dates*.

    Straight per-order facts (``WorkingOrder.filled_qty`` is signed and keyed
    by the canonical Flex symbol): multi-step sends sum, rejected orders carry
    zero fills and drop out. Working orders don't store the env, so rows join
    to it via their submit.
    """
    env_submit_ids = {s.submit_id for s in store.load_submits() if s.env == env}
    fills: dict[str, Decimal] = {}
    for d in dates:
        for w in store.load_working_orders(trade_date=d):
            if w.submit_id not in env_submit_ids or w.filled_qty == 0:
                continue
            sym = w.symbol.upper()
            fills[sym] = fills.get(sym, Decimal("0")) + w.filled_qty
    return {sym: qty for sym, qty in fills.items() if qty != 0}


@dataclass(frozen=True)
class BookAudit:
    """``live book − (previous snapshot + ledger fills)`` per symbol."""

    prev_as_of: date
    dates: tuple[date, ...]
    # symbol, book_qty, expected_qty, drift — nonzero drift rows only
    rows: tuple[tuple[str, Decimal, Decimal, Decimal], ...]
    total_abs_drift: Decimal
    fill_symbols: int

    @property
    def ok(self) -> bool:
        return not self.rows

    def format_table(self) -> str:
        window = (
            self.dates[0].isoformat()
            if len(self.dates) == 1
            else f"{self.dates[0].isoformat()}..{self.dates[-1].isoformat()}"
        )
        header = (
            f"book audit — live book vs {self.prev_as_of.isoformat()} snapshot "
            f"+ ledger fills ({window}, {self.fill_symbols} symbol(s) filled)"
        )
        if self.ok:
            return f"{header}\n  no drift"
        cols = ("symbol", "book_qty", "expected_qty", "drift")
        shown = self.rows[:AUDIT_ROWS_SHOWN]
        table = [tuple(str(c) for c in row) for row in shown]
        widths = [len(c) for c in cols]
        for line in table:
            for i, cell in enumerate(line):
                widths[i] = max(widths[i], len(cell))
        fmt = lambda cells: "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
        out = [header, fmt(cols), fmt(tuple("-" * w for w in widths))]
        out.extend(fmt(line) for line in table)
        if len(self.rows) > len(shown):
            out.append(f"  … and {len(self.rows) - len(shown)} more")
        out.append(
            f"DRIFT: {len(self.rows)} symbol(s), total_abs={self.total_abs_drift} "
            "shares — the book moved in a way the ledger does not explain "
            "(manual trades? missed fills?)"
        )
        return "\n".join(out)


def audit_book(
    store,
    book: dict[str, Decimal],
    *,
    env: str,
    prev_as_of: date,
    prev_book: dict[str, Decimal],
    as_of: date,
) -> BookAudit:
    """Diff *book* against ``prev_book + ledger fills`` over ``(prev_as_of, as_of]``."""
    dates = [
        prev_as_of + timedelta(days=i) for i in range(1, (as_of - prev_as_of).days + 1)
    ]
    fills = ledger_fills(store, env, dates)
    expected: dict[str, Decimal] = {s.upper(): q for s, q in prev_book.items()}
    for sym, qty in fills.items():
        expected[sym] = expected.get(sym, Decimal("0")) + qty
    live = {str(s).upper(): q for s, q in book.items()}
    rows = []
    total = Decimal("0")
    for sym in sorted(set(live) | set(expected)):
        drift = live.get(sym, Decimal("0")) - expected.get(sym, Decimal("0"))
        if drift != 0:
            rows.append((sym, live.get(sym, Decimal("0")), expected.get(sym, Decimal("0")), drift))
            total += abs(drift)
    return BookAudit(
        prev_as_of=prev_as_of,
        dates=tuple(dates),
        rows=tuple(rows),
        total_abs_drift=total,
        fill_symbols=len(fills),
    )


def snapshot_flex_book(
    store,
    *,
    env: str,
    as_of: date,
    flex_config=None,
    position_group: str | None = None,
    symbol_suffix: str = ".US",
    refresh_source=None,
    audit: bool = True,
    dry_run: bool = False,
    flex_positions: dict[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Fetch, record (first-wins) and audit the *env* book as of *as_of*.

    *refresh_source* (a ``FlexRefreshSource``, e.g. ``LiveRefreshSource``)
    refreshes the ledger for every audit-window date that has working orders,
    before fills are read. *flex_positions* injects the book directly (tests /
    offline replays) instead of calling ``ReplayPositions``. ``dry_run``
    fetches and audits but never writes. Returns the CLI's JSON summary; the
    caller maps ``audit.ok == False`` to exit 8.
    """
    if flex_positions is None:
        from ki_ops.kotl.flex_live import fetch_flex_positions, load_flex_config

        flex_config = flex_config or load_flex_config(flex_env=env.upper())
        flex_positions, _ = fetch_flex_positions(
            flex_config,
            position_group=position_group or FlexOrderDefaults().position_group,
            symbol_suffix=symbol_suffix,
        )
    book = {str(sym).upper(): qty for sym, qty in flex_positions.items()}
    total_abs = sum((abs(q) for q in book.values()), Decimal("0"))
    print(
        f"flex book {env} as of {as_of.isoformat()}: {len(book)} symbol(s), "
        f"total_abs={total_abs} shares"
    )

    summary: dict[str, Any] = {
        "command": "kotl-snapshot-book",
        "env": env,
        "as_of": as_of.isoformat(),
        "symbols": len(book),
        "total_abs_shares": str(total_abs),
        "dry_run": dry_run,
        "recorded": False,
        "audit": None,
    }

    prev = store.load_latest_book_snapshot(env, before=as_of)

    if dry_run:
        print("DRY RUN: snapshot not recorded")
    else:
        recorded = store.record_book_snapshot(as_of, env, book)
        summary["recorded"] = recorded
        if recorded:
            print(f"book snapshot recorded: ({as_of.isoformat()}, {env})")
        else:
            print(
                f"book snapshot already exists for ({as_of.isoformat()}, {env}) — "
                "kept (first-wins)"
            )

    if not audit:
        return summary
    if prev is None:
        print(
            f"book audit skipped: no prior {env} snapshot before "
            f"{as_of.isoformat()} (bootstrap night)"
        )
        return summary

    prev_as_of, prev_book = prev
    if refresh_source is not None:
        from ki_ops.kotl.refresh import refresh_working_orders

        refreshed: dict[str, int] = {}
        for i in range(1, (as_of - prev_as_of).days + 1):
            d = prev_as_of + timedelta(days=i)
            updated = refresh_working_orders(store, d, refresh_source)
            if updated:
                refreshed[d.isoformat()] = len(updated)
        if refreshed:
            print(
                "ledger refreshed before audit: "
                + ", ".join(f"{d}: {n} order(s)" for d, n in sorted(refreshed.items()))
            )
        summary["refreshed"] = refreshed

    result = audit_book(
        store, book, env=env, prev_as_of=prev_as_of, prev_book=prev_book, as_of=as_of
    )
    print(result.format_table())
    summary["audit"] = {
        "prev_as_of": prev_as_of.isoformat(),
        "ok": result.ok,
        "drift_names": len(result.rows),
        "drift_total_abs": str(result.total_abs_drift),
    }
    return summary
