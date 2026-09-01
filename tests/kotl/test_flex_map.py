"""Tests for ki-ops → Flex order mapping (offline)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ki_ops.kotl.flex_map import (
    FlexOrderDefaults,
    flex_orders_from_rebalance_csv,
    flex_symbol,
    order_to_flex_dict,
    orders_to_flex_dicts,
)
from ki_ops.models import Order, Side

ROOT = Path(__file__).resolve().parents[2]
SOD = ROOT / "examples" / "sod_positions.csv"
TARGETS = ROOT / "examples" / "target_intents.csv"


def test_flex_symbol_adds_us_suffix():
    assert flex_symbol("aapl") == "AAPL.US"
    assert flex_symbol("DASH.US") == "DASH.US"


def test_order_to_flex_dict_buy():
    order = Order("AMZN", Side.BUY, Decimal("12"), Decimal("186"), None, order_id="intent-amzn-buy")
    payload = order_to_flex_dict(order, submit_id="sub-abc")

    assert payload["symbol"] == "AMZN.US"
    assert payload["side"] == "BUY"
    assert payload["quantity"] == 12.0
    assert payload["fund"] == "KELAI"
    assert payload["positionGroup"] == "USATop2000_strategy_v1"
    assert payload["orderType"] == "MARKET"
    assert payload["algo"] == "VWAP_AMRS"
    assert "submit_id=sub-abc" in payload["notes"]
    assert "intent_id=intent-amzn-buy" in payload["notes"]


def test_flex_orders_from_example_rebalance_csv():
    payloads = flex_orders_from_rebalance_csv(SOD, TARGETS, submit_id="sub-1")
    assert len(payloads) == 14

    by_sym = {p["symbol"]: p for p in payloads}
    assert by_sym["AAPL.US"]["side"] == "SELL"
    assert by_sym["AAPL.US"]["quantity"] == 18.0
    assert by_sym["AMZN.US"]["side"] == "BUY"
    assert by_sym["AMZN.US"]["quantity"] == 12.0

    for p in payloads:
        assert p["fund"] == "KELAI"
        assert p["timeInForce"] == "GFD"
        assert "submit_id=sub-1" in p["notes"]


def test_orders_to_flex_dicts_empty():
    assert orders_to_flex_dicts([]) == []
