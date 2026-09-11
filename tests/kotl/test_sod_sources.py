"""SOD source resolution for submit-kelai: flex / prior-target / csv / flat + recon."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("h5py")

from types import SimpleNamespace

from ki_ops.kotl.fake_flex import FakeFlexAdapter
from ki_ops.kotl.flex_live import FlexConfig
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import (
    ReconDivergenceError,
    _flex_book_to_ds2,
    _resolve_sod_source,
    reconcile_books,
    submit_kelai_shares,
)
from tests.kotl.fake_flex_sdk import (
    ID_SEDOL,
    ID_TICKER,
    FakeFlexBackend,
    install_fake_sdk,
    make_security,
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


def _submit(tmp_path, shares_dir, *, store=None, **kwargs):
    store = store or KotlStore(tmp_path / "kotl")
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
    assert "skipping SOD reconciliation" in capsys.readouterr().out
    assert _sides(store, submit) == {"AAPL.US": Decimal("30")}


def test_recon_report_math():
    report = reconcile_books(
        {"AAPL": Decimal("25"), "NKE": Decimal("-100")},
        {"AAPL": Decimal("20"), "MSFT": Decimal("10")},
        baseline="prior target (Portfolio_20260805.csv)",
        max_shares=Decimal("100"),
        max_names=1,
    )
    diffs = {sym: diff for sym, _, _, diff in report.diffs}
    assert diffs == {"AAPL": Decimal("5"), "NKE": Decimal("-100"), "MSFT": Decimal("-10")}
    assert report.total_abs_diff == Decimal("115")
    assert report.names_diverged == 3
    assert report.breached  # names 3 > max_names 1 even though shares > 100 too


# ---------------------------------------------------------------------------
# flex SOD + book-snapshot recon (the normal, post-bootstrap path)
# ---------------------------------------------------------------------------


def test_flex_sod_snapshot_recon_passes_despite_partial_fill_day(
    tmp_path, shares_dir, capsys
):
    """Yesterday's target said 20 but only 15 filled. The nightly snapshot
    holds the real book (15); live book == snapshot → strict 0/0 recon passes
    with no operator override, and the send tops the position up (50−15)."""
    store = KotlStore(tmp_path / "kotl")
    store.record_book_snapshot(date(2026, 8, 5), "FAKE", {"AAPL.US": Decimal("15")})
    store, submit = _submit(
        tmp_path,
        shares_dir,
        store=store,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("15")},
    )
    assert _sides(store, submit) == {
        "AAPL.US": Decimal("35"),  # 50 target − 15 live book
        "MSFT.US": Decimal("-30"),
    }
    out = capsys.readouterr().out
    assert "book snapshot as of 2026-08-05" in out
    assert "no divergence" in out


def test_flex_sod_snapshot_recon_trips_on_overnight_drift(tmp_path, shares_dir, capsys):
    store = KotlStore(tmp_path / "kotl")
    store.record_book_snapshot(date(2026, 8, 5), "FAKE", {"AAPL.US": Decimal("20")})
    with pytest.raises(ReconDivergenceError) as err:
        _submit(
            tmp_path,
            shares_dir,
            store=store,
            sod_source="flex",
            flex_positions={"AAPL.US": Decimal("25")},  # book moved overnight
        )
    report = err.value.report
    assert report.total_abs_diff == Decimal("5")
    assert "book snapshot as of 2026-08-05" in str(err.value)
    out = capsys.readouterr().out
    assert "breached=True" in out
    assert not (tmp_path / "kotl" / "submits.csv").exists()


def test_flex_sod_snapshot_recon_uses_latest_before_trade_date(tmp_path, shares_dir):
    """Only snapshots strictly before the trade date count; the newest wins."""
    store = KotlStore(tmp_path / "kotl")
    store.record_book_snapshot(date(2026, 8, 1), "FAKE", {"AAPL.US": Decimal("99")})
    store.record_book_snapshot(date(2026, 8, 5), "FAKE", {"AAPL.US": Decimal("20")})
    # same-day snapshot must not be the baseline (it would be post-fill)
    store.record_book_snapshot(date(2026, 8, 6), "FAKE", {"AAPL.US": Decimal("0")})
    store, submit = _submit(
        tmp_path,
        shares_dir,
        store=store,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("20")},
    )
    assert submit.ok


def test_flex_sod_stale_snapshot_warns(tmp_path, shares_dir, capsys):
    store = KotlStore(tmp_path / "kotl")
    store.record_book_snapshot(date(2026, 7, 30), "FAKE", {"AAPL.US": Decimal("20")})
    _submit(
        tmp_path,
        shares_dir,
        store=store,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("20")},
    )
    out = capsys.readouterr().out
    assert "book snapshot is 7 days old" in out


def test_flex_sod_no_snapshot_falls_back_to_prior_target(tmp_path, shares_dir, capsys):
    store, submit = _submit(
        tmp_path,
        shares_dir,
        sod_source="flex",
        flex_positions={"AAPL.US": Decimal("20")},
    )
    out = capsys.readouterr().out
    assert "recon falls back to the prior target file" in out
    assert "prior target (" in out
    assert submit.ok


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


# ---------------------------------------------------------------------------
# flex SOD: inverse Flex→ds2 symbol map (live envs)
# ---------------------------------------------------------------------------

FLEX_CONFIG = FlexConfig(endpoint="127.0.0.1:50051", token="tok")


def _sedol_csv(tmp_path, rows: dict[str, str]):
    path = tmp_path / "sedols.csv"
    path.write_text(
        "infocode,sedol\n" + "".join(f"{i},{s}\n" for i, s in rows.items())
    )
    return path


def test_flex_book_to_ds2_fake_env_keeps_bare_tickers():
    """Non-live envs never touch the SecurityService: bare-ticker book only."""
    book = _flex_book_to_ds2(
        {"AAPL.US": Decimal("10"), "BF/B.US": Decimal("5")},
        snapshot=SimpleNamespace(infocode_by_ticker={}),
        target_tickers={"AAPL"},
        env="FAKE",
        flex_config=None,
        symbol_suffix=".US",
        sedol_source="none",
        cache_dir=None,
        data_dir=None,
    )
    assert book == {"AAPL": Decimal("10"), "BF/B": Decimal("5")}


def test_flex_book_to_ds2_translates_sedol_and_class_share_names(
    tmp_path, monkeypatch, capsys
):
    """The two prod failure shapes: SEDOL-translated (CCCS.US ← CCC) and
    slash class-share (BF/B.US ← BFB) live symbols join back to ds2 tickers."""
    backend = FakeFlexBackend()
    backend.security_master = [
        make_security("CCCS.US", 1, identifiers=[(ID_SEDOL, "BP4CXL8")]),
        make_security("BF/B.US", 2, identifiers=[(ID_TICKER, "BF.B")]),
        make_security("AAPL.US", 3),
    ]
    install_fake_sdk(monkeypatch, backend)
    book = _flex_book_to_ds2(
        {"CCCS.US": Decimal("100"), "BF/B.US": Decimal("50"), "AAPL.US": Decimal("10")},
        snapshot=SimpleNamespace(
            infocode_by_ticker={"CCC": "7", "BFB": "8", "AAPL": "9"}
        ),
        target_tickers={"CCC", "BFB", "AAPL"},
        env="UAT",
        flex_config=FLEX_CONFIG,
        symbol_suffix=".US",
        sedol_source=str(_sedol_csv(tmp_path, {"7": "BP4CXL8"})),
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    )
    assert book == {
        "CCC": Decimal("100"),
        "BFB": Decimal("50"),
        "AAPL": Decimal("10"),
    }
    out = capsys.readouterr().out
    assert "inverse symbol map" in out
    assert "CCCS.US→CCC" in out and "BF/B.US→BFB" in out


def test_flex_book_to_ds2_reverse_maps_sod_only_dropped_names(
    tmp_path, monkeypatch, capsys
):
    """Positions today's target dropped (zero-target / disappeared names, the
    2026-09-11 second failure): the target-based inverse map cannot know them,
    so the leftover live symbol is reverse-resolved — Flex master SEDOL →
    security master infocode → snapshot ds2 ticker. Names with no SEDOL→ds2
    hop keep the bare ticker (and fail closed downstream)."""
    backend = FakeFlexBackend()
    backend.security_master = [
        make_security("CCCS.US", 1, identifiers=[(ID_SEDOL, "BP4CXL8")]),
        make_security("BRK/B.US", 2, identifiers=[(ID_SEDOL, "2073390")]),
        make_security("ATGE.US", 3, identifiers=[(ID_SEDOL, "2110255")]),
        make_security("ZZZQ.US", 4),  # no SEDOL in the master
    ]
    install_fake_sdk(monkeypatch, backend)
    book = _flex_book_to_ds2(
        {
            "CCCS.US": Decimal("100"),  # in target — first-pass inverse map
            "BRK/B.US": Decimal("10"),  # dropped slash class share
            "ATGE.US": Decimal("5"),  # dropped ds2-renamed name
            "ZZZQ.US": Decimal("1"),  # unmappable — stays bare
        },
        snapshot=SimpleNamespace(
            infocode_by_ticker={"CCC": "7", "BRKB": "55", "DV": "56"},
            price=lambda ticker: None,
        ),
        target_tickers={"CCC"},
        env="UAT",
        flex_config=FLEX_CONFIG,
        symbol_suffix=".US",
        sedol_source=str(
            _sedol_csv(tmp_path, {"7": "BP4CXL8", "55": "2073390", "56": "2110255"})
        ),
        cache_dir=tmp_path / "cache",
        data_dir=None,
    )
    assert book == {
        "CCC": Decimal("100"),
        "BRKB": Decimal("10"),
        "DV": Decimal("5"),
        "ZZZQ": Decimal("1"),
    }
    out = capsys.readouterr().out
    assert "BRK/B.US→BRKB" in out and "ATGE.US→DV" in out
    assert "no SEDOL→ds2 mapping" in out and "ZZZQ.US" in out


def test_flex_book_to_ds2_outage_warns_and_falls_back(tmp_path, monkeypatch, capsys):
    backend = FakeFlexBackend()
    backend.security_error = RuntimeError("flex is down")
    install_fake_sdk(monkeypatch, backend)
    book = _flex_book_to_ds2(
        {"CCCS.US": Decimal("100")},
        snapshot=SimpleNamespace(infocode_by_ticker={"CCC": "7"}),
        target_tickers={"CCC"},
        env="UAT",
        flex_config=FLEX_CONFIG,
        symbol_suffix=".US",
        sedol_source="none",
        cache_dir=tmp_path / "cache",
        data_dir=None,
    )
    assert book == {"CCCS": Decimal("100")}  # bare fallback, fails closed later
    assert "inverse symbol map unavailable" in capsys.readouterr().out


def test_flex_book_to_ds2_collision_refuses(tmp_path, monkeypatch):
    """Two ds2 tickers resolving to ONE flex symbol is data corruption —
    refuse rather than silently merging their positions."""
    backend = FakeFlexBackend()
    backend.security_master = [
        make_security("CCCS.US", 1, identifiers=[(ID_SEDOL, "BP4CXL8")]),
    ]
    install_fake_sdk(monkeypatch, backend)
    with pytest.raises(ValueError, match="two ds2 tickers"):
        _flex_book_to_ds2(
            {"CCCS.US": Decimal("100")},
            snapshot=SimpleNamespace(infocode_by_ticker={"CCC": "7", "XYZ": "8"}),
            target_tickers={"CCC", "XYZ"},
            env="UAT",
            flex_config=FLEX_CONFIG,
            symbol_suffix=".US",
            sedol_source=str(_sedol_csv(tmp_path, {"7": "BP4CXL8", "8": "BP4CXL8"})),
            cache_dir=tmp_path / "cache",
            data_dir=None,
        )


def test_uat_flex_sod_prices_sedol_translated_position(tmp_path, monkeypatch, capsys):
    """Regression for the 2026-09-11 prod failures: we hold CCCS.US (bought by
    yesterday's submit under the canonical Flex spelling of ds2 ticker CCC —
    still in today's target) and BRK/B.US (ds2 BRKB, dropped from today's
    target → flatten). Without the inverse map the first raised ``flex SOD
    tickers have no ds2 close``; without the reverse SOD-only pass the second
    did. Now CCC tops up 30 and BRKB flattens −10, both sent under their
    canonical Flex symbols."""
    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("CCC,50,VWAP\nAAPL,-30,VWAP\n")
    backend = FakeFlexBackend()
    backend.security_master = [
        make_security("CCCS.US", 1, identifiers=[(ID_SEDOL, "BP4CXL8")]),
        make_security("AAPL.US", 2),
        make_security("BRK/B.US", 3, identifiers=[(ID_SEDOL, "2073390"), (ID_TICKER, "BRK.B")]),
    ]
    install_fake_sdk(monkeypatch, backend)
    h5 = make_ds2_h5(tmp_path / "ds2.h5", tickers=("CCC", "AAPL", "BRKB"))
    # tickers-mode infocodes are 101+i: CCC=101, AAPL=102, BRKB=103
    sedol_csv = _sedol_csv(tmp_path, {"101": "BP4CXL8", "103": "2073390"})

    store = KotlStore(tmp_path / "kotl")
    submit = submit_kelai_shares(
        store,
        trade_date=TD,
        shares_file=shares,
        ds2_h5=h5,
        sod_source="flex",
        flex_positions={"CCCS.US": Decimal("20"), "BRK/B.US": Decimal("10")},
        flex_config=FLEX_CONFIG,
        env="UAT",
        sedol_source=str(sedol_csv),
        adapter=FakeFlexAdapter(),
        submitted_at=TS,
        cache_dir=tmp_path / "cache",
    )
    assert submit.ok
    rows = store.load_working_orders(submit_id=submit.submit_id)
    assert {r.symbol: r.sent_qty for r in rows} == {
        "CCCS.US": Decimal("30"),  # 50 target − 20 held, sent as canonical Flex
        "AAPL.US": Decimal("-30"),
        "BRK/B.US": Decimal("-10"),  # dropped name flattened at the ds2 close
    }
    out = capsys.readouterr().out
    assert "CCCS.US→CCC" in out and "BRK/B.US→BRKB" in out


def test_strategy_id_shares_path():
    from ki_ops.kotl.kelaidata_source import default_shares_path

    assert (
        default_shares_path(TD, strategy_id="USATop2000_neutralized")
        == "s3://kelaitrading/portfolio/shares/USATop2000_neutralized/Portfolio_20260806.csv"
    )
    assert default_shares_path(TD) == "s3://kelaitrading/portfolio/shares/Portfolio_20260806.csv"
