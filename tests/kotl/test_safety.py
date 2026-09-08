"""Safety rails on the submit path: idempotency, dry-run, caps."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

pytest.importorskip("h5py")

from ki_ops.kotl.fake_flex import FakeFlexAdapter
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import SubmitRefusedError, has_ok_submit, submit_kelai_shares
from tests.kotl.test_kelaidata_source import make_ds2_h5

TD = date(2026, 8, 6)
TS = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env_setup(tmp_path):
    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")
    return {
        "store": KotlStore(tmp_path / "kotl"),
        "shares": shares,
        "h5": make_ds2_h5(tmp_path / "ds2.h5"),
        "cache": tmp_path / "cache",
        "tmp": tmp_path,
    }


def _submit(env_setup, **kwargs):
    kwargs.setdefault("sod_source", "flat")
    kwargs.setdefault("adapter", FakeFlexAdapter())
    return submit_kelai_shares(
        env_setup["store"],
        trade_date=TD,
        shares_file=env_setup["shares"],
        ds2_h5=env_setup["h5"],
        cache_dir=env_setup["cache"],
        submitted_at=TS,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_live_resubmit_refused_without_force(env_setup):
    first = _submit(env_setup, env="UAT")
    assert first.ok
    assert has_ok_submit(env_setup["store"], TD, "UAT") is not None

    with pytest.raises(SubmitRefusedError, match="already exists"):
        _submit(env_setup, env="UAT")

    forced = _submit(env_setup, env="UAT", force=True)
    assert forced.ok
    assert len(env_setup["store"].load_submits()) == 2


def test_fake_env_resubmit_allowed(env_setup):
    _submit(env_setup, env="FAKE")
    again = _submit(env_setup, env="FAKE")  # offline dev loop stays frictionless
    assert again.ok


def test_other_env_or_date_not_blocked(env_setup):
    _submit(env_setup, env="UAT")
    prod = _submit(env_setup, env="PROD")  # different env, same date → allowed
    assert prod.ok


def test_dry_run_never_blocked_by_idempotency(env_setup):
    _submit(env_setup, env="UAT")
    dry = _submit(env_setup, env="UAT", dry_run=True)
    assert dry.flex_response == {"dry_run": True}


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_no_grpc_no_ledger(env_setup, capsys):
    class ExplodingAdapter:
        def create_orders(self, order_list):
            raise AssertionError("dry run must not call the adapter")

    dry = _submit(env_setup, env="UAT", dry_run=True, adapter=ExplodingAdapter())
    assert dry.flex_order_ids == ()
    assert len(dry.payload) == 2
    assert not (env_setup["tmp"] / "kotl" / "submits.csv").exists()
    assert not (env_setup["tmp"] / "kotl" / "working_orders.csv").exists()

    out = capsys.readouterr().out
    assert "AAPL.US" in out  # table printed
    trade_files = list((env_setup["tmp"] / "kotl" / "trades").rglob("*.csv"))
    assert len(trade_files) == 1
    assert trade_files[0].name.endswith("_dryrun.csv")
    with trade_files[0].open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert all(r["dry_run"] == "true" for r in rows)


def test_live_submit_writes_trade_file_with_order_ids(env_setup, capsys):
    submit = _submit(env_setup, env="UAT")
    trade_files = list((env_setup["tmp"] / "kotl" / "trades").rglob("*.csv"))
    assert len(trade_files) == 1
    assert not trade_files[0].name.endswith("_dryrun.csv")
    with trade_files[0].open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["flex_order_id"] for r in rows} == set(submit.flex_order_ids)
    assert all(r["status"] == "submitted" for r in rows)
    assert "trade file:" in capsys.readouterr().out


def test_trade_file_out_override(env_setup, tmp_path):
    dest = tmp_path / "custom" / "my_trades.csv"
    _submit(env_setup, env="FAKE", trade_file_out=str(dest))
    assert dest.exists()


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------


def test_max_orders_cap_refuses(env_setup):
    with pytest.raises(SubmitRefusedError, match="max_orders"):
        _submit(env_setup, env="UAT", max_orders=1)
    assert not (env_setup["tmp"] / "kotl" / "submits.csv").exists()


def test_max_gross_notional_cap_refuses(env_setup):
    # AAPL 50 × 191.5 + MSFT 30 × 505 = 24725 gross
    with pytest.raises(SubmitRefusedError, match="max_gross_notional"):
        _submit(env_setup, env="UAT", max_gross_notional=Decimal("24000"))


def test_caps_pass_when_generous(env_setup):
    submit = _submit(
        env_setup, env="UAT", max_orders=100, max_gross_notional=Decimal("25000")
    )
    assert submit.ok


def test_caps_apply_to_fake_env_too(env_setup):
    with pytest.raises(SubmitRefusedError, match="max_orders"):
        _submit(env_setup, env="FAKE", max_orders=1)


def test_caps_dry_run_reports_instead_of_refusing(env_setup, capsys):
    dry = _submit(env_setup, env="UAT", max_orders=1, dry_run=True)
    assert dry.flex_response == {"dry_run": True}
    assert "would refuse" in capsys.readouterr().out
