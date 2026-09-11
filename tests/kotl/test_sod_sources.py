"""SOD source resolution for submit-kelai: flex / prior-target / csv / flat + recon."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("h5py")

from ki_ops.kotl.fake_flex import FakeFlexAdapter
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import (
    ReconDivergenceError,
    _resolve_sod_source,
    reconcile_flex_vs_prior,
    submit_kelai_shares,
)
from tests.kotl.test_kelaidata_source import make_ds2_h5

TD = date(2026, 8, 6)
TS = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)


@pytest.fixture()
def shares_dir(tmp_path):
    """Today's targets + yesterday's Portfolio file in one folder."""
    d = tmp_path / "shares"
    d.mkdir()
    (d / "Portfolio_20260806.csv").write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")
    (d / "Portfolio_20260805.csv").write_text("AAPL,20,VWAP\n")
    return d


def _submit(tmp_path, shares_dir, **kwargs):
    store = KotlStore(tmp_path / "kotl")
    submit = submit_kelai_shares(
        store,
        trade_date=TD,
        shares_file=shares_dir / "Portfolio_20260806.csv",
        ds2_h5=make_ds2_h5(tmp_path / "ds2.h5"),
        adapter=FakeFlexAdapter(),
        submitted_at=TS,
        cache_dir=tmp_path / "cache",
        **kwargs,
    )
    return store, submit


def _sides(store, submit):
    rows = store.load_working_orders(submit_id=submit.submit_id)
    return {r.symbol: r.sent_qty for r in rows}


# ---------------------------------------------------------------------------
# source resolution + legacy mapping
# ---------------------------------------------------------------------------


def test_resolve_sod_source_legacy_mapping():
    assert _resolve_sod_source(None, Path("sod.csv"), False) == "csv"
    assert _resolve_sod_source(None, None, True) == "flat"
    assert _resolve_sod_source("flex", None, False) == "flex"
    with pytest.raises(ValueError, match="SOD is required"):
        _resolve_sod_source(None, None, False)
    with pytest.raises(ValueError, match="unknown sod_source"):
        _resolve_sod_source("yolo", None, False)


def test_flat_sod_trades_full_book(tmp_path, shares_dir):
    store, submit = _submit(tmp_path, shares_dir, sod_source="flat")
    assert _sides(store, submit) == {
        "AAPL.US": Decimal("50"),
        "MSFT.US": Decimal("-30"),
    }


def test_csv_sod_legacy_arg(tmp_path, shares_dir):
    sod = tmp_path / "sod.csv"
    sod.write_text("symbol,quantity,market_price\nAAPL,20,190\n")
    store, submit = _submit(tmp_path, shares_dir, sod_csv=sod)  # no sod_source
    assert _sides(store, submit) == {
        "AAPL.US": Decimal("30"),
        "MSFT.US": Decimal("-30"),
    }


# ---------------------------------------------------------------------------
# flex SOD + reconciliation guard
# ---------------------------------------------------------------------------


def test_flex_sod_strips_suffix_and_recons_clean(tmp_path, shares_dir, capsys):
    store, submit = _submit(
        tmp_path,
        shares_dir,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("20")},  # matches prior target exactly
    )
    assert _sides(store, submit) == {
        "AAPL.US": Decimal("30"),  # 50 target − 20 flex SOD
        "MSFT.US": Decimal("-30"),
    }
    out = capsys.readouterr().out
    assert "SOD reconciliation" in out
    assert "no divergence" in out


def test_flex_sod_recon_guard_trips(tmp_path, shares_dir, capsys):
    with pytest.raises(ReconDivergenceError) as err:
        _submit(
            tmp_path,
            shares_dir,
            sod_source="flex",
            flex_positions={"AAPL.US": Decimal("25")},  # prior target said 20
        )
    report = err.value.report
    assert report.total_abs_diff == Decimal("5")
    assert report.names_diverged == 1
    out = capsys.readouterr().out
    assert "AAPL" in out and "breached=True" in out
    # nothing submitted, nothing persisted
    assert not (tmp_path / "kotl" / "submits.csv").exists()


def test_flex_sod_recon_guard_threshold_pass(tmp_path, shares_dir):
    store, submit = _submit(
        tmp_path,
        shares_dir,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("25")},
        recon_max_shares=Decimal("10"),
        recon_max_names=5,
    )
    assert submit.ok
    assert _sides(store, submit)["AAPL.US"] == Decimal("25")  # 50 − 25


def test_flex_sod_recon_dry_run_reports_but_never_aborts(tmp_path, shares_dir, capsys):
    store, submit = _submit(
        tmp_path,
        shares_dir,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("25")},
        dry_run=True,
    )
    out = capsys.readouterr().out
    assert "breached=True" in out
    assert "would abort" in out
    assert submit.flex_response == {"dry_run": True}
    assert not (tmp_path / "kotl" / "submits.csv").exists()


def test_flex_sod_no_prior_file_warns_and_proceeds(tmp_path, capsys):
    d = tmp_path / "shares"
    d.mkdir()
    (d / "Portfolio_20260806.csv").write_text("AAPL,50,VWAP\n")
    store, submit = _submit(
        tmp_path,
        d,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("20")},
    )
    assert "skipping Flex-vs-prior-target reconciliation" in capsys.readouterr().out
    assert _sides(store, submit) == {"AAPL.US": Decimal("30")}


def test_recon_report_math():
    report = reconcile_flex_vs_prior(
        {"AAPL": Decimal("25"), "NKE": Decimal("-100")},
        {"AAPL": Decimal("20"), "MSFT": Decimal("10")},
        prior_file="Portfolio_20260805.csv",
        max_shares=Decimal("100"),
        max_names=1,
    )
    diffs = {sym: diff for sym, _, _, diff in report.diffs}
    assert diffs == {"AAPL": Decimal("5"), "NKE": Decimal("-100"), "MSFT": Decimal("-10")}
    assert report.total_abs_diff == Decimal("115")
    assert report.names_diverged == 3
    assert report.breached  # names 3 > max_names 1 even though shares > 100 too


# ---------------------------------------------------------------------------
# prior-target SOD
# ---------------------------------------------------------------------------


def test_prior_target_sod(tmp_path, shares_dir, capsys):
    store, submit = _submit(tmp_path, shares_dir, sod_source="prior-target")
    assert _sides(store, submit) == {
        "AAPL.US": Decimal("30"),  # 50 − 20 (yesterday's target)
        "MSFT.US": Decimal("-30"),
    }
    assert "SOD from prior target file" in capsys.readouterr().out


def test_prior_target_sod_missing_prior_raises(tmp_path):
    d = tmp_path / "shares"
    d.mkdir()
    (d / "Portfolio_20260806.csv").write_text("AAPL,50,VWAP\n")
    with pytest.raises(ValueError, match="no prior Portfolio"):
        _submit(tmp_path, d, sod_source="prior-target")


def test_prior_target_sod_flattens_dropped_names(tmp_path):
    d = tmp_path / "shares"
    d.mkdir()
    # Yesterday held TSLA; today's book drops it → flatten order expected.
    (d / "Portfolio_20260806.csv").write_text("AAPL,50,VWAP\n")
    (d / "Portfolio_20260805.csv").write_text("AAPL,20,VWAP\nTSLA,15,VWAP\n")
    store, submit = _submit(tmp_path, d, sod_source="prior-target")
    assert _sides(store, submit) == {
        "AAPL.US": Decimal("30"),
        "TSLA.US": Decimal("-15"),
    }


def test_strategy_id_shares_path():
    from ki_ops.kotl.kelaidata_source import default_shares_path

    assert (
        default_shares_path(TD, strategy_id="USATop2000_neutralized")
        == "s3://kelaitrading/portfolio/shares/USATop2000_neutralized/Portfolio_20260806.csv"
    )
    assert default_shares_path(TD) == "s3://kelaitrading/portfolio/shares/Portfolio_20260806.csv"
