"""Risk settings from YAML."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

BOOLS = {
    "enabled",
    "use_stop_losses",
    "trailing_stop",
    "enforce_market_hours",
    "enforce_order_size_limits",
    "pre_market_trading",
    "after_hours_trading",
    "allow_shorts",
}
INTS = {"volatility_lookback", "max_orders_per_minute"}
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RiskManagementSettings:
    enabled: bool = True
    max_position_size: Decimal = Decimal("4000")  # max abs share qty per name
    max_portfolio_value: Decimal = Decimal("200000")  # GMV cap (long + |short| + cash)
    max_daily_loss: Decimal = Decimal("2000")
    max_position_concentration: Decimal = Decimal("0.2")
    max_position_volatility: Decimal = Decimal("0.3")
    volatility_lookback: int = 30
    use_stop_losses: bool = False
    default_stop_loss: Decimal = Decimal("0.05")
    trailing_stop: bool = True
    trailing_stop_distance: Decimal = Decimal("0.02")
    min_order_size: Decimal = Decimal("100")
    max_order_size: Decimal = Decimal("5000")
    enforce_order_size_limits: bool = False
    max_orders_per_minute: int = 10
    enforce_market_hours: bool = False
    pre_market_trading: bool = False
    after_hours_trading: bool = False
    max_turnover: Decimal = Decimal("0.25")
    allow_shorts: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RiskManagementSettings:
        raw = data.get("risk_management", data)
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            val = raw[f.name]
            if f.name in BOOLS:
                kwargs[f.name] = bool(val)
            elif f.name in INTS:
                kwargs[f.name] = int(val)
            else:
                kwargs[f.name] = Decimal(str(val))
        return cls(**{**{f.name: getattr(defaults, f.name) for f in fields(cls)}, **kwargs})


def resolve_config_path(raw: str, *, config_file: Path) -> Path:
    """Resolve a YAML path: absolute as-is; else vs config dir, then repo root."""
    p = Path(raw)
    if p.is_absolute():
        return p
    cfg_dir = config_file.resolve().parent
    for candidate in (cfg_dir / p, cfg_dir.parent / p, REPO_ROOT / p):
        if candidate.is_file():
            return candidate.resolve()
    return (cfg_dir.parent / p).resolve()


def load_risk_settings(path: str | Path | None = None) -> RiskManagementSettings:
    paths = [Path(path)] if path else [
        Path.cwd() / "config" / "risk_management.yaml",
        Path.cwd() / "risk_management.yaml",
        REPO_ROOT / "config" / "risk_management.yaml",
    ]
    for p in paths:
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return RiskManagementSettings.from_mapping(data)
    return RiskManagementSettings()
