"""KOTL submit inputs straight from kelaidata artifacts on S3.

Two inputs, both produced by the kelaidata pipeline:

- **Shares trade file** — ``s3://kelaitrading/portfolio/shares/<YYYYMMDD>.csv``,
  written by ``dollar_to_shares``: headerless ``TICKER,shares[,VWAP]`` rows,
  signed whole-share **target positions** for the trade date.
- **ds2 H5** — ``s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5``: wide
  pandas-fixed panels ``ds2_data/<FIELD>`` (rows = dates, columns = LSEG
  infocodes) plus the ``TICKER_INDEX`` int32 code matrix decoded against the
  ``/metadata/TICKERS`` vocabulary. Read here with plain ``h5py`` one row at a
  time, so we never load the multi-GB panels.

Prices attached to targets are the last close **strictly before** the trade
date — the trade file built from session T−1 trades on T, so at submit time
the freshest close is T−1's.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ki_ops.intents import TargetIntent

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = ROOT / "data" / "kotl" / "cache"

DEFAULT_SHARES_TEMPLATE = "s3://kelaitrading/portfolio/shares/{yyyymmdd}.csv"
DEFAULT_DS2_H5 = "s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5"

DS2_NAMESPACE = "ds2_data"
TICKER_VOCABULARY = "metadata/TICKERS"
_TICKER_MISSING_CODE = -1


def default_shares_path(trade_date: date) -> str:
    return DEFAULT_SHARES_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))


def parse_s3_url(url: str) -> tuple[str, str]:
    """``s3://bucket/key`` → ``(bucket, key)``."""
    rest = url[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"not a valid s3 url: {url}")
    return bucket, key


def fetch(path: str | Path, *, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Path:
    """Return a local file for *path*, downloading ``s3://`` URLs into *cache_dir*.

    Cached files are keyed by bucket/key and re-downloaded only when the S3
    ETag differs from the one recorded at the last download.
    """
    text = str(path)
    if not text.startswith("s3://"):
        local = Path(path)
        if not local.is_file():
            raise FileNotFoundError(f"input file not found: {local}")
        return local

    import boto3

    bucket, key = parse_s3_url(text)
    cache_dir = Path(cache_dir)
    local = cache_dir / bucket / key
    etag_file = local.with_name(local.name + ".etag")

    s3 = boto3.client("s3")
    head = s3.head_object(Bucket=bucket, Key=key)
    etag = head["ETag"]
    if local.is_file() and etag_file.is_file() and etag_file.read_text() == etag:
        return local

    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_name(local.name + ".part")
    s3.download_file(bucket, key, str(tmp))
    tmp.replace(local)
    etag_file.write_text(etag)
    return local


def load_shares_trade_file(path: str | Path) -> dict[str, Decimal]:
    """Parse a ``dollar_to_shares`` output CSV → ordered ``{TICKER: signed shares}``.

    Headerless ``TICKER,shares[,VWAP]``; a first row whose second field is not
    numeric is tolerated as a header. Fractional shares and duplicate tickers
    are errors — this file is the tradeable book, so refuse anything ambiguous.
    """
    path = Path(path)
    out: dict[str, Decimal] = {}
    dupes: set[str] = set()
    fractional: list[str] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.reader(fh)):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) < 2:
                raise ValueError(f"{path}:{i + 1}: need at least TICKER,shares columns")
            ticker = row[0].strip().upper()
            raw_qty = row[1].strip().replace(",", "")
            try:
                qty = Decimal(raw_qty)
            except InvalidOperation:
                if i == 0:
                    continue  # header row
                raise ValueError(f"{path}:{i + 1}: non-numeric share qty {row[1]!r}") from None
            if not ticker:
                raise ValueError(f"{path}:{i + 1}: empty ticker")
            if qty != qty.to_integral_value():
                fractional.append(ticker)
                continue
            if ticker in out:
                dupes.add(ticker)
            if qty != 0:
                out[ticker] = qty
    if dupes:
        shown = ", ".join(sorted(dupes)[:20])
        raise ValueError(f"Duplicate tickers in shares trade file {path}: {shown}")
    if fractional:
        shown = ", ".join(fractional[:20])
        raise ValueError(f"Fractional share quantities in {path}: {shown}")
    return out


@dataclass(frozen=True)
class Ds2Snapshot:
    """One date's slice of the ds2 panels, keyed by ticker."""

    px_as_of: date
    close_by_ticker: dict[str, Decimal]
    adv_by_ticker: dict[str, Decimal]
    infocode_by_ticker: dict[str, str]

    def price(self, ticker: str) -> Decimal | None:
        return self.close_by_ticker.get(ticker.upper())


def _row_before(group, trade_date: date):
    """Last (date, row_index) in a pandas-fixed group strictly before *trade_date*."""
    import numpy as np

    axis1 = group["axis1"][:]  # int64 ns timestamps
    cutoff = np.datetime64(
        datetime(trade_date.year, trade_date.month, trade_date.day), "ns"
    ).astype("int64")
    positions = np.nonzero(axis1 < cutoff)[0]
    if positions.size == 0:
        raise ValueError(f"no ds2 rows before {trade_date.isoformat()}")
    pos = int(positions[-1])
    row_date = np.datetime64(int(axis1[pos]), "ns").astype("datetime64[D]")
    return date.fromisoformat(str(row_date)), pos


def _field_row(h5, field: str, trade_date: date) -> tuple[date, dict[str, float]]:
    """One field's row strictly before *trade_date* → ``{infocode: value}`` (NaN dropped)."""
    key = f"{DS2_NAMESPACE}/{field}"
    if key not in h5:
        raise KeyError(f"ds2 H5 missing group {key}")
    group = h5[key]
    as_of, pos = _row_before(group, trade_date)
    infocodes = group["axis0"][:]
    values = group["block0_values"][pos, :]
    out = {str(int(sid)): float(v) for sid, v in zip(infocodes, values) if v == v}
    return as_of, out


def _ticker_map(h5, trade_date: date) -> dict[str, str]:
    """Point-in-time ``{TICKER: infocode}`` from TICKER_INDEX + /metadata/TICKERS."""
    key = f"{DS2_NAMESPACE}/TICKER_INDEX"
    if key not in h5 or TICKER_VOCABULARY not in h5:
        raise ValueError(
            "ds2 H5 has no TICKER_INDEX / metadata/TICKERS — file predates the "
            "ticker vocabulary; pass an explicit ticker mapping instead"
        )
    group = h5[key]
    _, pos = _row_before(group, trade_date)
    infocodes = group["axis0"][:]
    codes = group["block0_values"][pos, :]
    vocab = h5[TICKER_VOCABULARY].asstr()[...]

    out: dict[str, str] = {}
    collisions: set[str] = set()
    for sid, code in zip(infocodes, codes):
        code = int(code)
        if code == _TICKER_MISSING_CODE:
            continue
        if code < 0 or code >= len(vocab):
            raise ValueError(f"TICKER_INDEX code {code} out of vocabulary range")
        ticker = str(vocab[code]).upper()
        if ticker in out:
            collisions.add(ticker)
        out[ticker] = str(int(sid))
    if collisions:
        shown = ", ".join(sorted(collisions)[:20])
        raise ValueError(
            f"Ticker owned by multiple infocodes on the snapshot date: {shown} "
            "— resolve upstream before trading"
        )
    return out


def load_ds2_snapshot(
    h5_path: str | Path,
    *,
    trade_date: date,
    ticker_to_infocode: dict[str, str] | None = None,
) -> Ds2Snapshot:
    """Prior-close CLOSE / ADV20_ADJUSTED slice keyed by ticker.

    *ticker_to_infocode* overrides the H5's own TICKER_INDEX decode (fallback
    for files that predate the vocabulary; e.g. from TICKER_MAPPING_DT).
    """
    import h5py

    with h5py.File(str(h5_path), "r") as h5:
        px_as_of, close_by_id = _field_row(h5, "CLOSE", trade_date)
        _, adv_by_id = _field_row(h5, "ADV20_ADJUSTED", trade_date)
        if ticker_to_infocode is None:
            ticker_to_infocode = _ticker_map(h5, trade_date)

    close: dict[str, Decimal] = {}
    adv: dict[str, Decimal] = {}
    for ticker, sid in ticker_to_infocode.items():
        px = close_by_id.get(sid)
        if px is not None and px > 0:
            close[ticker] = Decimal(str(px))
        vol = adv_by_id.get(sid)
        if vol is not None and vol > 0:
            adv[ticker] = Decimal(str(vol))
    return Ds2Snapshot(
        px_as_of=px_as_of,
        close_by_ticker=close,
        adv_by_ticker=adv,
        infocode_by_ticker=dict(ticker_to_infocode),
    )


def targets_from_shares(
    shares_by_ticker: dict[str, Decimal],
    snapshot: Ds2Snapshot,
) -> list[TargetIntent]:
    """Shares trade file + ds2 snapshot → priced :class:`TargetIntent` rows.

    Every target must have a prior close: silently unpriced targets would
    understate notionals downstream, so missing prices fail the whole submit.
    """
    missing = sorted(t for t in shares_by_ticker if snapshot.price(t) is None)
    if missing:
        shown = ", ".join(missing[:20])
        raise ValueError(
            f"{len(missing)} target tickers have no ds2 close as of "
            f"{snapshot.px_as_of.isoformat()}: {shown}{' …' if len(missing) > 20 else ''}"
        )
    return [
        TargetIntent(ticker, qty, snapshot.price(ticker))
        for ticker, qty in shares_by_ticker.items()
    ]
