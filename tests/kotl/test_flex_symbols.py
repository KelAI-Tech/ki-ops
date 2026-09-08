"""Pre-submit SecurityService resolution: candidates, cache, submit-path wiring."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ki_ops.kotl.flex_live import FlexConfig
from ki_ops.kotl.flex_symbols import (
    CACHE_FILENAME,
    batch_lookup,
    build_unresolved_rows,
    candidate_queries,
    format_resolution_summary,
    format_unresolved_table,
    lookup_security,
    render_unresolved_csv,
    resolve_flex_symbols,
)
from tests.kotl.fake_flex_sdk import (
    ID_SEDOL,
    ID_TICKER,
    FakeFlexBackend,
    install_fake_sdk,
    make_security,
)

CONFIG = FlexConfig(endpoint="127.0.0.1:50051", token="tok")


@pytest.fixture()
def backend(monkeypatch):
    be = FakeFlexBackend()
    # Live-verified UAT master shape (2026-09-08): plain names under
    # TICKER.US, class shares ONLY under the slash spelling with the dotted
    # ticker as a TICKER identifier, SEDOLs resolvable as lookup queries.
    be.security_master = [
        make_security(
            "AAPL.US", 15, identifiers=[(ID_TICKER, "AAPL"), (ID_SEDOL, "2046251")]
        ),
        make_security("MSFT.US", 540, identifiers=[(ID_TICKER, "MSFT")]),
        make_security(
            "BF/B.US", 366, identifiers=[(ID_TICKER, "BF.B"), (ID_SEDOL, "2146838")]
        ),
        make_security("BRK/B.US", 367, identifiers=[(ID_TICKER, "BRK.B")]),
    ]
    install_fake_sdk(monkeypatch, be)
    return be


# ---------------------------------------------------------------------------
# candidate generation
# ---------------------------------------------------------------------------


def test_candidates_plain_ticker():
    assert candidate_queries("AAPL") == [("symbol", "AAPL.US")]


def test_candidates_undotted_class_share():
    assert candidate_queries("BFB") == [
        ("symbol", "BFB.US"),
        ("ticker_alias", "BF.B"),
        ("slash_symbol", "BF/B.US"),
    ]


def test_candidates_dotted_class_share():
    assert candidate_queries("BF.B") == [
        ("ticker_alias", "BF.B"),
        ("slash_symbol", "BF/B.US"),
    ]


def test_candidates_sedol_first():
    assert candidate_queries("AAPL", sedol="2046251") == [
        ("sedol", "2046251"),
        ("symbol", "AAPL.US"),
    ]


def test_candidates_no_class_alias_for_long_or_nonclass_letters():
    # Class letters are restricted to A/B/C/K — GOLD must not try GOL.D.
    assert candidate_queries("GOLD") == [("symbol", "GOLD.US")]


# ---------------------------------------------------------------------------
# lookup_security / batch_lookup
# ---------------------------------------------------------------------------


def test_lookup_security_canonical_symbol_and_identifiers(backend):
    hit = lookup_security(CONFIG, "BF.B")
    assert hit is not None
    assert hit["flex_symbol"] == "BF/B.US"  # commonData.symbol, not the query
    assert hit["flex_security_id"] == 366
    assert hit["identifiers"]["SEDOL"] == "2146838"
    assert lookup_security(CONFIG, "NOPE.US") is None


def test_batch_lookup_order_and_misses(backend):
    backend.security_chunk_size = 2  # multiple stream messages per batch
    results = batch_lookup(CONFIG, ["AAPL.US", "NOPE.US", "2046251", "BF.B", "X.Y"])
    assert [r["flex_symbol"] if r else None for r in results] == [
        "AAPL.US",
        None,
        "AAPL.US",  # SEDOL query resolves via the symbol field (live-verified)
        "BF/B.US",
        None,
    ]


def test_batch_lookup_chunks_requests(backend):
    batch_lookup(CONFIG, ["AAPL.US", "MSFT.US", "BF/B.US"], chunk_size=2)
    assert backend.batch_lookup_calls == 2  # 2 + 1


# ---------------------------------------------------------------------------
# resolve_flex_symbols: fallback, sedol, cache
# ---------------------------------------------------------------------------


def test_resolve_candidate_fallback(backend):
    resolved, unresolved, details = resolve_flex_symbols(
        CONFIG, ["AAPL", "BFB", "BRKB", "BABA"]
    )
    assert resolved == {"AAPL": "AAPL.US", "BFB": "BF/B.US", "BRKB": "BRK/B.US"}
    assert unresolved == ["BABA"]
    assert details["AAPL"]["resolved_via"] == "symbol"
    assert details["BFB"]["resolved_via"] == "ticker_alias"
    assert details["BFB"]["tried"] == ["BFB.US", "BF.B"]
    # BABA ends in a class letter, so the (missing) class aliases are tried too.
    assert details["BABA"]["tried"] == ["BABA.US", "BAB.A", "BAB/A.US"]


def test_resolve_sedol_preferred(backend):
    resolved, unresolved, details = resolve_flex_symbols(
        CONFIG, ["AAPL"], sedols={"AAPL": "2046251"}
    )
    assert resolved == {"AAPL": "AAPL.US"}
    assert details["AAPL"]["resolved_via"] == "sedol"
    assert details["AAPL"]["query"] == "2046251"


def test_resolve_cache_reused_and_unresolved_rechecked(backend, tmp_path):
    cache = tmp_path / CACHE_FILENAME
    resolved, unresolved, _ = resolve_flex_symbols(
        CONFIG, ["AAPL", "BFB", "BABA"], cache_path=cache
    )
    assert resolved["BFB"] == "BF/B.US" and unresolved == ["BABA"]
    calls_after_first = backend.batch_lookup_calls

    data = json.loads(cache.read_text())
    assert set(data["entries"]) == {"AAPL", "BFB"}  # unresolved never cached

    resolved2, unresolved2, details2 = resolve_flex_symbols(
        CONFIG, ["AAPL", "BFB", "BABA"], cache_path=cache
    )
    assert resolved2 == {"AAPL": "AAPL.US", "BFB": "BF/B.US"}
    assert unresolved2 == ["BABA"]
    assert details2["AAPL"]["resolved_via"] == "cache"
    # Only BABA (unresolved, always re-checked) hit the network again — three
    # candidate rounds (BABA.US, BAB.A, BAB/A.US), all single-name batches.
    queries = [e.symbol for e in backend.last_batch_lookup_request.security]
    assert queries == ["BAB/A.US"]
    assert backend.batch_lookup_calls == calls_after_first + 3


def test_resolve_empty_and_dedupe(backend):
    resolved, unresolved, _ = resolve_flex_symbols(CONFIG, ["AAPL", "aapl ", "AAPL"])
    assert resolved == {"AAPL": "AAPL.US"}
    assert unresolved == []
    assert resolve_flex_symbols(CONFIG, []) == ({}, [], {})


# ---------------------------------------------------------------------------
# unresolved report
# ---------------------------------------------------------------------------


def test_unresolved_rows_and_csv():
    details = {"BABA": {"tried": ["BABA.US"]}}
    payloads = [{"symbol": "BABA.US", "side": "BUY", "quantity": 10.0}]
    rows = build_unresolved_rows(["BABA"], details, payloads)
    assert rows == [
        {
            "ticker": "BABA",
            "payload_symbol": "BABA.US",
            "side": "BUY",
            "quantity": 10.0,
            "candidates_tried": "BABA.US",
        }
    ]
    text = render_unresolved_csv(rows)
    assert text.splitlines()[0] == "ticker,payload_symbol,side,quantity,candidates_tried"
    assert "BABA,BABA.US,BUY,10.0,BABA.US" in text
    table = format_unresolved_table(rows)
    assert "UNRESOLVED SECURITIES (1)" in table and "FlexTrade" in table


def test_resolution_summary_counts_and_rewrites():
    resolved = {"AAPL": "AAPL.US", "BFB": "BF/B.US"}
    details = {
        "AAPL": {"resolved_via": "symbol"},
        "BFB": {"resolved_via": "ticker_alias"},
    }
    text = format_resolution_summary(resolved, ["BABA"], details)
    assert "2 resolved" in text and "1 unresolved" in text
    assert "symbol=1" in text and "ticker_alias=1" in text
    assert "BFB→BF/B.US" in text


# ---------------------------------------------------------------------------
# submit-path wiring (env UAT + fake SDK + fake adapter)
# ---------------------------------------------------------------------------

TD = date(2026, 8, 6)
TS = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)


@pytest.fixture()
def submit_env(backend, tmp_path):
    pytest.importorskip("h5py")
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    shares = tmp_path / "shares"
    shares.mkdir()
    # BFB is the ds2 (undotted) spelling; BABA is absent from the fake master.
    (shares / "Portfolio_20260806.csv").write_text("AAPL,50,VWAP\nBFB,10,VWAP\nBABA,5,VWAP\n")
    h5 = make_ds2_h5(tmp_path / "ds2.h5", tickers=("AAPL", "MSFT", "BFB", "BABA"))
    return shares, h5


def _submit_kelai(tmp_path, submit_env, **kwargs):
    from ki_ops.kotl.fake_flex import FakeFlexAdapter
    from ki_ops.kotl.store import KotlStore
    from ki_ops.kotl.submit import submit_kelai_shares

    shares, h5 = submit_env
    store = KotlStore(tmp_path / "kotl")
    submit = submit_kelai_shares(
        store,
        trade_date=TD,
        shares_file=shares / "Portfolio_20260806.csv",
        ds2_h5=h5,
        sod_source="flat",
        env="UAT",
        adapter=FakeFlexAdapter(),
        flex_config=CONFIG,
        submitted_at=TS,
        cache_dir=tmp_path / "cache",
        **kwargs,
    )
    return store, submit


def test_submit_block_mode_raises_and_writes_report(tmp_path, submit_env, capsys):
    from ki_ops.kotl.submit import UnresolvedSecuritiesError

    with pytest.raises(UnresolvedSecuritiesError) as err:
        _submit_kelai(tmp_path, submit_env)
    assert err.value.unresolved == ["BABA"]
    out = capsys.readouterr().out
    assert "UNRESOLVED SECURITIES" in out
    assert "unresolved securities file:" in out
    # report written next to the (never-written) trade file, nothing persisted
    reports = list((tmp_path / "kotl" / "trades").rglob("unresolved_*.csv"))
    assert len(reports) == 1
    assert "BABA" in reports[0].read_text()
    assert not (tmp_path / "kotl" / "submits.csv").exists()


def test_submit_skip_mode_submits_resolved_canonical(tmp_path, submit_env, capsys):
    store, submit = _submit_kelai(tmp_path, submit_env, unresolved="skip")
    assert submit.ok
    rows = store.load_working_orders(submit_id=submit.submit_id)
    # canonical Flex symbol stored in working orders; BABA skipped
    assert {r.symbol for r in rows} == {"AAPL.US", "BF/B.US"}
    payload_by_symbol = {p["symbol"]: p for p in submit.payload}
    assert payload_by_symbol["BF/B.US"]["sourceSymbol"] == "BFB.US"
    assert "sourceSymbol" not in payload_by_symbol["AAPL.US"]
    out = capsys.readouterr().out
    assert "--unresolved skip: submitting 2 resolved order(s), skipping 1" in out
    reports = list((tmp_path / "kotl" / "trades").rglob("unresolved_*.csv"))
    assert len(reports) == 1


def test_submit_all_resolved_no_report(tmp_path, backend, submit_env):
    shares, h5 = submit_env
    (shares / "Portfolio_20260806.csv").write_text("AAPL,50,VWAP\nBFB,10,VWAP\n")
    store, submit = _submit_kelai(tmp_path, (shares, h5))
    assert submit.ok
    assert {p["symbol"] for p in submit.payload} == {"AAPL.US", "BF/B.US"}
    assert not list((tmp_path / "kotl" / "trades").rglob("unresolved_*.csv"))


def test_submit_dry_run_reports_would_block(tmp_path, submit_env, capsys):
    store, submit = _submit_kelai(tmp_path, submit_env, dry_run=True)
    out = capsys.readouterr().out
    assert "flex symbol resolution:" in out
    assert "a live submit would block (exit 6)" in out
    # dry-run still rewrites resolved payloads to canonical
    assert {p["symbol"] for p in submit.payload} == {"AAPL.US", "BF/B.US", "BABA.US"}
    assert not (tmp_path / "kotl" / "submits.csv").exists()
    reports = list((tmp_path / "kotl" / "trades").rglob("unresolved_*_dryrun.csv"))
    assert len(reports) == 1


def test_submit_dry_run_survives_resolution_outage(tmp_path, backend, submit_env, capsys):
    backend.security_error = RuntimeError("network unreachable")
    store, submit = _submit_kelai(tmp_path, submit_env, dry_run=True)
    out = capsys.readouterr().out
    assert "flex symbol resolution unavailable" in out
    assert {p["symbol"] for p in submit.payload} == {"AAPL.US", "BFB.US", "BABA.US"}


def test_submit_live_resolution_outage_fails(tmp_path, backend, submit_env):
    backend.security_error = RuntimeError("network unreachable")
    with pytest.raises(RuntimeError, match="network unreachable"):
        _submit_kelai(tmp_path, submit_env)


def test_submit_fake_env_skips_resolution(tmp_path, backend, submit_env):
    from ki_ops.kotl.fake_flex import FakeFlexAdapter
    from ki_ops.kotl.store import KotlStore
    from ki_ops.kotl.submit import submit_kelai_shares

    shares, h5 = submit_env
    store = KotlStore(tmp_path / "kotl")
    submit = submit_kelai_shares(
        store,
        trade_date=TD,
        shares_file=shares / "Portfolio_20260806.csv",
        ds2_h5=h5,
        sod_source="flat",
        env="FAKE",
        adapter=FakeFlexAdapter(),
        submitted_at=TS,
        cache_dir=tmp_path / "cache",
    )
    assert backend.batch_lookup_calls == 0
    assert {p["symbol"] for p in submit.payload} == {"AAPL.US", "BFB.US", "BABA.US"}


def test_submit_unresolved_mode_validated(tmp_path, submit_env):
    with pytest.raises(ValueError, match="unresolved must be"):
        _submit_kelai(tmp_path, submit_env, unresolved="yolo")


def test_cli_unresolved_exit_code(monkeypatch, tmp_path):
    import argparse

    from ki_ops.kotl import cli as kotl_cli
    from ki_ops.kotl.submit import UnresolvedSecuritiesError

    def boom(*args, **kwargs):
        raise UnresolvedSecuritiesError("UNRESOLVED SECURITIES: 1 symbol", ["BABA"])

    monkeypatch.setattr("ki_ops.kotl.submit.submit_kelai_shares", boom)
    args = argparse.Namespace(
        kotl_command="submit-kelai",
        trade_date=TD,
        shares=None,
        ds2=None,
        sod=None,
        assume_flat_sod=True,
        sod_source=None,
        strategy_id=None,
        flex_env="FAKE",
        dry_run=False,
        force=False,
        recon_max_shares=None,
        recon_max_names=None,
        max_orders=None,
        max_gross_notional=None,
        trade_file_out=None,
        unresolved="block",
        data_dir=tmp_path,
        cache_dir=None,
        store="csv",
        db_secret=None,
        db_schema=None,
    )
    assert kotl_cli.run_kotl(args) == kotl_cli.EXIT_UNRESOLVED_SECURITIES
