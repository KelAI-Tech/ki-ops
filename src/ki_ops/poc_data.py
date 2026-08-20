"""POC input manifest: SOD, trade intents, trade-time prices, ticker map."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from ki_ops.config import REPO_ROOT, resolve_config_path

DEFAULT_POC_DATA = REPO_ROOT / "config" / "poc_pos_and_px.yaml"


@dataclass(frozen=True)
class PocDataPaths:
    sod: Path
    trades: Path
    prices: Path
    ticker_map: Path
    config_file: Path


def load_poc_data_paths(path: str | Path | None = None) -> PocDataPaths:
    """Load SOD / trades / prices / ticker_map paths from a POC manifest YAML."""
    if path:
        candidates = [Path(path)]
    else:
        candidates = [Path.cwd() / "config" / DEFAULT_POC_DATA.name, DEFAULT_POC_DATA]
    for cfg in candidates:
        if not cfg.is_file():
            continue
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        raw = data.get("poc_data", data)
        return _paths_from_mapping(raw, config_file=cfg)
    raise FileNotFoundError(
        "POC data manifest not found; expected config/poc_pos_and_px.yaml "
        "(sod, trades, prices, ticker_map keys)"
    )


def _paths_from_mapping(raw: Mapping[str, Any], *, config_file: Path) -> PocDataPaths:
    missing = [k for k in ("sod", "trades", "prices", "ticker_map") if not raw.get(k)]
    if missing:
        raise ValueError(f"POC manifest {config_file} missing keys: {', '.join(missing)}")
    return PocDataPaths(
        sod=resolve_config_path(str(raw["sod"]), config_file=config_file),
        trades=resolve_config_path(str(raw["trades"]), config_file=config_file),
        prices=resolve_config_path(str(raw["prices"]), config_file=config_file),
        ticker_map=resolve_config_path(str(raw["ticker_map"]), config_file=config_file),
        config_file=config_file.resolve(),
    )
