"""CLI smoke tests for kotl (offline)."""

from __future__ import annotations

import contextlib
import io
from datetime import date
from pathlib import Path

from ki_ops.kotl.cli import run_kotl

ROOT = Path(__file__).resolve().parents[2]


def test_kotl_cli_submit_refresh_status(tmp_path):
    data_dir = tmp_path / "kotl"

    class Args:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    rc = run_kotl(
        Args(
            kotl_command="submit-rebalance",
            sod=ROOT / "examples" / "sod_positions.csv",
            targets=ROOT / "examples" / "target_intents.csv",
            trade_date=date(2026, 8, 6),
            data_dir=data_dir,
        )
    )
    assert rc == 0
    assert (data_dir / "submits.csv").exists()

    rc = run_kotl(
        Args(
            kotl_command="refresh",
            trade_date=date(2026, 8, 6),
            fixture=ROOT / "examples" / "kotl" / "refresh_partial.json",
            data_dir=data_dir,
        )
    )
    assert rc == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="status",
                trade_date=date(2026, 8, 6),
                data_dir=data_dir,
                json=False,
            )
        )
    assert rc == 0
    assert "AAPL.US" in buf.getvalue()
    assert "flat=False" in buf.getvalue()


def test_kotl_cli_submit_kelai(tmp_path):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares = tmp_path / "20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")
    sod = tmp_path / "sod.csv"
    sod.write_text("symbol,quantity,market_price\nAAPL,20,190\n")
    data_dir = tmp_path / "kotl"

    class Args:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="submit-kelai",
                trade_date=date(2026, 8, 6),
                shares=str(shares),
                ds2=str(h5),
                sod=sod,
                assume_flat_sod=False,
                data_dir=data_dir,
                cache_dir=tmp_path / "cache",
            )
        )
    assert rc == 0
    assert (data_dir / "working_orders.csv").exists()
    assert '"order_count": 2' in buf.getvalue()
