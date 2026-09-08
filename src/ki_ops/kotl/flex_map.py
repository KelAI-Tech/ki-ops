"""Map ki-ops trade intents to Flex CreateOrders payloads (offline)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ki_ops.intents import derive_trade_intents, load_sod_positions_csv, load_target_intents_csv
from ki_ops.models import Order, Side


@dataclass(frozen=True)
class FlexOrderDefaults:
    """Execution defaults applied to every Flex order (Kelai sample shape)."""

    fund: str = "KELAI"
    position_group: str = "USATop2000_strategy_v1"
    # The Flex API token is issued for JCO; UAT rejects other users with
    # "User JCO is not entitled to trade as <user>" (verified live 2026-09-08).
    user: str = "JCO"
    owner: str = "JCO"
    trader: str = "JCO"
    order_type: str = "MARKET"
    time_in_force: str = "GFD"
    algo: str = "VWAP_AMRS"
    broker: str = "KEL-GS-EQ-LT"
    broker_automation_type: str = "AUTOROUTE"
    manual_fill: bool = False
    trading_currency: str = "USD"
    settlement_currency: str = "USD"


def flex_symbol(symbol: str, *, suffix: str = ".US") -> str:
    """Bare ticker → Flex symbol (e.g. ``AAPL`` → ``AAPL.US``). Already-suffixed left as-is."""
    sym = symbol.strip().upper()
    if "." in sym:
        return sym
    return f"{sym}{suffix}"


def order_to_flex_dict(
    order: Order,
    *,
    defaults: FlexOrderDefaults | None = None,
    symbol_suffix: str = ".US",
    submit_id: str | None = None,
) -> dict:
    """One ki-ops :class:`Order` → dict for ``kelai_sender.send_orders``."""
    cfg = defaults or FlexOrderDefaults()
    notes_parts = []
    if submit_id:
        notes_parts.append(f"submit_id={submit_id}")
    if order.order_id:
        notes_parts.append(f"intent_id={order.order_id}")
    notes = ";".join(notes_parts)

    return {
        "symbol": flex_symbol(order.symbol, suffix=symbol_suffix),
        "quantity": float(order.quantity),
        "side": order.side.value,
        "orderType": cfg.order_type,
        "fund": cfg.fund,
        "positionGroup": cfg.position_group,
        "user": cfg.user,
        "owner": cfg.owner,
        "trader": cfg.trader,
        "manualFill": cfg.manual_fill,
        "brokerAutomationType": cfg.broker_automation_type,
        "timeInForce": cfg.time_in_force,
        "algo": cfg.algo,
        "broker": cfg.broker,
        "tradingCurrency": cfg.trading_currency,
        "settlementCurrency": cfg.settlement_currency,
        "price": float(order.limit_price),
        "notes": notes,
    }


def orders_to_flex_dicts(
    orders: Sequence[Order],
    *,
    defaults: FlexOrderDefaults | None = None,
    symbol_suffix: str = ".US",
    submit_id: str | None = None,
) -> list[dict]:
    return [
        order_to_flex_dict(
            o,
            defaults=defaults,
            symbol_suffix=symbol_suffix,
            submit_id=submit_id,
        )
        for o in orders
    ]


def flex_orders_from_rebalance_csv(
    sod_csv: str | Path,
    targets_csv: str | Path,
    *,
    defaults: FlexOrderDefaults | None = None,
    symbol_suffix: str = ".US",
    submit_id: str | None = None,
    flatten_missing_targets: bool = True,
) -> list[dict]:
    """``target − SOD`` from CSVs → Flex order dicts (no network)."""
    sod = load_sod_positions_csv(sod_csv)
    targets = load_target_intents_csv(targets_csv)
    orders = derive_trade_intents(
        sod,
        targets,
        flatten_missing_targets=flatten_missing_targets,
    )
    return orders_to_flex_dicts(
        orders,
        defaults=defaults,
        symbol_suffix=symbol_suffix,
        submit_id=submit_id,
    )


def with_defaults(defaults: FlexOrderDefaults, **overrides: str | bool) -> FlexOrderDefaults:
    """Return a copy of *defaults* with field overrides (snake_case kwargs)."""
    field_map = {
        "fund": "fund",
        "position_group": "position_group",
        "user": "user",
        "owner": "owner",
        "trader": "trader",
        "order_type": "order_type",
        "time_in_force": "time_in_force",
        "algo": "algo",
        "broker": "broker",
        "broker_automation_type": "broker_automation_type",
        "manual_fill": "manual_fill",
        "trading_currency": "trading_currency",
        "settlement_currency": "settlement_currency",
    }
    updates = {field_map[k]: v for k, v in overrides.items() if k in field_map}
    return replace(defaults, **updates) if updates else defaults
