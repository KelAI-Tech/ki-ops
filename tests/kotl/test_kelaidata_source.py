"""kelaidata S3/H5 source: shares file parsing, ds2 snapshot, submit path."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

h5py = pytest.importorskip("h5py")
np = pytest.importorskip("numpy")

from ki_ops.kotl.kelaidata_source import (
    Ds2Snapshot,
    default_shares_path,
    fetch,
    load_ds2_snapshot,
    load_shares_trade_file,
    parse_s3_url,
    targets_from_shares,
)
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.submit import submit_kelai_shares

TD = date(2026, 8, 6)


def _ns(day: str) -> int:
    d = date.fromisoformat(day)
    return int(np.datetime64(datetime(d.year, d.month, d.day), "ns").astype("int64"))


def _write_panel(h5, key: str, dates: list[str], infocodes: list[int], values, dtype):
    grp = h5.create_group(key)
    grp.create_dataset("axis0", data=np.asarray(infocodes, dtype="int64"))
    grp.create_dataset("axis1", data=np.asarray([_ns(d) for d in dates], dtype="int64"))
    grp.create_dataset("block0_items", data=np.asarray(infocodes, dtype="int64"))
    grp.create_dataset("block0_values", data=np.asarray(values, dtype=dtype))


def make_ds2_h5(path, *, tsla_close_nan: bool = False):
    """Two dates × three infocodes (AAPL/MSFT/TSLA), pandas-fixed style layout."""
    dates = ["2026-08-04", "2026-08-05"]
    ids = [101, 102, 103]
    close = [[190.0, 500.0, 250.0], [191.5, 505.0, float("nan") if tsla_close_nan else 252.0]]
    adv = [[1e6, 2e6, 3e6], [1.1e6, 2.1e6, 3.1e6]]
    codes = [[0, 1, 2], [0, 1, 2]]
    with h5py.File(str(path), "w") as f:
        _write_panel(f, "ds2_data/CLOSE", dates, ids, close, "float64")
        _write_panel(f, "ds2_data/ADV20_ADJUSTED", dates, ids, adv, "float64")
        _write_panel(f, "ds2_data/TICKER_INDEX", dates, ids, codes, "int32")
        f.create_group("metadata").create_dataset(
            "TICKERS",
            data=np.asarray(["AAPL", "MSFT", "TSLA"], dtype=object),
            dtype=h5py.string_dtype(encoding="utf-8"),
        )
    return path


def test_default_shares_path():
    assert default_shares_path(TD) == "s3://kelaitrading/portfolio/shares/Portfolio_20260806.csv"


def test_parse_s3_url():
    assert parse_s3_url("s3://bucket/a/b.csv") == ("bucket", "a/b.csv")
    with pytest.raises(ValueError):
        parse_s3_url("s3://bucket-only")


def test_fetch_local_passthrough(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("AAPL,1,VWAP\n")
    assert fetch(f) == f
    with pytest.raises(FileNotFoundError):
        fetch(tmp_path / "missing.csv")


def test_load_shares_trade_file(tmp_path):
    f = tmp_path / "20260806.csv"
    f.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\nTSLA,0,VWAP\n")
    shares = load_shares_trade_file(f)
    assert shares == {"AAPL": Decimal("50"), "MSFT": Decimal("-30")}  # zero rows dropped


def test_load_shares_trade_file_skips_header(tmp_path):
    f = tmp_path / "h.csv"
    f.write_text("TICKER,shares,VWAP\naapl,7,VWAP\n")
    assert load_shares_trade_file(f) == {"AAPL": Decimal("7")}


def test_load_shares_trade_file_rejects_duplicates_and_fractions(tmp_path):
    dup = tmp_path / "dup.csv"
    dup.write_text("AAPL,5,VWAP\nAAPL,6,VWAP\n")
    with pytest.raises(ValueError, match="Duplicate tickers"):
        load_shares_trade_file(dup)

    frac = tmp_path / "frac.csv"
    frac.write_text("MSFT,1.5,VWAP\n")
    with pytest.raises(ValueError, match="Fractional"):
        load_shares_trade_file(frac)


def test_load_ds2_snapshot_prior_close(tmp_path):
    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    snap = load_ds2_snapshot(h5, trade_date=TD)
    assert snap.px_as_of == date(2026, 8, 5)
    assert snap.close_by_ticker["AAPL"] == Decimal("191.5")
    assert snap.adv_by_ticker["MSFT"] == Decimal("2100000")
    assert snap.infocode_by_ticker == {"AAPL": "101", "MSFT": "102", "TSLA": "103"}

    earlier = load_ds2_snapshot(h5, trade_date=date(2026, 8, 5))
    assert earlier.px_as_of == date(2026, 8, 4)
    assert earlier.close_by_ticker["AAPL"] == Decimal("190")

    with pytest.raises(ValueError, match="no ds2 rows before"):
        load_ds2_snapshot(h5, trade_date=date(2026, 8, 4))


def test_targets_from_shares_requires_prices(tmp_path):
    h5 = make_ds2_h5(tmp_path / "ds2_data.h5", tsla_close_nan=True)
    snap = load_ds2_snapshot(h5, trade_date=TD)
    with pytest.raises(ValueError, match="TSLA"):
        targets_from_shares({"AAPL": Decimal("5"), "TSLA": Decimal("10")}, snap)

    targets = targets_from_shares({"AAPL": Decimal("5"), "MSFT": Decimal("-3")}, snap)
    by_sym = {t.symbol: t for t in targets}
    assert by_sym["AAPL"].quantity == Decimal("5")
    assert by_sym["AAPL"].market_price == Decimal("191.5")
    assert by_sym["MSFT"].quantity == Decimal("-3")


def test_snapshot_price_lookup_is_case_insensitive():
    snap = Ds2Snapshot(
        px_as_of=date(2026, 8, 5),
        close_by_ticker={"AAPL": Decimal("191.5")},
        adv_by_ticker={},
        infocode_by_ticker={"AAPL": "101"},
    )
    assert snap.price("aapl") == Decimal("191.5")


def test_submit_kelai_shares_end_to_end(tmp_path):
    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares = tmp_path / "20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")
    sod = tmp_path / "sod.csv"
    sod.write_text("symbol,quantity,market_price\nAAPL,20,190\n")

    store = KotlStore(tmp_path / "data")
    submit = submit_kelai_shares(
        store,
        trade_date=TD,
        shares_file=shares,
        ds2_h5=h5,
        sod_csv=sod,
        cache_dir=tmp_path / "cache",
    )
    assert submit.ok

    orders = {o.symbol: o for o in store.load_working_orders(trade_date=TD)}
    assert set(orders) == {"AAPL.US", "MSFT.US"}
    assert orders["AAPL.US"].side == "BUY"
    assert orders["AAPL.US"].sent_qty == Decimal("30")  # 50 target − 20 SOD
    assert orders["MSFT.US"].side == "SELL"
    assert orders["MSFT.US"].sent_qty == Decimal("-30")

    payload_px = {p["symbol"]: p["price"] for p in submit.payload}
    assert payload_px["AAPL.US"] == pytest.approx(191.5)


def test_submit_kelai_shares_requires_explicit_sod(tmp_path):
    store = KotlStore(tmp_path / "data")
    with pytest.raises(ValueError, match="SOD is required"):
        submit_kelai_shares(store, trade_date=TD, shares_file=tmp_path / "x.csv", ds2_h5=tmp_path / "y.h5")


def test_submit_kelai_shares_flat_sod_trades_full_book(tmp_path):
    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares = tmp_path / "20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")

    store = KotlStore(tmp_path / "data")
    submit_kelai_shares(
        store,
        trade_date=TD,
        shares_file=shares,
        ds2_h5=h5,
        assume_flat_sod=True,
        cache_dir=tmp_path / "cache",
    )
    orders = {o.symbol: o for o in store.load_working_orders(trade_date=TD)}
    assert orders["AAPL.US"].sent_qty == Decimal("50")
    assert orders["MSFT.US"].sent_qty == Decimal("-30")


def test_ds2_missing_ticker_index_is_clear_error(tmp_path):
    path = tmp_path / "old.h5"
    dates = ["2026-08-05"]
    with h5py.File(str(path), "w") as f:
        _write_panel(f, "ds2_data/CLOSE", dates, [101], [[190.0]], "float64")
        _write_panel(f, "ds2_data/ADV20_ADJUSTED", dates, [101], [[1e6]], "float64")
    with pytest.raises(ValueError, match="TICKER_INDEX"):
        load_ds2_snapshot(path, trade_date=TD)
    # explicit map fallback works without the vocabulary
    snap = load_ds2_snapshot(path, trade_date=TD, ticker_to_infocode={"AAPL": "101"})
    assert snap.close_by_ticker["AAPL"] == Decimal("190")
