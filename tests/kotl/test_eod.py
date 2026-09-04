"""``ki-ops kotl eod`` — refresh, flatness, immutable snapshot, fills CSV."""

from __future__ import annotations

import contextlib
import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ki_ops.kotl.cli import run_kotl
from ki_ops.kotl.eod import run_eod
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import submit_rebalance_csv

ROOT = Path(__file__).resolve().parents[2]
SOD = ROOT / "examples" / "sod_positions.csv"
TARGETS = ROOT / "examples" / "target_intents.csv"
FIXTURE = ROOT / "examples" / "kotl" / "refresh_partial.json"
TD = date(2026, 8, 6)


def _seeded_store(tmp_path: Path) -> KotlStore:
    store = KotlStore(tmp_path / "kotl")
    submit_rebalance_csv(store, SOD, TARGETS, trade_date=TD)
    return store


def test_eod_snapshot_and_fills(tmp_path):
    store = _seeded_store(tmp_path)
    summary, report = run_eod(store, trade_date=TD, fixture=FIXTURE)

    assert summary["flat"] is False  # partial fills leave leaves outstanding
    assert summary["refreshed_count"] == 14
    assert summary["open_count"] == report.open_count >= 1
    assert summary["cancelled_count"] == 0
    assert Decimal(summary["total_abs_leaves"]) > 0

    snap = Path(summary["eod_dir"])
    assert snap == store.data_dir / "eod" / "2026-08-06"
    assert (snap / "working_orders.csv").is_file()
    payload = json.loads((snap / "report.json").read_text())
    assert payload["trade_date"] == "2026-08-06"
    assert len(payload["lines"]) == 14

    with (snap / "eod_fills_2026-08-06.csv").open(newline="") as fh:
        fills = {row["symbol"]: row for row in csv.DictReader(fh)}
    # AVGO/JPM/ORCL had zero fills in the fixture → excluded from recon CSV.
    assert not {"AVGO.US", "JPM.US", "ORCL.US"} & set(fills)
    aapl = fills["AAPL.US"]
    assert aapl["side"] == "SELL"
    assert Decimal(aapl["filled_qty"]) == Decimal("-10")
    assert Decimal(aapl["avg_fill_px"]) == Decimal("191.2")


def test_eod_snapshot_is_immutable(tmp_path):
    store = _seeded_store(tmp_path)
    run_eod(store, trade_date=TD, fixture=FIXTURE)
    with pytest.raises(FileExistsError, match="immutable"):
        run_eod(store, trade_date=TD, fixture=FIXTURE)


def test_eod_tolerance_makes_flat(tmp_path):
    store = _seeded_store(tmp_path)
    summary, report = run_eod(
        store, trade_date=TD, fixture=FIXTURE, tolerance=Decimal("100000")
    )
    assert summary["flat"] is True
    assert report.flat is True


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _eod_args(tmp_path: Path, **overrides) -> Args:
    base = dict(
        kotl_command="eod",
        trade_date=TD,
        fixture=FIXTURE,
        data_dir=tmp_path / "kotl",
        tolerance="0",
        json=True,
        notify=False,
        eod_dir=None,
    )
    base.update(overrides)
    return Args(**base)


def test_eod_cli_not_flat_exits_3(tmp_path):
    _seeded_store(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_eod_args(tmp_path))
    assert rc == 3
    summary = json.loads(buf.getvalue())
    assert summary["flat"] is False
    assert {"flat", "open_count", "cancelled_count", "total_abs_leaves"} <= set(summary)


def test_eod_cli_flat_exits_0_and_writes_to_eod_dir(tmp_path):
    _seeded_store(tmp_path)
    eod_dir = tmp_path / "custom_eod"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_eod_args(tmp_path, tolerance="100000", json=False, eod_dir=eod_dir))
    assert rc == 0
    out = buf.getvalue()
    assert "AAPL.US" in out  # human table before the summary JSON
    assert (eod_dir / "2026-08-06" / "report.json").is_file()


def test_eod_cli_notify_uses_dispatch(tmp_path, monkeypatch):
    _seeded_store(tmp_path)
    calls = {}

    def fake_dispatch(*, settings, subject, body, **kwargs):
        calls["subject"] = subject
        calls["body"] = body
        return {"email": True, "slack": False}

    monkeypatch.setattr("ki_ops.extras.notify.dispatch", fake_dispatch)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_eod_args(tmp_path, notify=True))
    assert rc == 3
    summary = json.loads(buf.getvalue())
    assert summary["notify"] == {"email": True, "slack": False}
    assert "flat=False" in calls["subject"]
    assert json.loads(calls["body"])["trade_date"] == "2026-08-06"
