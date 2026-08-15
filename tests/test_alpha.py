"""Tests for alpha dollar panel → theoretical SOD / targets POC."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ki_ops.alpha import (
    load_alpha_dollar_panel,
    portfolio_from_dollar_row,
    run_alpha_panel_checks,
    summarize_alpha_days,
    targets_from_dollar_row,
)
from ki_ops.config import RiskManagementSettings
from ki_ops.engine import PreTradeEngine

TS = datetime(2026, 7, 2, 14, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
ALPHA_PARQUET = ROOT / "examples" / (
    "df_combo_lseg_v2c_00233cb52db9baa05a20329d01af6420f88241854b6c66b3e9da066884abfae8"
    "_neut_C5_cap125_nosv.parquet"
)


def _tiny_panel() -> pd.DataFrame:
    idx = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"])
    return pd.DataFrame(
        {
            "1001": [100_000.0, 110_000.0, 90_000.0],
            "1002": [-100_000.0, -105_000.0, -95_000.0],
            "1003": [float("nan"), 20_000.0, 0.0],
        },
        index=idx,
    )


def test_dollar_row_to_portfolio_and_targets():
    panel = _tiny_panel()
    sod = portfolio_from_dollar_row(panel.loc[panel.index[0]])
    assert sod.qty("1001") == Decimal("100000")
    assert sod.qty("1002") == Decimal("-100000")
    assert "1003" not in sod.holdings
    assert sod.gross_exposure == Decimal("200000")

    targets = targets_from_dollar_row(panel.loc[panel.index[1]])
    by_sym = {t.symbol: t for t in targets}
    assert by_sym["1001"].quantity == Decimal("110000")
    assert by_sym["1003"].quantity == Decimal("20000")


def test_day_over_day_turnover_on_tiny_panel():
    engine = PreTradeEngine(
        settings=RiskManagementSettings(
            max_position_size=Decimal("1000000"),
            max_portfolio_value=Decimal("1000000"),
            max_position_concentration=Decimal("1"),
            min_order_size=Decimal("1"),
            max_order_size=Decimal("1000000"),
            max_orders_per_minute=100000,
            max_turnover=Decimal("1"),
        )
    )
    days = run_alpha_panel_checks(_tiny_panel(), engine)
    assert len(days) == 2
    # Day1→2: Δ 35k traded; one-way (35k/2)/200k = 0.0875
    assert days[0].turnover == Decimal("0.0875")
    assert days[0].allowed is True
    summary = summarize_alpha_days(days)
    assert summary["n_rebalance_days"] == 2
    assert summary["n_allowed"] == 2


def test_load_august5_panel_if_present():
    if not ALPHA_PARQUET.is_file():
        pytest.skip("alpha parquet not present")
    panel = load_alpha_dollar_panel(ALPHA_PARQUET, start="2026-08-05", end="2026-08-05")
    assert len(panel.index) == 1
    assert panel.index.max() == pd.Timestamp("2026-08-05")


def test_construct_lseg_trades_hits_12pct_turnover():
    idx = pd.to_datetime(["2026-08-04", "2026-08-05"])
    panel = pd.DataFrame(
        {"1001": [100.0, 110.0], "1002": [-100.0, -105.0]},
        index=idx,
    )
    from ki_ops.alpha import construct_lseg_trades_for_turnover

    built = construct_lseg_trades_for_turnover(panel, sod_date="2026-08-05", target_turnover=Decimal("0.12"))
    assert built["n_trades"] == 2
    # GMV = 215; one-way 12% -> gross 2*0.12*215 = 51.6
    to = Decimal(built["realized_turnover"])
    assert abs(to - Decimal("0.12")) < Decimal("0.0000001")
    assert built["prior_template_date"] == "2026-08-04"
    assert Decimal(built["scale_k"]) > 0

