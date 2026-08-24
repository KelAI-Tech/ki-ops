"""EMS trade-intent file + prior-day alpha dollar px approximation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from ki_ops.config import RiskManagementSettings
from ki_ops.extras.ems_intents import (
    EmsIntent,
    approx_px_from_alpha_dollars,
    approximate_ems_prices_from_alpha,
    enrich_intents_with_prior_alpha_px,
    evaluate_ems_against_alpha_sod,
    load_ems_trade_intents_csv,
    parse_asof_from_filename,
    prior_alpha_date,
)
from ki_ops.engine import PreTradeEngine


def test_parse_asof_and_headerless_load(tmp_path: Path):
    p = tmp_path / "Portfolio_20260813.csv"
    p.write_text("AAPL,10,VWAP\nMSFT,0,VWAP\nTSLA,-4,VWAP\n", encoding="utf-8")
    assert parse_asof_from_filename(p) == date(2026, 8, 13)
    rows = load_ems_trade_intents_csv(p)
    assert [r.symbol for r in rows] == ["AAPL", "MSFT", "TSLA"]
    assert rows[0].quantity == Decimal("10")
    assert rows[2].quantity == Decimal("-4")


def test_load_ems_header_and_infocode(tmp_path: Path):
    p = tmp_path / "Portfolio_20260806.csv"
    p.write_text(
        "ticker,quantity,algo,infocode\nAAPL,10,VWAP,13407\nMSFT,0,VWAP,\n",
        encoding="utf-8",
    )
    rows = load_ems_trade_intents_csv(p)
    assert [r.symbol for r in rows] == ["AAPL", "MSFT"]
    assert rows[0].security_id == "13407"
    assert rows[1].security_id is None


def test_px_is_abs_dollars_over_abs_qty():
    assert approx_px_from_alpha_dollars(Decimal("10"), Decimal("1910")) == Decimal("191")
    assert approx_px_from_alpha_dollars(Decimal("-5"), Decimal("-1000")) == Decimal("200")
    assert approx_px_from_alpha_dollars(Decimal("0"), Decimal("100")) is None


def test_enrich_uses_id_map_and_skips_zeros():
    intents = [
        EmsIntent("AAA", 10, "VWAP"),
        EmsIntent("BBB", 0, "VWAP"),
        EmsIntent("CCC", -2, "VWAP"),
    ]
    dollars = {"1001": Decimal("5000"), "1002": Decimal("-800")}
    id_map = {"AAA": "1001", "CCC": "1002"}
    out = enrich_intents_with_prior_alpha_px(intents, dollars, id_map)
    assert out[0].px_approx == Decimal("500")
    assert out[0].px_source == "prior_alpha_usd / qty"
    assert out[1].px_approx is None
    assert out[1].px_source == "no_trade"
    assert out[2].px_approx == Decimal("400")


def test_end_to_end_prior_day_before_asof(tmp_path: Path):
    panel = pd.DataFrame(
        {"1001": [1000.0, 1910.0], "1002": [-400.0, -800.0]},
        index=pd.to_datetime(["2026-08-05", "2026-08-12"]),
    )
    pq = tmp_path / "alpha.parquet"
    panel.to_parquet(pq)
    intents = tmp_path / "Portfolio_20260813.csv"
    intents.write_text("AAA,10,VWAP\nCCC,-2,VWAP\nZZZ,3,VWAP\n", encoding="utf-8")
    id_map = tmp_path / "map.csv"
    id_map.write_text("security_id,symbol\n1001,AAA\n1002,CCC\n", encoding="utf-8")

    enriched, summary, prior = approximate_ems_prices_from_alpha(
        intents, pq, id_map_csv=id_map
    )
    assert prior == "2026-08-12"
    by = {i.symbol: i for i in enriched}
    assert by["AAA"].px_approx == Decimal("191")
    assert by["CCC"].px_approx == Decimal("400")
    assert by["ZZZ"].px_source == "unmapped_ticker"
    assert summary["n_priced"] == 2
    assert summary["n_unmapped_live"] == 1


def test_prior_alpha_date_strictly_before():
    panel = pd.DataFrame(
        {"1001": [1.0, 2.0, 3.0]},
        index=pd.to_datetime(["2026-08-05", "2026-08-06", "2026-08-13"]),
    )
    ts = prior_alpha_date(panel, date(2026, 8, 13))
    assert str(ts.date()) == "2026-08-06"


def test_evaluate_parquet_sod_vs_ems_intents(tmp_path: Path):
    panel = pd.DataFrame(
        {"1001": [1910.0], "1002": [-800.0]},
        index=pd.to_datetime(["2026-08-05"]),
    )
    pq = tmp_path / "alpha.parquet"
    panel.to_parquet(pq)
    intents = tmp_path / "Portfolio_drop.csv"
    intents.write_text("AAA,10,VWAP\n", encoding="utf-8")
    id_map = tmp_path / "map.csv"
    id_map.write_text("security_id,symbol\n1001,AAA\n", encoding="utf-8")
    engine = PreTradeEngine(
        settings=RiskManagementSettings(
            max_position_size=Decimal("10000000"),
            max_portfolio_value=Decimal("10000000"),
            max_position_concentration=Decimal("1"),
            min_order_size=Decimal("1"),
            max_order_size=Decimal("10000000"),
            max_orders_per_minute=100000,
            max_turnover=Decimal("1"),
        )
    )
    out = evaluate_ems_against_alpha_sod(
        intents, pq, engine, as_of="2026-08-07", id_map_csv=id_map
    )
    assert out["sod_date"] == "2026-08-05"
    assert out["as_of"] == "2026-08-07"
    assert out["n_sod_names"] == 2
    assert out["sod_source"] == "parquet"
    assert out["n_orders"] == 1
    assert Decimal(out["sod_gmv"]) == Decimal("2710")
    # two-way: 1910 traded / 2710 GMV ≈ 0.70
    assert Decimal(out["turnover"]) == Decimal("0.70")


def test_scale_ems_targets_hits_turnover_band(tmp_path: Path):
    from ki_ops.extras.ems_intents import scale_ems_targets_to_turnover, write_target_intents_csv

    intents = [
        EmsIntent("AAA", 10, "VWAP"),
        EmsIntent("BBB", -5, "VWAP"),
        EmsIntent("CCC", 0, "VWAP"),
    ]
    px = {"AAA": Decimal("10"), "BBB": Decimal("20"), "CCC": Decimal("1")}
    # raw gross = 100+100=200; two-way vs 10000 GMV = 0.02; k = 0.12/0.02 = 6
    scaled, stats = scale_ems_targets_to_turnover(
        intents, px, sod_gross=Decimal("10000"), target_turnover=Decimal("0.12")
    )
    assert Decimal(stats["scale_k"]) == Decimal("6")
    assert Decimal(stats["realized_turnover"]) == Decimal("0.12")
    by = {i.symbol: i for i in scaled}
    assert by["AAA"].quantity == Decimal("60")
    assert by["BBB"].quantity == Decimal("-30")
    assert by["CCC"].quantity == Decimal("0")
    out = write_target_intents_csv(scaled, tmp_path / "tgt.csv")
    text = out.read_text()
    assert "CCC" not in text
    assert "AAA" in text
