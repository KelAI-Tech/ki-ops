"""Kelai security master (Snowflake SECURITY_MASTER_DT) SEDOL source."""

from __future__ import annotations

import pytest

from ki_ops.kotl import security_master as sm


class FakeCursor:
    def __init__(self, rows_by_query):
        self.rows_by_query = rows_by_query
        self.executed: list[str] = []
        self._rows: list[tuple] = []
        self.closed = False

    def execute(self, sql):
        self.executed.append(sql)
        self._rows = self.rows_by_query(sql)

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows_by_query):
        self.cursor_obj = FakeCursor(rows_by_query)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# schema selection
# ---------------------------------------------------------------------------


def test_schema_prod_vs_canary(monkeypatch):
    monkeypatch.delenv("KOTL_SECMASTER_SCHEMA", raising=False)
    assert sm.secmaster_schema("PROD") == "LSEG"
    assert sm.secmaster_schema("prod") == "LSEG"
    assert sm.secmaster_schema("UAT") == "LSEG_CANARY"
    assert sm.secmaster_schema("local") == "LSEG_CANARY"


def test_schema_env_override(monkeypatch):
    monkeypatch.setenv("KOTL_SECMASTER_SCHEMA", "LSEG_DEV_DUALCLASS")
    assert sm.secmaster_schema("PROD") == "LSEG_DEV_DUALCLASS"


# ---------------------------------------------------------------------------
# query + fetch
# ---------------------------------------------------------------------------


def test_sedol_query_shape():
    sql = sm._sedol_query("LSEG", [36100, 71571])
    assert "KELAI.LSEG.SECURITY_MASTER_DT" in sql
    assert "IN (36100,71571)" in sql
    assert "SEDOL IS NOT NULL" in sql
    assert "QUALIFY" in sql  # dedupe guard: one row per infocode


def test_fetch_sedols_by_infocode_basic():
    conn = FakeConnection(lambda sql: [(36100, "2198163"), (71571, "2073390 ")])
    out = sm.fetch_sedols_by_infocode(
        ["36100", 71571, "36100"], env="PROD", connection=conn
    )
    assert out == {"36100": "2198163", "71571": "2073390"}
    # injected connections are the caller's to close
    assert conn.closed is False
    assert conn.cursor_obj.closed is True
    (sql,) = conn.cursor_obj.executed
    assert "KELAI.LSEG." in sql and "36100,71571" in sql


def test_fetch_sedols_by_infocode_chunks(monkeypatch):
    monkeypatch.setattr(sm, "QUERY_CHUNK", 2)
    conn = FakeConnection(lambda sql: [])
    sm.fetch_sedols_by_infocode([1, 2, 3, 4, 5], connection=conn)
    assert len(conn.cursor_obj.executed) == 3


def test_fetch_sedols_by_infocode_empty():
    assert sm.fetch_sedols_by_infocode([], connection=object()) == {}


def test_fetch_sedols_query_failure_wrapped():
    class BrokenConnection:
        def cursor(self):
            raise RuntimeError("warehouse suspended")

    with pytest.raises(sm.SecurityMasterError, match="warehouse suspended"):
        sm.fetch_sedols_by_infocode([1], connection=BrokenConnection())


# ---------------------------------------------------------------------------
# ticker join (ds2 snapshot vocabulary)
# ---------------------------------------------------------------------------


def test_fetch_sedols_by_ticker_joins_and_normalizes():
    conn = FakeConnection(lambda sql: [(36100, "2198163"), (71571, "2073390")])
    out = sm.fetch_sedols_by_ticker(
        {"csco": "36100", "BRKB": " 71571 ", "NOPE": "999"},
        env="UAT",
        connection=conn,
    )
    assert out == {"CSCO": "2198163", "BRKB": "2073390"}
    (sql,) = conn.cursor_obj.executed
    assert "KELAI.LSEG_CANARY." in sql
