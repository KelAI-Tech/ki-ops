"""Filled-trade CSV helpers (extras)."""

from __future__ import annotations

from pathlib import Path

from ki_ops.extras.trades import load_trades_csv, summarize_trades


def test_load_sample_trades(tmp_path: Path):
    path = tmp_path / "trades.csv"
    path.write_text(
        "trade_id,symbol,side,quantity,price,timestamp,fees\n"
        "1,AAPL,BUY,10,100,2026-08-11T14:00:00Z,1\n"
        "2,AAPL,SELL,4,110,2026-08-11T15:00:00Z,1\n",
        encoding="utf-8",
    )
    summary = summarize_trades(load_trades_csv(path))
    assert summary["count"] == 2
    assert summary["gross_notional"] == "1440"
