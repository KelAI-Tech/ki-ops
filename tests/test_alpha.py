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
    assert sod.gmv == Decimal("200000")

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
        )
    )
    days = run_alpha_panel_checks(_tiny_panel(), engine)
    assert len(days) == 2
    # Day1→2: Δ 35k traded; two-way 35k/200k = 0.175
    assert days[0].turnover == Decimal("0.175")
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
    # GMV = 215; two-way 12% -> gross 0.12*215 = 25.8
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
    # quantity is share qty: 2×$10 + 4×$5 = $40 gross → two-way 20% of $200 GMV
    trades.write_text(
        "ticker,infocode,quantity\nAAA,1001,2\nBBB,1002,-4\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        "risk_management:\n"
        "  turnover_mean: 0.5\n"
        "  turnover_std: 0.1\n"
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
    master = tmp_path / "master.csv"
    master.write_text(
        "INFOCODE,TICKER,STATUSCODE,ISACTIVE,DELISTDATE\n"
        "1001,AAA,A,True,\n"
        "1002,BBB,A,True,\n",
        encoding="utf-8",
    )
    poc = tmp_path / "poc.yaml"
    poc.write_text(
        f"sod: {sod}\ntrades: {trades}\nprices: {px}\nsecurity_master: {master}\n",
        encoding="utf-8",
    )
    rc = main(
        [
            "run-perturb-baseline",
            "--poc-data",
            str(poc),
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
    assert out["perturb"] == "baseline"
    assert out["input"]["sod_source"] == "csv"
    assert out["output"]["n_sod_names"] == 2
    assert out["output"]["n_orders"] == 2
    assert Decimal(out["output"]["turnover"]) == Decimal("0.20")
    assert "two-way" in out["input"]["turnover_convention"]
    assert out["output"]["passed"] is True
    assert "config_hash" in out["input"]
    assert "sod" in out["input"]["input_hashes"]
    assert out["input"]["adv"] is None
    assert out["input"]["ticker_mapping_dt"] is None
    assert out["input"]["security_master"] == f"{master}: tradability_isactive"
    json_path = tmp_path / "artifact.json"
    rc2 = main(
        [
            "run-perturb-baseline",
            "--poc-data",
            str(poc),
            "--sod",
            str(sod),
            "--trades",
            str(trades),
            "--config",
            str(cfg),
            "--prices",
            str(px),
            "--json-out",
            str(json_path),
        ]
    )
    assert rc2 == 0
    disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert disk["input"]["config_hash"] == out["input"]["config_hash"]
    assert disk["output"]["passed"] is True


def test_real_lseg_baseline_warns_adv_and_drops_delisted_name(capsys):
    """Delisted 56992 is dropped (warn); infocode 335446 exceeds 10% of ADV20_ADJ (warn)."""
    import json

    from ki_ops.cli import main

    sod = ROOT / "examples" / "sod_lseg_20260805.csv"
    trades = ROOT / "examples" / "trade_intents_lseg_20260806.csv"
    adv = ROOT / "examples" / "lseg_base_data_us_dt_20260804.csv"
    if not sod.is_file() or not trades.is_file() or not adv.is_file():
        pytest.skip("LSEG example CSVs not present")

    rc = main(["run-perturb-baseline", "--sod", str(sod), "--trades", str(trades)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["output"]["passed"] == "with warnings"
    assert out["output"]["violation_codes"] == []
    assert "MAX_ADV_PARTICIPATION" in out["output"]["warning_codes"]
    assert Decimal(out["input"]["max_adv_participation"]) == Decimal("0.10")
    assert Path(out["input"]["adv"]).name == "lseg_base_data_us_dt_20260804.csv"
    assert out["input"]["security_master"].endswith("lseg_security_master_dt.csv: tradability_isactive")
    assert Path(out["input"]["ticker_mapping_dt"]).name == "lseg_ticker_mapping_dt.csv"
    assert "NOT_TRADABLE" in out["output"]["warning_codes"]
    assert "NOT_TRADABLE" not in out["output"]["violation_codes"]
    assert Decimal(out["output"]["turnover"]) < Decimal("0.25")
    assert "two-way" in out["input"]["turnover_convention"]
    assert "MAX_POSITION_SIZE" not in out["output"]["violation_codes"]
    assert "MAX_ORDER_SIZE" not in out["output"]["violation_codes"]
    assert "MIN_ORDER_SIZE" not in out["output"]["violation_codes"]


def test_run_perturb_var_checks_breaches_turnover(tmp_path: Path, capsys):
    import json

    from ki_ops.cli import main

    sod = ROOT / "examples" / "sod_lseg_20260805.csv"
    trades = ROOT / "examples" / "trade_intents_lseg_20260806.csv"
    if not sod.is_file() or not trades.is_file():
        pytest.skip("LSEG example CSVs not present")
    if not (ROOT / "examples" / "lseg_base_data_us_dt_20260804.csv").is_file():
        pytest.skip("ADV snapshot CSV not present")

    trades_copy = tmp_path / "trade_intents_lseg_20260806.csv"
    trades_copy.write_text(trades.read_text(encoding="utf-8"), encoding="utf-8")
    rc = main(
        [
            "run-perturb-var-checks",
            "--sod",
            str(sod),
            "--trades",
            str(trades_copy),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["perturb"] == "var-checks"
    assert out["input"]["sod_source"] == "csv"
    assert out["output"]["passed"] is False
    assert Decimal(out["output"]["turnover"]) >= Decimal("0.33")
    # Scaler targets positions-only GMV ~90M; reported value is GMV+cash,
    # so allow ~1% slack for the cash generated by net selling.
    gmv = Decimal(out["output"]["projected_portfolio_value"])
    assert Decimal("89000000") <= gmv <= Decimal("91000000")
    assert Decimal(out["input"]["max_net_exposure"]) == Decimal("0.10")
    assert Decimal(out["input"]["max_adv_participation"]) == Decimal("0.10")
    assert out["output"]["violation_codes"] == ["MAX_TURNOVER"]
    assert "MAX_ADV_PARTICIPATION" in out["output"]["warning_codes"]
    assert "NOT_TRADABLE" in out["output"]["warning_codes"]
    assert out["input"]["prices_csv"] and Path(out["input"]["prices_csv"]).name == "lseg_datastream2_px_20260804.csv"
    var_checks = Path(out["input"]["trade_intents_file"])
    assert var_checks.name == "trade_intents_lseg_20260806_var_checks.csv"
    assert var_checks.is_file()
    assert var_checks.read_text(encoding="utf-8").startswith("ticker,infocode,quantity")


def test_run_perturb_zero_turnover(tmp_path: Path, capsys):
    import json

    from ki_ops.cli import main

    sod = ROOT / "examples" / "sod_lseg_20260805.csv"
    trades = ROOT / "examples" / "trade_intents_lseg_20260806.csv"
    if not sod.is_file() or not trades.is_file():
        pytest.skip("LSEG example CSVs not present")
    if not (ROOT / "examples" / "lseg_base_data_us_dt_20260804.csv").is_file():
        pytest.skip("ADV snapshot CSV not present")

    trades_copy = tmp_path / "trade_intents_lseg_20260806.csv"
    trades_copy.write_text(trades.read_text(encoding="utf-8"), encoding="utf-8")
    rc = main(
        [
            "run-perturb-zero",
            "--sod",
            str(sod),
            "--trades",
            str(trades_copy),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["perturb"] == "zero-turnover"
    assert Decimal(out["output"]["turnover"]) == Decimal("0.00")
    assert out["output"]["passed"] == "with warnings"
    assert out["output"]["violation_codes"] == []
    assert "MAX_TURNOVER" not in out["output"]["violation_codes"]
    assert "MAX_POSITION_SIZE" in out["output"]["warning_codes"]
    zero = Path(out["input"]["trade_intents_file"])
    assert zero.name == "trade_intents_lseg_20260806_zero.csv"
    assert zero.is_file()
    body = zero.read_text(encoding="utf-8").strip().splitlines()
    assert body[0] == "ticker,infocode,quantity"
    assert all(line.endswith(",0") for line in body[1:])

