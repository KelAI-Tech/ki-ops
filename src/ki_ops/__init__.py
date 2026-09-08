"""KI Ops library — core pre-trade / LSEG POC surface."""

from ki_ops.alpha import (
    load_alpha_dollar_panel,
    portfolio_from_dollar_row,
    run_alpha_panel_checks,
    summarize_alpha_days,
    targets_from_dollar_row,
)
from ki_ops.config import RiskManagementSettings, load_risk_settings
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

__all__ = [
    "Holding",
    "Order",
    "Portfolio",
    "PreTradeEngine",
    "PreTradeResult",
    "RiskManagementSettings",
    "TargetIntent",
    "Trade",
    "TradeIntentBatch",
    "apply_trades",
    "build_trade_intent_batch",
    "derive_trade_intents",
    "load_alpha_dollar_panel",
    "load_risk_settings",
    "load_sod_positions_csv",
    "load_symbol_volatilities",
    "load_target_intents_csv",
    "portfolio_from_dollar_row",
    "portfolio_from_holdings",
    "run_alpha_panel_checks",
    "summarize_alpha_days",
    "targets_from_dollar_row",
]

def _distribution_version() -> str:
    """Single-source version: the installed distribution metadata.

    The version is derived from git tags by setuptools-scm at build time
    (see pyproject.toml). When running straight from a source tree with no
    installed distribution, there is no version to report.
    """
    try:
        from importlib.metadata import version

        return version("ki-ops")
    except Exception:  # pragma: no cover - uninstalled source tree
        return "0+source"


__version__ = _distribution_version()

try:
    # Generated into the wheel at build time by setup.py; absent in source
    # checkouts and sdists built without git metadata.
    from ki_ops._build_info import GIT_SHA as __git_sha__  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - source tree / no build info
    __git_sha__ = "unknown"
