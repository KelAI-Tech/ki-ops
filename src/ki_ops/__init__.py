"""KI Ops library."""

from ki_ops.alpha import (
    load_alpha_dollar_panel,
    portfolio_from_dollar_row,
    run_alpha_panel_checks,
    summarize_alpha_days,
    targets_from_dollar_row,
)
from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.ems_intents import (
    approximate_ems_prices_from_alpha,
    evaluate_ems_against_alpha_sod,
    load_ems_trade_intents_csv,
)
from ki_ops.engine import PreTradeEngine, PreTradeResult
from ki_ops.intents import (
    TargetIntent,
    TradeIntentBatch,
    build_trade_intent_batch,
    derive_trade_intents,
    load_sod_positions_csv,
    load_symbol_volatilities,
    load_target_intents_csv,
)
from ki_ops.models import Holding, Order, Portfolio, Trade
from ki_ops.portfolio import apply_trades, portfolio_from_holdings
from ki_ops.risk import (
    BookExposures,
    RiskSnapshot,
    SecurityRecord,
    SecurityUniverse,
    build_risk_snapshot,
    load_security_master_csv,
    snapshot_book,
    universe_from_records,
)

__all__ = [
    "BookExposures",
    "Holding",
    "Order",
    "Portfolio",
    "PreTradeEngine",
    "PreTradeResult",
    "RiskManagementSettings",
    "RiskSnapshot",
    "SecurityRecord",
    "SecurityUniverse",
    "TargetIntent",
    "Trade",
    "TradeIntentBatch",
    "apply_trades",
    "approximate_ems_prices_from_alpha",
    "build_risk_snapshot",
    "build_trade_intent_batch",
    "derive_trade_intents",
    "evaluate_ems_against_alpha_sod",
    "load_alpha_dollar_panel",
    "load_ems_trade_intents_csv",
    "load_risk_settings",
    "load_security_master_csv",
    "load_sod_positions_csv",
    "load_symbol_volatilities",
    "load_target_intents_csv",
    "portfolio_from_dollar_row",
    "portfolio_from_holdings",
    "run_alpha_panel_checks",
    "snapshot_book",
    "summarize_alpha_days",
    "targets_from_dollar_row",
    "universe_from_records",
]

__version__ = "0.1.0"
