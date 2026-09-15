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
    "allow_turnover_override",
}
INTS = {"volatility_lookback", "max_orders_per_minute"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def _as_bool(name: str, val: Any) -> bool:
    """Strict bool coercion — ``bool("false")`` is ``True``, which is a trap."""
    if isinstance(val, bool):
        return val
    if isinstance(val, int) and val in (0, 1):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in {"true", "yes", "on", "1"}:
            return True
        if s in {"false", "no", "off", "0"}:
            return False
    raise ValueError(f"Invalid boolean for risk_management.{name}: {val!r}")


@dataclass(frozen=True)
class RiskManagementSettings:
    enabled: bool = True
    max_position_size: Decimal = Decimal("4000")  # max abs share qty per name
    max_portfolio_value: Decimal = Decimal("200000")  # cap on GMV + cash (deployed capital)
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
    # TWO-WAY turnover: (buy$ + sell$) / position GMV — kelaisim's convention.
    # One-way is half of this, so 0.25 two-way ≈ 12.5% one-way.
    #
    # Statistical band (requires turnover_mean > 0 and turnover_std > 0):
    #   mean < TO ≤ mean + 2σ  → WARN_TURNOVER (no block)
    #   TO > mean + 2σ         → MAX_TURNOVER block, unless allow_turnover_override
    #                            (override downgrades to TURNOVER_OVERRIDE warn)
    # When mean/σ are unset (0), the turnover check is skipped.
    turnover_mean: Decimal = Decimal("0")
    turnover_std: Decimal = Decimal("0")
    allow_turnover_override: bool = False
    # |NMV|/GMV on the projected book. 1.0 = 100% (effectively off).
    max_net_exposure: Decimal = Decimal("1")
    # Max abs(order shares) / ADV. 0 disables. Over-cap is a warning for now.
    max_adv_participation: Decimal = Decimal("0")
    allow_shorts: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RiskManagementSettings:
        raw = data.get("risk_management", data)
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            # A typo'd limit silently running with the default would be worse
            # than refusing to start.
            raise ValueError(
                f"Unknown risk_management keys: {', '.join(unknown)} "
                f"(known: {', '.join(sorted(known))})"
            )
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            val = raw[f.name]
            if f.name in BOOLS:
                kwargs[f.name] = _as_bool(f.name, val)
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
        Path.cwd() / "config" / "risk_management_small_book.yaml",
        REPO_ROOT / "config" / "risk_management_small_book.yaml",
    ]
    for p in paths:
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return RiskManagementSettings.from_mapping(data)
    return RiskManagementSettings()


def with_turnover_override(
    settings: RiskManagementSettings, override: bool
) -> RiskManagementSettings:
    """Return settings with ``allow_turnover_override`` set when *override* is true."""
    if not override:
        return settings
    from dataclasses import replace

    return replace(settings, allow_turnover_override=True)