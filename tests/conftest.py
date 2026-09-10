from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _market_open_clock(monkeypatch):
    """Pin the market-hours clock inside the submit window.

    Live-env submit tests must not depend on when the suite runs (nights,
    weekends, holidays). 2026-08-06 14:00 UTC is a Thursday, 10:00 EDT — the
    same trade date most kotl tests use. Market-hours tests override the
    verdict clock explicitly.
    """
    monkeypatch.setattr(
        "ki_ops.kotl.market_hours._now_utc",
        lambda: datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc),
    )


@pytest.fixture(autouse=True)
def _no_network_sedol_source(monkeypatch):
    """Unit tests never reach Snowflake: the default sedol source is 'none'.

    Tests that exercise the snowflake wiring monkeypatch
    ``ki_ops.kotl.security_master.fetch_sedols_by_ticker`` and opt back in
    explicitly with ``sedol_source="snowflake"``.
    """
    monkeypatch.setenv("KOTL_SEDOL_SOURCE", "none")
