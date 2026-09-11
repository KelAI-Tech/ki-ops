"""Nightly snapshot-book: fetch → record (first-wins) → ledger audit."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from ki_ops.kotl.book_snapshot import audit_book, ledger_fills, snapshot_flex_book
from ki_ops.kotl.models import Submit, WorkingOrder
from ki_ops.kotl.store import KotlStore

D1 = date(2026, 9, 9)
D2 = date(2026, 9, 10)
TS = datetime(2026, 9, 10, 20, 30, tzinfo=timezone.utc)


def _seed_orders(store, *, env: str, trade_date: date, fills: dict[str, str]) -> None:
    """One submit + one filled working order per (symbol, signed fill qty)."""
    submit = Submit.new(env=env, ok=True, payload=(), submitted_at=TS)
    store.append_submit(submit)
    rows = []
    for i, (symbol, qty) in enumerate(fills.items()):
        signed = Decimal(qty)
        side = "BUY" if signed >= 0 else "SELL"
        rows.append(
            WorkingOrder.from_flex_snapshot(
                submit_id=submit.submit_id,
                trade_date=trade_date,
                flex_order_id=f"{submit.submit_id}-{i}",
                symbol=symbol,
                side=side,
                fund="KELAI",
                position_group="USATop2000_strategy_v1",
                unsigned_sent_qty=abs(signed),
                unsigned_filled_qty=abs(signed),
                flex_status="FILLED",
                last_seen_at=TS,
            )
        )
    store.upsert_working_orders(rows)


def test_ledger_fills_sums_multi_step_and_filters_env(tmp_path):
    store = KotlStore(tmp_path)
    # two separate submits touch WM.US the same day — fills must sum
    _seed_orders(store, env="UAT", trade_date=D2, fills={"WM.US": "10"})
    _seed_orders(store, env="UAT", trade_date=D2, fills={"WM.US": "5", "CDNS.US": "-3"})
    # another env's fills must not leak in
    _seed_orders(store, env="PROD", trade_date=D2, fills={"WM.US": "999"})

    fills = ledger_fills(store, "UAT", [D2])
    assert fills == {"WM.US": Decimal("15"), "CDNS.US": Decimal("-3")}
    assert ledger_fills(store, "UAT", [D1]) == {}


def test_audit_book_clean_and_drift(tmp_path):
    store = KotlStore(tmp_path)
    prev_book = {"WM.US": Decimal("5"), "AAPL.US": Decimal("100")}
    _seed_orders(store, env="UAT", trade_date=D2, fills={"WM.US": "10", "AAPL.US": "-100"})

    # book == prev + fills → clean (AAPL flattened to 0 drops from the book)
    clean = audit_book(
        store,
        {"WM.US": Decimal("15")},
        env="UAT",
        prev_as_of=D1,
        prev_book=prev_book,
        as_of=D2,
    )
    assert clean.ok
    assert "no drift" in clean.format_table()

    # a manual trade the ledger never saw → drift
    drifted = audit_book(
        store,
        {"WM.US": Decimal("15"), "TSLA.US": Decimal("7")},
        env="UAT",
        prev_as_of=D1,
        prev_book=prev_book,
        as_of=D2,
    )
    assert not drifted.ok
    assert drifted.rows == (("TSLA.US", Decimal("7"), Decimal("0"), Decimal("7")),)
    assert drifted.total_abs_drift == Decimal("7")
    assert "DRIFT" in drifted.format_table()


def test_audit_window_spans_weekend(tmp_path):
    """Friday snapshot → Monday audit must include Saturday/Sunday dates."""
    store = KotlStore(tmp_path)
    fri, mon = date(2026, 9, 4), date(2026, 9, 7)
    _seed_orders(store, env="UAT", trade_date=date(2026, 9, 5), fills={"WM.US": "3"})
    result = audit_book(
        store,
        {"WM.US": Decimal("8")},
        env="UAT",
        prev_as_of=fri,
        prev_book={"WM.US": Decimal("5")},
        as_of=mon,
    )
    assert result.ok
    assert result.dates == (date(2026, 9, 5), date(2026, 9, 6), mon)


def test_snapshot_flex_book_records_first_wins_and_audits(tmp_path, capsys):
    store = KotlStore(tmp_path)

    # night 1 (bootstrap): records, audit skipped
    s1 = snapshot_flex_book(
        store,
        env="UAT",
        as_of=D1,
        flex_positions={"WM.US": Decimal("5")},
    )
    assert s1["recorded"] is True
    assert s1["audit"] is None
    assert "bootstrap night" in capsys.readouterr().out

    # night 2: ledger explains the move → audit ok
    _seed_orders(store, env="UAT", trade_date=D2, fills={"WM.US": "10"})
    s2 = snapshot_flex_book(
        store,
        env="UAT",
        as_of=D2,
        flex_positions={"WM.US": Decimal("15")},
    )
    assert s2["recorded"] is True
    assert s2["audit"] == {
        "prev_as_of": D1.isoformat(),
        "ok": True,
        "drift_names": 0,
        "drift_total_abs": "0",
    }
    assert store.load_book_snapshot(D2, "UAT") == {"WM.US": Decimal("15")}

    # re-run the same night: first-wins, still audited
    s3 = snapshot_flex_book(
        store,
        env="UAT",
        as_of=D2,
        flex_positions={"WM.US": Decimal("15")},
    )
    assert s3["recorded"] is False
    assert "first-wins" in capsys.readouterr().out


def test_snapshot_flex_book_flags_unexplained_drift(tmp_path, capsys):
    store = KotlStore(tmp_path)
    store.record_book_snapshot(D1, "UAT", {"WM.US": Decimal("5")})
    summary = snapshot_flex_book(
        store,
        env="UAT",
        as_of=D2,
        flex_positions={"WM.US": Decimal("6")},  # no ledger fill explains +1
    )
    # the snapshot is recorded anyway — it is the factual book
    assert summary["recorded"] is True
    assert summary["audit"]["ok"] is False
    assert summary["audit"]["drift_total_abs"] == "1"
    assert store.load_book_snapshot(D2, "UAT") == {"WM.US": Decimal("6")}
    assert "DRIFT" in capsys.readouterr().out


def test_snapshot_flex_book_dry_run_writes_nothing(tmp_path, capsys):
    store = KotlStore(tmp_path)
    summary = snapshot_flex_book(
        store,
        env="UAT",
        as_of=D1,
        flex_positions={"WM.US": Decimal("5")},
        dry_run=True,
    )
    assert summary["recorded"] is False
    assert store.load_book_snapshot(D1, "UAT") is None
    assert "DRY RUN" in capsys.readouterr().out
