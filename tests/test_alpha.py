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
    mapped = construct_lseg_trades_for_turnover(
        panel,
        sod_date="2026-08-05",
        target_turnover=Decimal("0.12"),
        ticker_by_infocode={"1001": "AAA", "1002": "BBB"},
    )
    by = {r["infocode"]: r["ticker"] for r in mapped["trade_rows"]}
    assert by["1001"] == "AAA"
    assert by["1002"] == "BBB"
    assert mapped["n_trades_with_ticker"] == 2
    priced = construct_lseg_trades_for_turnover(
        panel,
        sod_date="2026-08-05",
        target_turnover=Decimal("0.12"),
        ticker_by_infocode={"1001": "AAA", "1002": "BBB"},
        price_by_infocode={"1001": Decimal("10"), "1002": Decimal("5")},
    )
    assert abs(Decimal(priced["realized_turnover"]) - Decimal("0.12")) < Decimal("0.0000001")
    byp = {r["infocode"]: r for r in priced["trade_rows"]}
    assert Decimal(byp["1001"]["price"]) == Decimal("10")
    assert Decimal(byp["1001"]["notional"]) == Decimal(byp["1001"]["signed_quantity"]) * Decimal("10")
    assert priced["n_trades_with_px"] == 2


def test_run_perturb_uses_sod_and_trade_csv(tmp_path: Path, capsys):
    import json

    from ki_ops.cli import main

    sod = tmp_path / "sod_lseg_20260805.csv"
    sod.write_text(
        "infocode,ticker,notional\n1001,AAA,100\n1002,BBB,-100\n",
        encoding="utf-8",
    )
    trades = tmp_path / "trade_intents_lseg_20260806.csv"
    # quantity is share qty: 2×$10 + 4×$5 = $40 gross → one-way 10% of $200 GMV
    trades.write_text(
        "ticker,infocode,quantity\nAAA,1001,2\nBBB,1002,-4\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        "risk_management:\n"
        "  max_turnover: 0.25\n"
        "  max_position_size: 1000000\n"
        "  max_portfolio_value: 1000000\n"
        "  max_position_concentration: 1\n"
        "  min_order_size: 1\n"
        "  max_order_size: 1000000\n"
        "  max_orders_per_minute: 100000\n"
        "  allow_shorts: true\n"
        "  enforce_market_hours: false\n",
        encoding="utf-8",
    )
    px = tmp_path / "px.csv"
    px.write_text("infocode,close\n1001,10\n1002,5\n", encoding="utf-8")
    rc = main(
        [
            "run-perturb",
            "--sod",
            str(sod),
            "--trades",
            str(trades),
            "--config",
            str(cfg),
            "--prices",
            str(px),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["sod_source"] == "csv"
    assert out["perturb"] == "baseline"
    assert out["n_sod_names"] == 2
    assert out["n_orders"] == 2
    assert Decimal(out["turnover"]) == Decimal("0.10")
    assert out["passed"] is True


def test_real_lseg_baseline_allows_without_position_or_order_size(capsys):
    """Real POC CSVs: ~12% TO, no MAX_POSITION_SIZE / MAX_ORDER_SIZE."""
    import json

    from ki_ops.cli import main

    sod = ROOT / "examples" / "sod_lseg_20260805.csv"
    trades = ROOT / "examples" / "trade_intents_lseg_20260806.csv"
    if not sod.is_file() or not trades.is_file():
        pytest.skip("LSEG example CSVs not present")

    rc = main(["run-perturb", "--sod", str(sod), "--trades", str(trades)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["passed"] is True
    assert Decimal(out["turnover"]) < Decimal("0.25")
    assert "MAX_POSITION_SIZE" not in out["violation_codes"]
    assert "MAX_ORDER_SIZE" not in out["violation_codes"]
    assert "MIN_ORDER_SIZE" not in out["violation_codes"]


def test_run_perturb_turnover_breach(tmp_path: Path, capsys):
    import json

    from ki_ops.cli import main

    sod = ROOT / "examples" / "sod_lseg_20260805.csv"
    trades = ROOT / "examples" / "trade_intents_lseg_20260806.csv"
    if not sod.is_file() or not trades.is_file():
        pytest.skip("LSEG example CSVs not present")

    out_csv = tmp_path / "trade_intents_lseg_20260806_scaled.csv"
    rc = main(
        [
            "run-perturb-turnover",
            "--sod",
            str(sod),
            "--trades",
            str(trades),
            "--scaled-trades",
            str(out_csv),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["perturb"] == "max-turnover"
    assert out["sod_source"] == "csv"
    assert out["passed"] is False
    assert Decimal(out["turnover"]) >= Decimal("0.25")
    gmv = Decimal(out["projected_portfolio_value"])
    assert Decimal("89500000") <= gmv <= Decimal("90500000")
    assert out["violation_codes"] == ["MAX_TURNOVER"]
    assert out["prices_csv"] and Path(out["prices_csv"]).name == "ds2_px_20260804.csv"
    scaled = Path(out["trade_intents_file"])
    assert scaled.name == "trade_intents_lseg_20260806_scaled.csv"
    assert scaled.is_file()
    assert scaled.read_text(encoding="utf-8").startswith("ticker,infocode,quantity")


def test_run_perturb_zero_turnover(tmp_path: Path, capsys):
    import json

    from ki_ops.cli import main

    sod = ROOT / "examples" / "sod_lseg_20260805.csv"
    trades = ROOT / "examples" / "trade_intents_lseg_20260806.csv"
    if not sod.is_file() or not trades.is_file():
        pytest.skip("LSEG example CSVs not present")

    out_csv = tmp_path / "trade_intents_lseg_20260806_zero.csv"
    rc = main(
        [
            "run-perturb-zero",
            "--sod",
            str(sod),
            "--trades",
            str(trades),
            "--scaled-trades",
            str(out_csv),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["perturb"] == "zero-turnover"
    assert Decimal(out["turnover"]) == Decimal("0.00")
    assert out["passed"] == "with warnings"
    assert out["violation_codes"] == []
    assert "MAX_TURNOVER" not in out["violation_codes"]
    assert "MAX_POSITION_SIZE" in out["warning_codes"]
    zero = Path(out["trade_intents_file"])
    assert zero.name == "trade_intents_lseg_20260806_zero.csv"
    assert zero.is_file()
    body = zero.read_text(encoding="utf-8").strip().splitlines()
    assert body[0] == "ticker,infocode,quantity"
    assert all(line.endswith(",0") for line in body[1:])

