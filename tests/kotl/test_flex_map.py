"""Tests for ki-ops → Flex order mapping (offline)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ki_ops.kotl.flex_map import (
    FlexOrderDefaults,
    flex_orders_from_rebalance_csv,
    flex_symbol,
    no_route_defaults,
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
    assert payload["accountType"] == "SWAP"  # desk requirement: Flex rejects non-Swap
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
        assert p["accountType"] == "SWAP"  # every order, buy and sell alike
        assert "submit_id=sub-1" in p["notes"]


def test_orders_to_flex_dicts_empty():
    assert orders_to_flex_dicts([]) == []


def test_no_route_defaults_blanks_only_routing_fields():
    cfg = no_route_defaults()
    assert cfg.broker == ""
    assert cfg.algo == ""
    assert cfg.broker_automation_type == "NO_AUTOMATION"
    base = FlexOrderDefaults()
    assert cfg.fund == base.fund
    assert cfg.position_group == base.position_group
    assert cfg.order_type == base.order_type
    assert cfg.time_in_force == base.time_in_force
    assert cfg.account_type == "SWAP"  # account type applies in no-route mode too

    custom = FlexOrderDefaults(position_group="OtherGroup")
    assert no_route_defaults(custom).position_group == "OtherGroup"


def test_no_route_payload_has_blank_broker_and_algo():
    order = Order("AMZN", Side.BUY, Decimal("12"), Decimal("186"), None)
    payload = order_to_flex_dict(order, defaults=no_route_defaults(), submit_id="sub-x")
    assert payload["broker"] == ""
    assert payload["algo"] == ""
    assert payload["brokerAutomationType"] == "NO_AUTOMATION"
    assert payload["symbol"] == "AMZN.US"  # everything else unchanged
    assert payload["accountType"] == "SWAP"  # the requirement applies regardless of routing


def test_account_type_default_is_swap_env_and_kwarg_override(monkeypatch):
    order = Order("AMZN", Side.BUY, Decimal("12"), Decimal("186"), None)

    monkeypatch.delenv("KOTL_FLEX_ACCOUNT_TYPE", raising=False)
    assert FlexOrderDefaults().account_type == "SWAP"

    # Env override (no code edit needed if the desk changes the requirement).
    monkeypatch.setenv("KOTL_FLEX_ACCOUNT_TYPE", "otc")
    assert FlexOrderDefaults().account_type == "OTC"
    assert order_to_flex_dict(order)["accountType"] == "OTC"

    # Explicit defaults (e.g. --account-type) beat the env.
    explicit = FlexOrderDefaults(account_type="PRIME")
    assert order_to_flex_dict(order, defaults=explicit)["accountType"] == "PRIME"
