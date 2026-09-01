"""Pick a refresh source from fixture path (simple vs kelai export)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ki_ops.kotl.fixture_refresh import FixtureRefreshSource
from ki_ops.kotl.kelai_refresh import KelaiRefreshSource


def load_refresh_source(path: str | Path):
    """Auto-detect fixture format: KOTL ``fills`` JSON vs kelai ``get_orders`` export."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return KelaiRefreshSource(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "fills" in data:
        return FixtureRefreshSource(data)
    return KelaiRefreshSource(path)
