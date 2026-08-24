"""Regression tests for robustness fixes: packaging-safe CLI import, zero-GMV
handling, duplicate CSV rows, strict YAML config, and missing-price findings."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ki_ops.config import RiskManagementSettings
from ki_ops.engine import PreTradeEngine, format_decimal
from ki_ops.intents import load_sod_positions_csv, load_target_intents_csv
from ki_ops.models import Order, Side
from ki_ops.portfolio import portfolio_from_holdings

TS = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)

PERMISSIVE = dict(
    max_position_size=Decimal("1000000"),
    max_portfolio_value=Decimal("1000000"),
    max_position_concentration=Decimal("1"),
    min_order_size=Decimal("1"),
    max_order_size=Decimal("1000000"),
    max_orders_per_minute=100000,
    max_turnover=Decimal("1"),
)


def test_format_decimal_handles_non_finite():
    assert format_decimal(Decimal("Infinity")) == "Infinity"
    assert format_decimal(Decimal("-Infinity")) == "-Infinity"
    assert format_decimal("1.005") == "1.01"


def test_zero_gmv_book_blocks_instead_of_crashing():
    """Trades against an empty book must be a structured block, not a stack trace."""
    result = PreTradeEngine(settings=RiskManagementSettings(**PERMISSIVE)).evaluate(
        portfolio_from_holdings([], cash=0),
        [Order("X", Side.BUY, 10, 100, TS)],
    )
    assert result.allowed is False
    assert "ZERO_GMV_BASE" in {v.code for v in result.violations}
    # to_dict formats the Infinity turnover without raising InvalidOperation.
    out = result.to_dict()
    assert out["turnover"] == "Infinity"
    assert out["passed"] is False


def test_duplicate_sod_symbols_raise(tmp_path: Path):
    p = tmp_path / "sod.csv"
    p.write_text("infocode,ticker,notional\n100,AAA,5000\n100,AAA,7000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate symbols in SOD"):
        load_sod_positions_csv(p)


def test_duplicate_target_symbols_raise(tmp_path: Path):
    p = tmp_path / "targets.csv"
    p.write_text("symbol,quantity,market_price\nAAPL,10,100\nAAPL,20,100\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate symbols in target"):
        load_target_intents_csv(p)


def test_unknown_yaml_key_rejected():
    with pytest.raises(ValueError, match="Unknown risk_management keys: max_turnver"):
        RiskManagementSettings.from_mapping({"risk_management": {"max_turnver": 0.1}})


def test_string_bools_parse_strictly():
    s = RiskManagementSettings.from_mapping({"risk_management": {"allow_shorts": "false"}})
    assert s.allow_shorts is False
    s = RiskManagementSettings.from_mapping({"risk_management": {"allow_shorts": "true"}})
    assert s.allow_shorts is True
    with pytest.raises(ValueError, match="Invalid boolean"):
        RiskManagementSettings.from_mapping({"risk_management": {"allow_shorts": "maybe"}})


def test_missing_price_blocks_run_perturb(tmp_path: Path, capsys):
    """A live trade intent absent from the px map must block, not silently
    fall back to unit price (which understates turnover and corrupts the
    share-based position size check)."""
    import json

    from ki_ops.cli import main

    sod = tmp_path / "sod.csv"
    sod.write_text("infocode,ticker,notional\n1001,AAA,100\n1002,BBB,-100\n", encoding="utf-8")
    trades = tmp_path / "trades.csv"
    trades.write_text("ticker,infocode,quantity\nAAA,1001,2\nBBB,1002,-4\n", encoding="utf-8")
    px = tmp_path / "px.csv"
    px.write_text("infocode,close\n1001,10\n", encoding="utf-8")  # 1002 missing
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        "risk_management:\n"
        "  max_turnover: 1\n"
        "  max_position_size: 1000000\n"
        "  max_portfolio_value: 1000000\n"
        "  max_position_concentration: 1\n"
        "  max_orders_per_minute: 100000\n",
        encoding="utf-8",
    )

    rc = main(
        ["run-perturb", "--sod", str(sod), "--trades", str(trades), "--prices", str(px), "--config", str(cfg)]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["passed"] is False
    assert "MISSING_PRICE" in out["violation_codes"]
    assert "MISSING_PRICE_SOD" in out["warning_codes"]
    assert "1002" in out["violations"][-1]["message"] or any(
        "1002" in v["message"] for v in out["violations"]
    )


def test_cli_parser_builds_without_poc_manifest(monkeypatch, tmp_path: Path):
    """Installed wheels don't ship config/ or examples/ — building the parser
    (and importing the CLI) must not require the POC manifest."""
    import ki_ops.cli as cli

    monkeypatch.setattr(cli, "DEFAULT_POC_DATA", tmp_path / "does_not_exist.yaml")
    parser = cli._parser()
    assert parser is not None
    # The lazy help label degrades gracefully instead of raising.
    assert "POC manifest" in cli._poc_default_label("sod")
