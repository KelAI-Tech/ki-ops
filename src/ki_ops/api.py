"""Optional FastAPI app."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ki_ops.config import RiskManagementSettings, load_risk_settings
from ki_ops.engine import PreTradeEngine
from ki_ops.intents import TargetIntent
from ki_ops.models import Holding, Order, Portfolio, Side


def create_app(settings: RiskManagementSettings | None = None):
    try:
        from fastapi import FastAPI
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Install API extras: pip install 'ki-ops[api]'") from exc

    engine = PreTradeEngine(settings=settings or load_risk_settings())
    app = FastAPI(title="KI Ops", version="0.1.0")

    class HoldingIn(BaseModel):
        symbol: str
        quantity: Decimal
        market_price: Decimal
        cost_basis: Decimal | None = None

    class TargetIn(BaseModel):
        symbol: str
        quantity: Decimal
        market_price: Decimal | None = None

    class OrderIn(BaseModel):
        symbol: str
        side: Side
        quantity: Decimal
        limit_price: Decimal
        order_id: str | None = None

    class EvaluateRequest(BaseModel):
        holdings: list[HoldingIn] = Field(default_factory=list)
        cash: Decimal = Decimal("0")
        orders: list[OrderIn]
        realized_daily_pnl: Decimal = Decimal("0")
        volatilities: dict[str, Decimal] = Field(default_factory=dict)

    class RebalanceRequest(BaseModel):
        sod_positions: list[HoldingIn] = Field(default_factory=list)
        cash: Decimal = Decimal("0")
        targets: list[TargetIn]
        realized_daily_pnl: Decimal = Decimal("0")
        volatilities: dict[str, Decimal] = Field(default_factory=dict)
        flatten_missing_targets: bool = True

    def _portfolio(rows: list[HoldingIn], cash: Decimal) -> Portfolio:
        return Portfolio(
            holdings={
                h.symbol: Holding(h.symbol, h.quantity, h.market_price, h.cost_basis) for h in rows
            },
            cash=cash,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/pretrade/evaluate")
    def evaluate(body: EvaluateRequest) -> dict[str, Any]:
        orders = [Order(o.symbol, o.side, o.quantity, o.limit_price, order_id=o.order_id) for o in body.orders]
        return engine.evaluate(
            _portfolio(body.holdings, body.cash),
            orders,
            realized_daily_pnl=body.realized_daily_pnl,
            volatilities=body.volatilities,
        ).to_dict()

    @app.post("/v1/pretrade/rebalance")
    def rebalance(body: RebalanceRequest) -> dict[str, Any]:
        targets = [TargetIntent(t.symbol, t.quantity, t.market_price) for t in body.targets]
        return engine.evaluate_from_targets(
            _portfolio(body.sod_positions, body.cash),
            targets,
            realized_daily_pnl=body.realized_daily_pnl,
            volatilities=body.volatilities,
            flatten_missing_targets=body.flatten_missing_targets,
        ).to_dict()

    return app
