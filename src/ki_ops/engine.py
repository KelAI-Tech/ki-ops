"""Pre-trade engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

from ki_ops.checks import CheckViolation
from ki_ops.checks.rules import drop_untradable_orders, run_all_checks, split_findings
from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.intents import TargetIntent, TradeIntentBatch, annotate_display_labels, build_trade_intent_batch
from ki_ops.listing import ListingStatus
from ki_ops.models import Order, Portfolio
from ki_ops.portfolio import TURNOVER_CONVENTION, project_orders, turnover_ratio

_TWOPLACES = Decimal("0.01")


def format_decimal(value: Decimal | float | int | str, *, places: Decimal = _TWOPLACES) -> str:
    """Format a number for CLI/JSON stdout (default 2 decimal places).

    Non-finite values (e.g. ``Decimal("Infinity")`` from a zero-GMV turnover
    base) are returned as-is instead of raising ``InvalidOperation``.
    """
    d = Decimal(str(value))
    if not d.is_finite():
        return str(d)
    return str(d.quantize(places, rounding=ROUND_HALF_UP))


def passed_status(allowed: bool, warnings: Sequence | None = None) -> bool | str:
    """Stdout ``passed`` value: True, False, or ``\"with warnings\"``."""
    if not allowed:
        return False
    if warnings:
        return "with warnings"
    return True


@dataclass(frozen=True)
class PreTradeResult:
    allowed: bool
    violations: tuple[CheckViolation, ...] = ()
    warnings: tuple[CheckViolation, ...] = ()
    turnover: Decimal = Decimal("0")
    projected_portfolio_value: Decimal = Decimal("0")
    projected_net_exposure: Decimal = Decimal("0")
    trade_intents: tuple[Order, ...] = ()

    def to_dict(self) -> dict:
        return {
            "passed": passed_status(self.allowed, self.warnings),
            "violations": [v.to_dict() for v in self.violations],
            "warnings": [v.to_dict() for v in self.warnings],
            "turnover": format_decimal(self.turnover),
            "turnover_convention": TURNOVER_CONVENTION,
            "projected_portfolio_value": format_decimal(self.projected_portfolio_value),
            "projected_net_exposure": format_decimal(self.projected_net_exposure),
            "trade_intents": [o.to_dict() for o in self.trade_intents],
        }


@dataclass
class PreTradeEngine:
    settings: RiskManagementSettings = field(default_factory=load_risk_settings)

    @classmethod
    def from_config_path(cls, path: str) -> PreTradeEngine:
        return cls(settings=load_risk_settings(path))

    def evaluate(
        self,
        portfolio: Portfolio,
        orders: Sequence[Order],
        *,
        realized_daily_pnl: Decimal | float | int | str = 0,
        volatilities: Mapping[str, Decimal | float | int | str] | None = None,
        listing: Mapping[str, ListingStatus] | None = None,
        as_of: date | None = None,
    ) -> PreTradeResult:
        order_list = annotate_display_labels(portfolio, list(orders))
        vols = {s.upper(): Decimal(str(v)) for s, v in (volatilities or {}).items()}
        pnl = Decimal(str(realized_daily_pnl))
        listing_findings: tuple[CheckViolation, ...] = ()
        if listing is not None:
            trade_date = as_of or date.today()
            order_list, extra = drop_untradable_orders(order_list, listing, as_of=trade_date)
            listing_findings = tuple(extra)
        projected = project_orders(portfolio, order_list)

        if not self.settings.enabled:
            return PreTradeResult(
                allowed=True,
                warnings=listing_findings,
                turnover=turnover_ratio(portfolio, order_list),
                projected_portfolio_value=projected.gmv_plus_cash,
                projected_net_exposure=projected.net_exposure,
                trade_intents=tuple(order_list),
            )

        blocks, warnings = split_findings(
            run_all_checks(
                portfolio,
                order_list,
                self.settings,
                pnl=pnl,
                vols=vols,
            )
        )
        return PreTradeResult(
            allowed=not blocks,
            violations=blocks,
            warnings=listing_findings + warnings,
            turnover=turnover_ratio(portfolio, order_list),
            projected_portfolio_value=projected.gmv_plus_cash,
            projected_net_exposure=projected.net_exposure,
            trade_intents=tuple(order_list),
        )

    def evaluate_from_targets(
        self,
        sod: Portfolio,
        targets: Sequence[TargetIntent],
        *,
        realized_daily_pnl: Decimal | float | int | str = 0,
        volatilities: Mapping[str, Decimal | float | int | str] | None = None,
        timestamp: datetime | None = None,
        flatten_missing_targets: bool = True,
        listing: Mapping[str, ListingStatus] | None = None,
        as_of: date | None = None,
    ) -> PreTradeResult:
        batch = build_trade_intent_batch(
            sod, targets, timestamp=timestamp, flatten_missing_targets=flatten_missing_targets
        )
        return self.evaluate(
            sod,
            batch.trade_intents,
            realized_daily_pnl=realized_daily_pnl,
            volatilities=volatilities,
            listing=listing,
            as_of=as_of,
        )

    def build_trade_intents(
        self,
        sod: Portfolio,
        targets: Sequence[TargetIntent],
        *,
        timestamp: datetime | None = None,
        flatten_missing_targets: bool = True,
    ) -> TradeIntentBatch:
        return build_trade_intent_batch(
            sod, targets, timestamp=timestamp, flatten_missing_targets=flatten_missing_targets
        )
