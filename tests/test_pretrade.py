from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
from ki_ops.trades import load_trades_csv, summarize_trades

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "risk_management.yaml"
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
    assert s.max_position_size == Decimal("10000")
    assert s.max_position_volatility == Decimal("0.3")
    assert s.volatility_lookback == 30
    assert s.use_stop_losses is False
    assert s.enforce_market_hours is False
    assert s.allow_shorts is True


def test_volatility_blocks_when_above_limit():
    result = PreTradeEngine(settings=loose(max_position_volatility=Decimal("0.3"))).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("AAPL", Side.BUY, 5, 100, TS)],
        volatilities={"AAPL": Decimal("0.45")},
    )
    assert result.allowed is False
    assert any(v.code == "MAX_POSITION_VOLATILITY" and v.symbol == "AAPL" for v in result.violations)


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
    assert any(v.code == "INSUFFICIENT_HOLDINGS" for v in result.violations)


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
    assert {v.symbol for v in vol_blocks} >= {"TSLA", "NVDA", "AMD", "BA", "COIN"}


def test_concentration_blocks_overweight_buy():
    result = PreTradeEngine(settings=loose(max_position_concentration=Decimal("0.2"))).evaluate(
        portfolio_from_holdings([Holding("CASHLIKE", 1, 80000)], cash=20000),
        [Order("AAPL", Side.BUY, 400, 50, TS)],
    )
    assert result.allowed is False
    assert any(v.code == "MAX_POSITION_CONCENTRATION" for v in result.violations)


def test_turnover_limit():
    portfolio = portfolio_from_holdings([], cash=10000)
    orders = [Order("MSFT", Side.BUY, 20, 100, TS)]
    # one-way: (2000/2)/10000 = 0.10
    assert turnover_ratio(portfolio, orders) == Decimal("0.1")
    result = PreTradeEngine(settings=loose(max_turnover=Decimal("0.05"))).evaluate(portfolio, orders)
    assert any(v.code == "MAX_TURNOVER" for v in result.violations)


def test_turnover_adds_buys_and_sells_does_not_net():
    """Buy $1k + sell $1k on a $10k book => one-way 0.10, not 0 (net)."""
    portfolio = portfolio_from_holdings([Holding("AAPL", 10, 100)], cash=9000)
    orders = [
        Order("MSFT", Side.BUY, 10, 100, TS),   # +1000
        Order("AAPL", Side.SELL, 10, 100, TS),  # +1000
    ]
    # (2000/2)/10000 = 0.10
    assert turnover_ratio(portfolio, orders) == Decimal("0.1")


def test_turnover_uses_gross_exposure_with_shorts():
    """Dollar-neutral SOD must not use tiny net NAV as the turnover base."""
    sod = portfolio_from_holdings(
        [
            Holding("AAPL", 100, 100),   # +10000
            Holding("TSLA", -50, 200),  # -10000
        ],
        cash=5000,
    )
    assert sod.total_value == Decimal("5000")          # net NAV = cash
    assert sod.gross_exposure == Decimal("25000")      # 10k+10k+5k cash
    orders = [
        Order("AAPL", Side.SELL, 10, 100, TS),  # 1000
        Order("TSLA", Side.BUY, 5, 200, TS),    # 1000 cover
    ]
    # one-way: (2000/2)/25000 = 0.04 — not (2000/2)/5000 = 0.2
    assert turnover_ratio(sod, orders) == Decimal("0.04")


def test_order_size_bounds():
    engine = PreTradeEngine(settings=loose(min_order_size=Decimal("100"), max_order_size=Decimal("5000")))
    portfolio = portfolio_from_holdings([], cash=50000)
    assert engine.evaluate(portfolio, [Order("X", Side.BUY, 1, 10, TS)]).allowed is False
    assert engine.evaluate(portfolio, [Order("Y", Side.BUY, 200, 30, TS)]).allowed is False


def test_daily_loss_shutdown():
    result = PreTradeEngine(settings=loose(max_daily_loss=Decimal("2000"))).evaluate(
        portfolio_from_holdings([], cash=10000),
        [Order("Z", Side.BUY, 2, 100, TS)],
        realized_daily_pnl=Decimal("-2500"),
    )
    assert any(v.code == "MAX_DAILY_LOSS" for v in result.violations)


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
