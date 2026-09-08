"""MysqlKotlStore: credential resolution + CSV/MySQL round-trip parity.

The parity tests start a throwaway ``mysql:8`` docker container and skip
cleanly when docker (or mysql-connector-python) is unavailable.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.models import Submit, WorkingOrder
from ki_ops.kotl.mysql_store import MysqlKotlStore, resolve_db_params
from ki_ops.kotl.store import KotlStore

# ---------------------------------------------------------------------------
# credential resolution (no docker needed)
# ---------------------------------------------------------------------------


def test_resolve_db_params_env_priority(monkeypatch):
    monkeypatch.setenv("KOTL_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("KOTL_DB_PORT", "3307")
    monkeypatch.setenv("KOTL_DB_USER", "root")
    monkeypatch.setenv("KOTL_DB_PASSWORD", "pw")
    monkeypatch.setenv("KOTL_DB_SCHEMA", "kelai_dev")
    params = resolve_db_params(db_secret="ignored/when-env-set")
    assert params == {
        "host": "127.0.0.1",
        "port": 3307,
        "user": "root",
        "password": "pw",
        "schema": "kelai_dev",
    }
    # explicit --db-schema wins over env
    assert resolve_db_params(db_schema="kelai_canary")["schema"] == "kelai_canary"


def test_resolve_db_params_secret_fields(monkeypatch):
    for var in ("KOTL_DB_HOST", "KOTL_DB_PORT", "KOTL_DB_USER", "KOTL_DB_PASSWORD", "KOTL_DB_SCHEMA"):
        monkeypatch.delenv(var, raising=False)
    import ki_ops.kotl.mysql_store as m

    monkeypatch.setattr(
        m,
        "load_db_secret",
        lambda secret_id, region="us-east-1": {
            "host": "db.internal",
            "username": "kelai",
            "password": "pw",
            "port": "3306",
            "dbname": "kelai",
        },
    )
    params = resolve_db_params(db_secret="dev/kelaidb", db_schema="kelai_canary")
    assert params["host"] == "db.internal"
    assert params["user"] == "kelai"
    assert params["port"] == 3306
    assert params["schema"] == "kelai_canary"  # --db-schema beats secret dbname


def test_cli_build_store_mysql_from_env(monkeypatch, tmp_path):
    from ki_ops.kotl.cli import _build_store

    monkeypatch.setenv("KOTL_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("KOTL_DB_PORT", "3307")
    monkeypatch.setenv("KOTL_DB_USER", "root")
    monkeypatch.setenv("KOTL_DB_PASSWORD", "pw")

    class Args:
        store = "mysql"
        db_secret = None
        db_schema = "kelai_canary"
        data_dir = tmp_path

    store = _build_store(Args())  # lazy: no connection until first use
    assert isinstance(store, MysqlKotlStore)
    assert store.schema == "kelai_canary"
    assert store.port == 3307

    class CsvArgs:
        store = "csv"
        data_dir = tmp_path

    assert isinstance(_build_store(CsvArgs()), KotlStore)


def test_resolve_db_params_missing(monkeypatch):
    for var in ("KOTL_DB_HOST", "KOTL_DB_PORT", "KOTL_DB_USER", "KOTL_DB_PASSWORD", "KOTL_DB_SCHEMA"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="db-secret"):
        resolve_db_params()


# ---------------------------------------------------------------------------
# docker mysql fixture
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mysql_store_params():
    mysql = pytest.importorskip("mysql.connector")
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    probe = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip("docker daemon not reachable")

    port = _free_port()
    name = f"kotl-mysql-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "MYSQL_ROOT_PASSWORD=kotltest",
            "-e", "MYSQL_DATABASE=kotl_test",
            "-p", f"127.0.0.1:{port}:3306",
            "mysql:8",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if run.returncode != 0:
        pytest.skip(f"could not start mysql:8 container: {run.stderr.strip()[:200]}")

    params = {
        "host": "127.0.0.1",
        "port": port,
        "user": "root",
        "password": "kotltest",
        "schema": "kotl_test",
    }
    try:
        deadline = time.time() + 180
        last_err = None
        while time.time() < deadline:
            try:
                conn = mysql.connect(
                    host=params["host"],
                    port=params["port"],
                    user=params["user"],
                    password=params["password"],
                    connection_timeout=3,
                )
                conn.close()
                break
            except Exception as exc:  # noqa: BLE001 — retry until deadline
                last_err = exc
                time.sleep(2)
        else:
            pytest.skip(f"mysql container never became ready: {last_err}")
        yield params
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture()
def mysql_store(mysql_store_params):
    store = MysqlKotlStore(**mysql_store_params)
    store.ensure_schema()
    conn = store.connection()
    cur = conn.cursor()
    for table in (
        "kotl_submits",
        "kotl_working_orders",
        "kotl_eod_snapshots",
        "kotl_submit_claims",
    ):
        cur.execute(f"DELETE FROM {table}")
    conn.commit()
    cur.close()
    yield store
    store.close()


# ---------------------------------------------------------------------------
# parity: same operations, identical round-trips
# ---------------------------------------------------------------------------

TD = date(2026, 9, 4)
TS = datetime(2026, 9, 4, 14, 30, 0, 123456, tzinfo=timezone.utc)


def _sample_submit() -> Submit:
    return Submit(
        submit_id="sub-1",
        submitted_at=TS,
        env="UAT",
        ok=True,
        flex_order_ids=("FLEX-1", "FLEX-2"),
        payload=(
            {"symbol": "AAPL.US", "quantity": 18.0, "side": "SELL"},
            {"symbol": "MSFT.US", "quantity": 30.0, "side": "BUY"},
        ),
        flex_response={"results": [{"orderId": "FLEX-1", "success": True}]},
    )


def _sample_orders() -> list[WorkingOrder]:
    open_row = WorkingOrder.from_submit_line(
        submit_id="sub-1",
        flex_order_id="FLEX-1",
        trade_date=TD,
        symbol="AAPL.US",
        side="SELL",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        unsigned_sent_qty=18,
        submitted_at=TS,
    )
    partial = WorkingOrder.from_flex_snapshot(
        submit_id="sub-1",
        trade_date=TD,
        flex_order_id="FLEX-2",
        symbol="MSFT.US",
        side="BUY",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        unsigned_sent_qty=30,
        unsigned_filled_qty=10,
        flex_status="PARTIALLY_FILLED",
        avg_fill_px=Decimal("505.25"),
        last_seen_at=TS,
        broker="KEL-GS-EQ-LT",
        algo="VWAP_AMRS",
        order_type="MARKET",
    )
    return [open_row, partial]


def _exercise(store) -> dict:
    store.append_submit(_sample_submit())
    store.upsert_working_orders(_sample_orders())
    # update pass: FLEX-1 fills fully (upsert must replace, not duplicate)
    updated = store.get_working_order("FLEX-1").with_flex_update(
        unsigned_filled_qty=18,
        flex_status="FILLED",
        avg_fill_px=Decimal("191.20"),
        last_seen_at=TS,
    )
    store.upsert_working_orders([updated])
    return {
        "submits": store.load_submits(),
        "all": store.load_working_orders(),
        "by_date": store.load_working_orders(trade_date=TD),
        "by_submit": store.load_working_orders(submit_id="sub-1"),
        "missing_date": store.load_working_orders(trade_date=date(2020, 1, 1)),
        "one": store.get_working_order("FLEX-2"),
        "gone": store.get_working_order("NOPE"),
    }


def test_csv_mysql_store_parity(tmp_path, mysql_store):
    csv_out = _exercise(KotlStore(tmp_path))
    db_out = _exercise(mysql_store)

    assert csv_out["submits"] == db_out["submits"]
    key = lambda rows: sorted(rows, key=lambda o: o.flex_order_id)
    assert key(csv_out["all"]) == key(db_out["all"])
    assert key(csv_out["by_date"]) == key(db_out["by_date"])
    assert key(csv_out["by_submit"]) == key(db_out["by_submit"])
    assert csv_out["missing_date"] == db_out["missing_date"] == []
    assert csv_out["one"] == db_out["one"]
    assert csv_out["gone"] is db_out["gone"] is None

    filled = db_out["all"]
    flex1 = next(o for o in filled if o.flex_order_id == "FLEX-1")
    assert flex1.status is OrderStatus.DONE
    assert flex1.filled_qty == Decimal("-18")
    assert flex1.leaves_qty == Decimal("0")


def test_mysql_eod_snapshots(mysql_store):
    summary = {"command": "kotl-eod", "trade_date": TD.isoformat(), "flat": True}
    mysql_store.append_eod_snapshot(TD, summary)
    rows = mysql_store.load_eod_snapshots(trade_date=TD)
    assert len(rows) == 1
    assert rows[0]["summary"] == summary
    assert rows[0]["trade_date"] == TD.isoformat()


def test_mysql_ensure_schema_idempotent(mysql_store):
    mysql_store.ensure_schema()
    mysql_store.ensure_schema()
    assert mysql_store.load_submits() == []


def test_mysql_claim_is_atomic_once_a_day(mysql_store):
    # First claimer wins; every later claimer sees the winner's submit_id.
    assert mysql_store.claim_submission(TD, "UAT", "sub-first") is None
    assert mysql_store.claim_submission(TD, "UAT", "sub-second") == "sub-first"
    assert mysql_store.claim_submission(TD, "UAT", "sub-third") == "sub-first"
    # Different env or date is an independent claim.
    assert mysql_store.claim_submission(TD, "PROD", "sub-prod") is None
    assert mysql_store.claim_submission(date(2026, 9, 5), "UAT", "sub-next-day") is None


def test_mysql_claim_two_writer_race(mysql_store, mysql_store_params):
    """Two connections claim concurrently — exactly one wins (PK serializes)."""
    other = MysqlKotlStore(**mysql_store_params)
    try:
        results = [
            mysql_store.claim_submission(TD, "UAT", "writer-a"),
            other.claim_submission(TD, "UAT", "writer-b"),
        ]
        assert results.count(None) == 1
        loser = next(r for r in results if r is not None)
        assert loser == "writer-a"  # first insert won; loser sees the winner
    finally:
        other.close()
