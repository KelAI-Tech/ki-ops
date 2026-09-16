"""CLI smoke tests for kotl (offline)."""

from __future__ import annotations

import contextlib
import io
from datetime import date
from pathlib import Path

from ki_ops.kotl.cli import (
    EXIT_BOOK_AUDIT_DRIFT,
    EXIT_RECON_DIVERGENCE,
    EXIT_SUBMIT_REFUSED,
    run_kotl,
)

ROOT = Path(__file__).resolve().parents[2]


class Args:
    """Argparse-namespace stand-in; unset options fall back via getattr defaults."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_kotl_cli_submit_refresh_status(tmp_path):
    data_dir = tmp_path / "kotl"

    class Args:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    rc = run_kotl(
        Args(
            kotl_command="submit-rebalance",
            sod=ROOT / "examples" / "sod_positions.csv",
            targets=ROOT / "examples" / "target_intents.csv",
            trade_date=date(2026, 8, 6),
            data_dir=data_dir,
        )
    )
    assert rc == 0
    assert (data_dir / "submits.csv").exists()

    rc = run_kotl(
        Args(
            kotl_command="refresh",
            trade_date=date(2026, 8, 6),
            fixture=ROOT / "examples" / "kotl" / "refresh_partial.json",
            data_dir=data_dir,
        )
    )
    assert rc == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="status",
                trade_date=date(2026, 8, 6),
                data_dir=data_dir,
                json=False,
            )
        )
    assert rc == 0
    assert "AAPL.US" in buf.getvalue()
    assert "flat=False" in buf.getvalue()


def test_kotl_cli_submit_kelai(tmp_path):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares = tmp_path / "20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")
    sod = tmp_path / "sod.csv"
    sod.write_text("symbol,quantity,market_price\nAAPL,20,190\n")
    data_dir = tmp_path / "kotl"

    class Args:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="submit-kelai",
                trade_date=date(2026, 8, 6),
                shares=str(shares),
                ds2=str(h5),
                sod=sod,
                assume_flat_sod=False,
                data_dir=data_dir,
                cache_dir=tmp_path / "cache",
            )
        )
    assert rc == 0
    assert (data_dir / "working_orders.csv").exists()
    assert '"order_count": 2' in buf.getvalue()


def _kelai_args(tmp_path, shares, h5, **kwargs):
    base = dict(
        kotl_command="submit-kelai",
        trade_date=date(2026, 8, 6),
        shares=str(shares),
        ds2=str(h5),
        sod=None,
        assume_flat_sod=False,
        data_dir=tmp_path / "kotl",
        cache_dir=tmp_path / "cache",
    )
    base.update(kwargs)
    return Args(**base)


def test_kotl_cli_submit_kelai_dry_run_and_safety_exit_codes(tmp_path):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares_dir = tmp_path / "shares"
    shares_dir.mkdir()
    shares = shares_dir / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_kelai_args(tmp_path, shares, h5, sod_source="flat", dry_run=True))
    assert rc == 0
    out = buf.getvalue()
    assert '"dry_run": true' in out
    assert "trade file:" in out
    assert not (tmp_path / "kotl" / "submits.csv").exists()

    # caps refusal → exit 5
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_kelai_args(tmp_path, shares, h5, sod_source="flat", max_orders=1))
    assert rc == EXIT_SUBMIT_REFUSED
    assert "SUBMIT REFUSED" in buf.getvalue()


def test_kotl_cli_submit_kelai_no_route(tmp_path):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares_dir = tmp_path / "shares"
    shares_dir.mkdir()
    shares = shares_dir / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            _kelai_args(tmp_path, shares, h5, sod_source="flat", no_route=True)
        )
    assert rc == 0
    out = buf.getvalue()
    assert "NO-ROUTE MODE" in out
    assert '"no_route": true' in out
    assert (tmp_path / "kotl" / "working_orders.csv").exists()
    # Desk requirement: Account Type = Swap on every order, no-route included.
    from ki_ops.kotl.store import KotlStore

    (submit,) = KotlStore(tmp_path / "kotl").load_submits()
    assert submit.payload
    assert all(p["accountType"] == "SWAP" for p in submit.payload)


def test_kotl_cli_submit_kelai_account_type_flag(tmp_path):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares_dir = tmp_path / "shares"
    shares_dir.mkdir()
    shares = shares_dir / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            _kelai_args(
                tmp_path, shares, h5, sod_source="flat", account_type="otc"
            )
        )
    assert rc == 0
    from ki_ops.kotl.store import KotlStore

    (submit,) = KotlStore(tmp_path / "kotl").load_submits()
    assert submit.payload
    assert all(p["accountType"] == "OTC" for p in submit.payload)


def test_build_submit_store_env_aware_defaults(tmp_path, monkeypatch, capsys):
    """submit-kelai/resend ledger: FAKE → csv; live env → KI_OPS_ENV preset
    MySQL by default; explicit --store always wins (csv on live warns)."""
    import ki_ops.kotl.mysql_store as mysql_store
    from ki_ops.kotl.cli import _build_submit_store
    from ki_ops.kotl.store import KotlStore

    calls = {}

    class FakeMysql:
        @classmethod
        def from_env_or_secret(cls, *, db_secret=None, db_schema=None):
            calls["args"] = (db_secret, db_schema)
            return "MYSQL-STORE"

    monkeypatch.setattr(mysql_store, "MysqlKotlStore", FakeMysql)
    monkeypatch.setenv("KI_OPS_ENV", "canary")

    # FAKE (offline) keeps the csv/data-dir default
    assert isinstance(_build_submit_store(Args(data_dir=tmp_path)), KotlStore)

    # live env, no --store → the env preset's MySQL ledger
    assert _build_submit_store(Args(data_dir=tmp_path, flex_env="UAT")) == "MYSQL-STORE"
    assert calls["args"] == ("kelai/kotl/db-canary", "kotl")
    assert "live-env default, KI_OPS_ENV=canary" in capsys.readouterr().out

    monkeypatch.setenv("KI_OPS_ENV", "prod")
    _build_submit_store(Args(data_dir=tmp_path, flex_env="PROD"))
    assert calls["args"] == ("kelai/kotl/db-prod", "kotl")

    # explicit flags beat the preset
    _build_submit_store(
        Args(data_dir=tmp_path, flex_env="UAT", store="mysql", db_secret="x/y", db_schema="z")
    )
    assert calls["args"] == ("x/y", "z")

    # explicit csv on a live env is honored but loud
    capsys.readouterr()
    assert isinstance(
        _build_submit_store(Args(data_dir=tmp_path, flex_env="UAT", store="csv")), KotlStore
    )
    assert "WARNING: --store csv on a live env" in capsys.readouterr().out


def test_kotl_cli_resend_offline(tmp_path):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares_dir = tmp_path / "shares"
    shares_dir.mkdir()
    shares = shares_dir / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")

    rc = run_kotl(_kelai_args(tmp_path, shares, h5, sod_source="flat"))
    assert rc == 0

    def _resend_args(**kwargs):
        base = dict(
            kotl_command="resend",
            sod_source="flat",
            ticker=None,
            retry_unresolved=None,
        )
        base.update(kwargs)
        return _kelai_args(tmp_path, shares, h5, **base)

    # no scope → usage error
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_resend_args())
    assert rc == 2
    assert "resend needs a scope" in buf.getvalue()

    # scoped to one ticker (FAKE env: no target mode; the filter still applies)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_resend_args(ticker=["AAPL"]))
    assert rc == 0
    out = buf.getvalue()
    assert "RESEND" in out
    assert "resend scope: 1 of 2 order(s) kept (AAPL)" in out
    assert '"order_count": 1' in out
    assert '"resend": true' in out

    # unknown ticker → refusal, exit 5
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_resend_args(ticker=["ZZZZ"]))
    assert rc == EXIT_SUBMIT_REFUSED
    assert "no trade intent" in buf.getvalue()


def test_kotl_cli_submit_kelai_flex_sod_recon_exit_code(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("h5py")
    from tests.kotl.fake_flex_sdk import FakeFlexBackend, install_fake_sdk, make_position
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares_dir = tmp_path / "shares"
    shares_dir.mkdir()
    shares = shares_dir / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\n")
    (shares_dir / "Portfolio_20260805.csv").write_text("AAPL,20,VWAP\n")

    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "127.0.0.1:50051")
    monkeypatch.setenv("KOTL_FLEX_TOKEN", "tok")
    backend = FakeFlexBackend()
    backend.positions = [make_position("AAPL.US", 25.0)]  # prior target said 20
    install_fake_sdk(monkeypatch, backend)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_kelai_args(tmp_path, shares, h5, sod_source="flex"))
    assert rc == EXIT_RECON_DIVERGENCE
    out = buf.getvalue()
    assert "RECON BLOCKED" in out
    assert "SOD reconciliation" in out

    # thresholds raised deliberately → proceeds (FAKE adapter, flex SOD)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            _kelai_args(
                tmp_path, shares, h5,
                sod_source="flex", recon_max_shares=10, recon_max_names=5,
            )
        )
    assert rc == 0
    assert '"order_count": 1' in buf.getvalue()


def test_kotl_cli_refresh_and_eod_live_source(tmp_path, monkeypatch):
    from tests.kotl.fake_flex_sdk import FakeFlexBackend, install_fake_sdk, make_order_info

    data_dir = tmp_path / "kotl"
    rc = run_kotl(
        Args(
            kotl_command="submit-rebalance",
            sod=ROOT / "examples" / "sod_positions.csv",
            targets=ROOT / "examples" / "target_intents.csv",
            trade_date=date(2026, 8, 6),
            data_dir=data_dir,
        )
    )
    assert rc == 0

    from ki_ops.kotl.store import KotlStore

    stored = KotlStore(data_dir).load_working_orders(trade_date=date(2026, 8, 6))
    aapl = next(o for o in stored if o.symbol == "AAPL.US")

    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "127.0.0.1:50051")
    monkeypatch.setenv("KOTL_FLEX_TOKEN", "tok")
    backend = FakeFlexBackend()
    backend.order_infos = [
        make_order_info(
            aapl.flex_order_id,
            "AAPL.US",
            side=1,
            quantity=18,
            filled_quantity=18,
            status=5,
            weighted_avg_price=191.0,
            trade_date="08/06/2026",
        )
    ]
    install_fake_sdk(monkeypatch, backend)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="refresh",
                trade_date=date(2026, 8, 6),
                fixture=None,
                source="live",
                flex_env="UAT",
                data_dir=data_dir,
            )
        )
    assert rc == 0
    assert '"updated_count": 1' in buf.getvalue()
    assert '"fixture": "live"' in buf.getvalue()

    refreshed = KotlStore(data_dir).get_working_order(aapl.flex_order_id)
    assert refreshed.status.value == "done"

    # eod over the live source (other orders still open → not flat, exit 3)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="eod",
                trade_date=date(2026, 8, 6),
                fixture=None,
                source="live",
                flex_env="UAT",
                data_dir=data_dir,
                tolerance="0",
                json=True,
                notify=False,
                eod_dir=None,
            )
        )
    assert rc == 3
    assert '"fixture": "live"' in buf.getvalue()
    assert (data_dir / "eod" / "2026-08-06" / "report.json").exists()


def test_kotl_cli_snapshot_book_exit_codes(tmp_path, monkeypatch):
    from decimal import Decimal

    from ki_ops.kotl.store import KotlStore
    from tests.kotl.fake_flex_sdk import FakeFlexBackend, install_fake_sdk, make_position

    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "127.0.0.1:50051")
    monkeypatch.setenv("KOTL_FLEX_TOKEN", "tok")
    backend = FakeFlexBackend()
    backend.positions = [make_position("WM.US", 5.0)]
    install_fake_sdk(monkeypatch, backend)

    def _args(**kwargs):
        base = dict(
            kotl_command="snapshot-book",
            flex_env="UAT",
            as_of=None,
            audit=True,
            dry_run=False,
            data_dir=tmp_path / "kotl",
        )
        base.update(kwargs)
        return Args(**base)

    # bootstrap night: records, audit skipped, exit 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_args(as_of=date(2026, 9, 9)))
    assert rc == 0
    out = buf.getvalue()
    assert '"recorded": true' in out
    assert "bootstrap night" in out

    # next night the book moved with no ledger fills → drift, exit 8
    backend.positions = [make_position("WM.US", 6.0)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(_args(as_of=date(2026, 9, 10)))
    assert rc == EXIT_BOOK_AUDIT_DRIFT
    out = buf.getvalue()
    assert "DRIFT" in out
    # the snapshot is still recorded — it is the factual book
    assert KotlStore(tmp_path / "kotl").load_book_snapshot(date(2026, 9, 10), "UAT") == {
        "WM.US": Decimal("6")
    }
