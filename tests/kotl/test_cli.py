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
