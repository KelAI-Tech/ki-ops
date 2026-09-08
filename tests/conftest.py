import pytest


@pytest.fixture(autouse=True)
def _no_network_sedol_source(monkeypatch):
    """Unit tests never reach Snowflake: the default sedol source is 'none'.

    Tests that exercise the snowflake wiring monkeypatch
    ``ki_ops.kotl.security_master.fetch_sedols_by_ticker`` and opt back in
    explicitly with ``sedol_source="snowflake"``.
    """
    monkeypatch.setenv("KOTL_SEDOL_SOURCE", "none")
