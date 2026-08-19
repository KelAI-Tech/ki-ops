"""Pre-trade engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from ki_ops.checks import CheckViolation
from ki_ops.checks.rules import run_all_checks, split_findings
from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.intents import TargetIntent, TradeIntentBatch, annotate_display_labels, build_trade_intent_batch
from ki_ops.models import Order, Portfolio
from ki_ops.portfolio import project_orders, turnover_ratio


@dataclass(frozen=True)
class PreTradeResult:
    allowed: bool
    violations: tuple[CheckViolation, ...] = ()
    warnings: tuple[CheckViolation, ...] = ()
    turnover: Decimal = Decimal("0")
    projected_portfolio_value: Decimal = Decimal("0")
    trade_intents: tuple[Order, ...] = ()

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": [v.to_dict() for v in self.warnings],
            "turnover": str(self.turnover),
            "projected_portfolio_value": str(self.projected_portfolio_value),
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
    ) -> PreTradeResult:
        order_list = annotate_display_labels(portfolio, list(orders))
        vols = {s.upper(): Decimal(str(v)) for s, v in (volatilities or {}).items()}
        pnl = Decimal(str(realized_daily_pnl))
        projected = project_orders(portfolio, order_list)

        if not self.settings.enabled:
            return PreTradeResult(
                allowed=True,
                turnover=turnover_ratio(portfolio, order_list),
                projected_portfolio_value=projected.gross_exposure,
                trade_intents=tuple(order_list),
            )

        blocks, warnings = split_findings(
            run_all_checks(portfolio, order_list, self.settings, pnl=pnl, vols=vols)
        )
        return PreTradeResult(
            allowed=not blocks,
            violations=blocks,
            warnings=warnings,
            turnover=turnover_ratio(portfolio, order_list),
            projected_portfolio_value=projected.gross_exposure,
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
    ) -> PreTradeResult:
        batch = build_trade_intent_batch(
            sod, targets, timestamp=timestamp, flatten_missing_targets=flatten_missing_targets
        )
        return self.evaluate(
            sod, batch.trade_intents, realized_daily_pnl=realized_daily_pnl, volatilities=volatilities
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
