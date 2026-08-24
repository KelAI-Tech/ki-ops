from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import csv

import pytest

from ki_ops.cli import _load_orders
from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.engine import PreTradeEngine
from ki_ops.intents import (
    TargetIntent,
    derive_trade_intents,
    load_sod_positions_csv,
    load_symbol_volatilities,
    load_target_intents_csv,
)
from ki_ops.models import Holding, Order, Side
from ki_ops.portfolio import portfolio_from_holdings, turnover_ratio

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "risk_management_small_book.yaml"
POC_CONFIG = ROOT / "config" / "risk_management_poc.yaml"
EXAMPLES = ROOT / "examples"
TS = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def loose(**kwargs) -> RiskManagementSettings:
    base = dict(
        enforce_market_hours=False,
        allow_shorts=True,
        min_order_size=Decimal("1"),
        max_order_size=Decimal("100000"),
        max_position_size=Decimal("100000"),
        max_position_concentration=Decimal("1"),
        max_turnover=Decimal("1"),
        max_orders_per_minute=100,
    )
    base.update(kwargs)
    return RiskManagementSettings(**base)


def test_load_risk_settings_matches_yaml():
    s = load_risk_settings(CONFIG)
    assert s.max_position_volatility == Decimal("0.3")
    assert s.volatility_lookback == 30
    assert s.use_stop_losses is False
    assert s.enforce_market_hours is False
    assert s.allow_shorts is True
    assert s.max_position_size == Decimal("4000")
    assert s.max_turnover == Decimal("0.25")  # two-way (≈ 12.5% one-way)
    assert s.max_adv_participation == Decimal("0")
    assert s.max_portfolio_value == Decimal("200000")


def test_poc_data_manifest_resolves():
    from ki_ops.poc_data import load_poc_data_paths

    poc = load_poc_data_paths(POC_CONFIG.parent / "poc_pos_and_px.yaml")
    assert poc.sod.is_file()
    assert poc.trades.is_file()
    assert poc.prices.is_file()
    assert poc.ticker_map.is_file()
    assert poc.sod.name == "sod_lseg_20260805.csv"
    assert poc.trades.name == "trade_intents_lseg_20260806.csv"
    assert poc.prices.name == "ds2_px_20260804.csv"
    assert poc.ticker_map.name == "lseg_security_master.csv"
    assert poc.adv is not None
    assert poc.adv.is_file()
    assert poc.adv.name == "base_data_us_adv_20260804.csv"


def test_volatility_blocks_when_above_limit():
    result = PreTradeEngine(settings=loose(max_position_volatility=Decimal("0.3"))).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("AAPL", Side.BUY, 5, 100, TS)],
        volatilities={"AAPL": Decimal("0.45")},
    )
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_POSITION_VOLATILITY"}
    assert any(v.symbol == "AAPL" for v in result.violations)


def test_volatility_allows_when_at_or_below_limit():
    engine = PreTradeEngine(settings=loose(max_position_volatility=Decimal("0.3")))
    portfolio = portfolio_from_holdings([], cash=10000)
    orders = [Order("AAPL", Side.BUY, 5, 100, TS)]

    under = engine.evaluate(portfolio, orders, volatilities={"AAPL": Decimal("0.2")})
    assert under.allowed is True
    assert not any(v.code == "MAX_POSITION_VOLATILITY" for v in under.violations)

    at_limit = engine.evaluate(portfolio, orders, volatilities={"AAPL": Decimal("0.3")})
    assert at_limit.allowed is True
    assert not any(v.code == "MAX_POSITION_VOLATILITY" for v in at_limit.violations)


def test_volatility_skips_symbols_without_vol_input():
    result = PreTradeEngine(settings=loose(max_position_volatility=Decimal("0.3"))).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("AAPL", Side.BUY, 5, 100, TS), Order("MSFT", Side.BUY, 5, 100, TS)],
        volatilities={"MSFT": Decimal("0.5")},  # only MSFT provided
    )
    codes = {(v.code, v.symbol) for v in result.violations}
    assert ("MAX_POSITION_VOLATILITY", "MSFT") in codes
    assert ("MAX_POSITION_VOLATILITY", "AAPL") not in codes


def test_shorts_allowed_when_enabled():
    result = PreTradeEngine(settings=loose()).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("AAPL", Side.SELL, 10, 100, TS)],
    )
    assert result.allowed is True


def test_shorts_blocked_when_disabled():
    result = PreTradeEngine(settings=loose(allow_shorts=False)).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("AAPL", Side.SELL, 10, 100, TS)],
    )
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"INSUFFICIENT_HOLDINGS"}


def test_negative_target_derives_short_intent():
    intents = derive_trade_intents(
        portfolio_from_holdings([], cash=10000),
        [TargetIntent("AAPL", -10, 100)],
    )
    assert intents[0].side is Side.SELL
    assert intents[0].display_label == "SHORT_SELL"


def test_display_label_sell_vs_short_sell():
    sod = portfolio_from_holdings([Holding("AAPL", 10, 100)], cash=1000)
    assert derive_trade_intents(sod, [TargetIntent("AAPL", 4, 100)])[0].display_label == "SELL"
    short = derive_trade_intents(sod, [TargetIntent("AAPL", -5, 100)])[0]
    assert short.quantity == Decimal("15")
    assert short.display_label == "SHORT_SELL"


def test_trade_intents_are_target_minus_sod():
    sod = portfolio_from_holdings(
        [Holding("AAPL", 50, 190), Holding("MSFT", 20, 420), Holding("GOOG", 5, 175)],
        cash=20000,
    )
    intents = {
        o.symbol: o
        for o in derive_trade_intents(
            sod,
            [TargetIntent("AAPL", 40, 191), TargetIntent("MSFT", 25, 418), TargetIntent("NVDA", 10, 120)],
            timestamp=TS,
        )
    }
    assert intents["AAPL"].quantity == 10 and intents["AAPL"].display_label == "SELL"
    assert intents["MSFT"].side is Side.BUY and intents["MSFT"].quantity == 5
    assert intents["GOOG"].side is Side.SELL
    assert intents["NVDA"].side is Side.BUY


def _signed_mvs(rows):
    """rows: iterable of (qty, price) -> (long_mv, abs_short_mv, net_mv)."""
    long_mv = abs_short = Decimal("0")
    for qty, px in rows:
        v = Decimal(str(qty)) * Decimal(str(px))
        if v >= 0:
            long_mv += v
        else:
            abs_short += abs(v)
    return long_mv, abs_short, long_mv - abs_short


def test_example_sod_is_dollar_neutral():
    sod = load_sod_positions_csv(EXAMPLES / "sod_positions.csv")
    long_mv, abs_short, net = _signed_mvs(
        (h.quantity, h.market_price) for h in sod.holdings.values()
    )
    assert long_mv > 0 and abs_short > 0
    # SOD may be roughly neutral
    assert abs(net) / long_mv < Decimal("0.01")
    assert sod.get("TSLA").quantity < 0
    assert sod.get("AAPL").quantity > 0


def test_example_targets_are_dollar_neutral():
    targets = load_target_intents_csv(EXAMPLES / "target_intents.csv")
    long_mv, abs_short, net = _signed_mvs((t.quantity, t.market_price) for t in targets)
    assert long_mv > 0 and abs_short > 0
    assert long_mv == abs_short
    assert net == 0
    shorts = {t.symbol for t in targets if t.quantity < 0}
    assert shorts >= {"META", "TSLA", "BA", "AMD", "COIN"}


def test_sod_allows_negative_short_quantity():
    sod = load_sod_positions_csv(EXAMPLES / "sod_positions.csv")
    assert sod.get("TSLA").quantity == Decimal("-80")
    assert sod.get("META").quantity == Decimal("-50")
    assert sod.get("BA").quantity == Decimal("-70")
    assert sod.get("AAPL").quantity == Decimal("78")


def test_targets_include_short_positions():
    targets = {t.symbol: t for t in load_target_intents_csv(EXAMPLES / "target_intents.csv")}
    shorts = {s for s, t in targets.items() if t.quantity < 0}
    assert shorts >= {"META", "TSLA", "BA", "AMD", "COIN"}


def test_derive_intents_from_short_sod():
    sod = portfolio_from_holdings(
        [
            Holding("TSLA", Decimal("-8"), Decimal("250")),
            Holding("META", Decimal("-5"), Decimal("510")),
        ],
        cash=20000,
    )
    intents = {
        o.symbol: o
        for o in derive_trade_intents(
            sod,
            [
                TargetIntent("TSLA", Decimal("-5"), Decimal("248")),  # cover 3
                TargetIntent("META", Decimal("-8"), Decimal("512")),  # short more 3
            ],
        )
    }
    assert intents["TSLA"].side is Side.BUY and intents["TSLA"].quantity == Decimal("3")
    assert intents["META"].side is Side.SELL and intents["META"].quantity == Decimal("3")
    assert intents["META"].display_label == "SHORT_SELL"


def test_example_files_block_on_max_position_volatility():
    sod = load_sod_positions_csv(EXAMPLES / "sod_positions.csv")
    targets = load_target_intents_csv(EXAMPLES / "target_intents.csv")
    vols = load_symbol_volatilities(EXAMPLES / "sod_positions.csv", EXAMPLES / "target_intents.csv")
    assert vols["TSLA"] > Decimal("0.3")
    assert vols["NVDA"] > Decimal("0.3")

    result = PreTradeEngine(
        settings=loose(max_position_volatility=Decimal("0.3"), max_position_size=Decimal("1000000"))
    ).evaluate_from_targets(sod, targets, volatilities=vols)

    vol_blocks = [v for v in result.violations if v.code == "MAX_POSITION_VOLATILITY"]
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_POSITION_VOLATILITY"}
    assert {v.symbol for v in vol_blocks} >= {"TSLA", "NVDA", "AMD", "BA", "COIN"}


def test_concentration_blocks_overweight_buy():
    """Buy that pushes one name above max_position_concentration blocks."""
    # Cash-only SOD is under the cap; $25k AAPL → 100% of the $25k position GMV > 20%.
    sod = portfolio_from_holdings([], cash=100000)
    assert PreTradeEngine(settings=loose(max_position_concentration=Decimal("0.2"))).evaluate(
        sod, []
    ).allowed is True

    result = PreTradeEngine(settings=loose(max_position_concentration=Decimal("0.2"))).evaluate(
        sod,
        [Order("AAPL", Side.BUY, 500, 50, TS)],  # $25k
    )
    assert result.allowed is False
    conc = [v for v in result.violations if v.code == "MAX_POSITION_CONCENTRATION"]
    assert {v.symbol for v in conc} == {"AAPL"}
    assert result.projected_portfolio_value == Decimal("100000")


def test_turnover_limit():
    portfolio = portfolio_from_holdings([Holding("AAPL", 100, 100)], cash=0)  # GMV 10000
    orders = [Order("MSFT", Side.BUY, 20, 100, TS)]
    # two-way: 2000/10000 = 0.20
    assert turnover_ratio(portfolio, orders) == Decimal("0.2")
    result = PreTradeEngine(settings=loose(max_turnover=Decimal("0.05"))).evaluate(portfolio, orders)
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_TURNOVER"}


def test_yaml_turnover_limit_blocks_over_25pct():
    """30% two-way must trip the YAML max_turnover 0.25 (two-way) without other limits."""
    settings = loose(max_turnover=Decimal("0.25"))
    portfolio = portfolio_from_holdings([Holding("SPY", 200, 100)], cash=0)  # GMV 20000
    # 3 × $2,000 buys → two-way 6000/20000 = 0.30
    orders = [
        Order("AAPL", Side.BUY, 20, 100, TS),
        Order("MSFT", Side.BUY, 20, 100, TS),
        Order("GOOG", Side.BUY, 20, 100, TS),
    ]
    assert turnover_ratio(portfolio, orders) == Decimal("0.3")
    result = PreTradeEngine(settings=settings).evaluate(portfolio, orders)
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_TURNOVER"}


def test_max_position_size_warns_over_yaml_cap():
    # Cap is abs share qty — warn only, does not block.
    result = PreTradeEngine(
        settings=loose(max_position_size=Decimal("4000"), max_position_concentration=Decimal("1"))
    ).evaluate(
        portfolio_from_holdings([Holding("BIG", 5000, 1)], cash=0),
        [],
    )
    assert result.allowed is True
    assert not any(v.code == "MAX_POSITION_SIZE" for v in result.violations)
    assert any(v.code == "MAX_POSITION_SIZE" for v in result.warnings)


def test_max_portfolio_value_uses_gmv_plus_cash():
    """Dollar-neutral book: deployed-capital cap (GMV + cash), not net NAV.

    SOD: |AAPL| + |TSLA| = 20k position GMV; + 5k cash = 25k deployed (net 5k).
    Short-open MSFT $10k: new |MSFT| 10k and cash +10k → GMV+cash 45k > 30k cap.
    Net equity stays 5k, so a NAV-based cap would not trip.
    """
    sod = portfolio_from_holdings(
        [
            Holding("AAPL", 100, 100),   # +10000
            Holding("TSLA", -50, 200),  # -10000
        ],
        cash=5000,
    )
    assert sod.total_value == Decimal("5000")
    assert sod.gmv == Decimal("20000")            # positions only
    assert sod.gmv_plus_cash == Decimal("25000")  # deployed capital
    result = PreTradeEngine(settings=loose(max_portfolio_value=Decimal("30000"))).evaluate(
        sod,
        [Order("MSFT", Side.SELL, 100, 100, TS)],
    )
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_PORTFOLIO_VALUE"}
    assert result.projected_portfolio_value == Decimal("45000")  # GMV + cash, not net


def test_lseg_poc_csvs_breach_yaml_turnover(tmp_path: Path):
    """Parquet-style SOD + POC trade CSV: 60% two-way trips YAML max_turnover 0.25.

    SOD matches ``examples/sod_lseg_*.csv`` (infocode, ticker, integer notional).
    Trades match ``examples/trade_intents_lseg_*.csv`` (ticker, infocode, signed qty).
    Uses the POC YAML so a long/short book does not also trip retail order caps.
    """
    settings = load_risk_settings(POC_CONFIG)
    assert settings.max_turnover == Decimal("0.25")  # two-way
    assert settings.max_position_size == Decimal("300000")

    # 20 long + 20 short at $10k: GMV $400k; each name 2.5% < POC 3% concentration.
    seed_long = [("36100", "CSCO"), ("39988", "MSFT"), ("42241", "BAP"), ("46092", "CAT")]
    seed_short = [("6347", "SCCO"), ("39985", "ORCL"), ("40142", "C"), ("45293", "AMZN")]
    longs = seed_long + [(str(81000 + i), f"L{i:02d}") for i in range(16)]
    shorts = seed_short + [(str(82000 + i), f"S{i:02d}") for i in range(16)]
    sod_n, trade_n = 10000, 6000  # gross $240k → two-way 60% of $400k GMV

    sod_path = tmp_path / "sod_lseg_20260805.csv"
    sod_path.write_text(
        "infocode,ticker,notional\n"
        + "".join(f"{sid},{tic},{sod_n}\n" for sid, tic in longs)
        + "".join(f"{sid},{tic},{-sod_n}\n" for sid, tic in shorts),
        encoding="utf-8",
    )
    # Mix of +qty / −qty like the 8/6 POC file. All trades add to |position|
    # so projected concentration stays 2.5% (POC cap is 3%).
    trd_path = tmp_path / "trade_intents_lseg_20260806.csv"
    lines = ["ticker,infocode,quantity\n"]
    for sid, tic in longs:
        lines.append(f"{tic},{sid},{trade_n}\n")
    for sid, tic in shorts:
        lines.append(f"{tic},{sid},{-trade_n}\n")
    trd_path.write_text("".join(lines), encoding="utf-8")

    sod = load_sod_positions_csv(sod_path)
    orders = _load_orders(trd_path)
    assert sod.gmv == Decimal("400000")
    assert turnover_ratio(sod, orders) == Decimal("0.6")
    result = PreTradeEngine(settings=settings).evaluate(sod, orders)
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_TURNOVER"}


def test_real_lseg_examples_breach_max_turnover():
    """Real 8/5 SOD + 8/6 POC trades (~24% two-way TO) scaled up to breach YAML 25%."""
    from ki_ops.alpha import (
        apply_trade_time_prices,
        load_infocode_price_map,
        portfolio_at_trade_time_prices,
    )

    sod_path = EXAMPLES / "sod_lseg_20260805.csv"
    trd_path = EXAMPLES / "trade_intents_lseg_20260806.csv"
    px_path = EXAMPLES / "ds2_px_20260804.csv"
    if not sod_path.is_file() or not trd_path.is_file() or not px_path.is_file():
        pytest.skip("LSEG example CSVs not present")

    prices = load_infocode_price_map(px_path, field="close")
    sod = portfolio_at_trade_time_prices(load_sod_positions_csv(sod_path), prices)
    orders = apply_trade_time_prices(_load_orders(trd_path), prices)
    base_to = turnover_ratio(sod, orders)
    assert base_to < Decimal("0.25")

    # Same trade mix as the saved POC file, scaled to ~26% two-way vs 8/5 GMV.
    scale = Decimal("0.26") / base_to
    scaled = [replace(o, quantity=o.quantity * scale) for o in orders]
    assert turnover_ratio(sod, scaled) > Decimal("0.25")

    settings = replace(
        load_risk_settings(POC_CONFIG),
        max_order_size=Decimal("100000000"),
        max_portfolio_value=Decimal("1000000000"),
        max_position_size=Decimal("100000000"),
        max_position_concentration=Decimal("1"),
        min_order_size=Decimal("0"),
    )
    assert settings.max_turnover == Decimal("0.25")  # two-way
    result = PreTradeEngine(settings=settings).evaluate(sod, scaled)
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_TURNOVER"}


def test_real_lseg_examples_zero_trades_have_zero_turnover(tmp_path: Path):
    """Real 8/5 SOD + 8/6 trade names with quantity 0 → one-way turnover 0.

    Applies Datastream2 px so share qty = SOD $ / px (same as the POC perturb path).
    """
    from ki_ops.alpha import (
        apply_trade_time_prices,
        load_infocode_price_map,
        portfolio_at_trade_time_prices,
    )

    sod_path = EXAMPLES / "sod_lseg_20260805.csv"
    trd_path = EXAMPLES / "trade_intents_lseg_20260806.csv"
    px_path = EXAMPLES / "ds2_px_20260804.csv"
    if not sod_path.is_file() or not trd_path.is_file() or not px_path.is_file():
        pytest.skip("LSEG example CSVs not present")

    zero_path = tmp_path / "trade_intents_lseg_20260806_zero.csv"
    with trd_path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    with zero_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "infocode", "quantity"])
        w.writeheader()
        for r in rows:
            w.writerow({"ticker": r["ticker"], "infocode": r["infocode"], "quantity": "0"})

    prices = load_infocode_price_map(px_path, field="close")
    sod = portfolio_at_trade_time_prices(load_sod_positions_csv(sod_path), prices)
    orders = apply_trade_time_prices(_load_orders(zero_path), prices)
    assert len(orders) == len(rows)
    assert all(o.quantity == 0 for o in orders)
    assert turnover_ratio(sod, orders) == Decimal("0")

    result = PreTradeEngine(settings=load_risk_settings(POC_CONFIG)).evaluate(sod, orders)
    assert result.turnover == Decimal("0")
    assert result.allowed is True
    assert not any(v.code == "MAX_TURNOVER" for v in result.violations)
    # Share-based position cap warns on oversized SOD names; does not block.
    assert any(v.code == "MAX_POSITION_SIZE" for v in result.warnings)


def test_turnover_adds_buys_and_sells_does_not_net():
    """Buy $1k + sell $1k on a $10k-GMV book => two-way 0.20, not 0 (net)."""
    portfolio = portfolio_from_holdings([Holding("AAPL", 100, 100)], cash=0)  # GMV 10000
    orders = [
        Order("MSFT", Side.BUY, 10, 100, TS),   # +1000
        Order("AAPL", Side.SELL, 10, 100, TS),  # +1000
    ]
    # (1000+1000)/10000 = 0.20
    assert turnover_ratio(portfolio, orders) == Decimal("0.2")


def test_turnover_uses_position_gmv_with_shorts():
    """Dollar-neutral SOD must use position GMV, not tiny net NAV or GMV+cash."""
    sod = portfolio_from_holdings(
        [
            Holding("AAPL", 100, 100),   # +10000
            Holding("TSLA", -50, 200),  # -10000
        ],
        cash=5000,
    )
    assert sod.total_value == Decimal("5000")        # net NAV = cash
    assert sod.gmv == Decimal("20000")               # positions only (kelaisim base)
    assert sod.gmv_plus_cash == Decimal("25000")
    orders = [
        Order("AAPL", Side.SELL, 10, 100, TS),  # 1000
        Order("TSLA", Side.BUY, 5, 200, TS),    # 1000 cover
    ]
    # two-way: 2000/20000 = 0.10 — cash neither dilutes nor replaces the base
    assert turnover_ratio(sod, orders) == Decimal("0.1")


def test_max_order_size_is_abs_qty_times_trade_px():
    """Order size = abs(trade_intent qty) × trade-time px (when checks enabled)."""
    from ki_ops.alpha import apply_trade_time_prices

    # 10 shares × $80 = $800 > $500 cap
    result = PreTradeEngine(
        settings=loose(enforce_order_size_limits=True, max_order_size=Decimal("500"))
    ).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("AAPL", Side.BUY, 10, 80, TS)],
    )
    assert result.allowed is False
    assert any(v.code == "MAX_ORDER_SIZE" for v in result.violations)
    assert {v.code for v in result.violations} == {"MAX_ORDER_SIZE"}

    # Share intent 10 @ $80; size $800. Legacy dollar rows need intents_are_dollars.
    priced = apply_trade_time_prices(
        [Order("1001", Side.SELL, 10, 1, TS)],
        {"1001": Decimal("80")},
    )
    assert priced[0].quantity == Decimal("10")
    assert priced[0].limit_price == Decimal("80")
    assert priced[0].notional == Decimal("800")
    dollar_legacy = apply_trade_time_prices(
        [Order("1001", Side.SELL, 800, 1, TS)],
        {"1001": Decimal("80")},
        intents_are_dollars=True,
    )
    assert dollar_legacy[0].quantity == Decimal("10")
    assert dollar_legacy[0].notional == Decimal("800")


def test_order_size_bounds():
    engine = PreTradeEngine(
        settings=loose(
            enforce_order_size_limits=True,
            min_order_size=Decimal("100"),
            max_order_size=Decimal("5000"),
        )
    )
    portfolio = portfolio_from_holdings([], cash=50000)
    too_small = engine.evaluate(portfolio, [Order("X", Side.BUY, 1, 10, TS)])  # $10
    too_big = engine.evaluate(portfolio, [Order("Y", Side.BUY, 200, 30, TS)])  # $6000
    assert too_small.allowed is False
    assert {v.code for v in too_small.violations} == {"MIN_ORDER_SIZE"}
    assert too_big.allowed is False
    assert {v.code for v in too_big.violations} == {"MAX_ORDER_SIZE"}


def test_daily_loss_shutdown():
    result = PreTradeEngine(settings=loose(max_daily_loss=Decimal("2000"))).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("Z", Side.BUY, 2, 100, TS)],
        realized_daily_pnl=Decimal("-2500"),
    )
    assert result.allowed is False
    assert {v.code for v in result.violations} == {"MAX_DAILY_LOSS"}
