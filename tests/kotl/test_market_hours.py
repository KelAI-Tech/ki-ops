"""Market-hours gate: NYSE calendar rules + the live-submit block (exit 7)."""

from __future__ import annotations

import contextlib
import io
from datetime import date, datetime, time, timezone

import pytest

from ki_ops.kotl.market_hours import (
    EARLY_CLOSE,
    REGULAR_CLOSE,
    is_early_close,
    market_close,
    market_hours_verdict,
    nyse_holidays,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# calendar: holidays, observance shifts, early closes
# ---------------------------------------------------------------------------


def test_nyse_holidays_2026():
    holidays = nyse_holidays(2026)
    assert holidays == {
        date(2026, 1, 1): "New Year's Day",
        date(2026, 1, 19): "Martin Luther King Jr. Day",
        date(2026, 2, 16): "Washington's Birthday",
        date(2026, 4, 3): "Good Friday",
        date(2026, 5, 25): "Memorial Day",
        date(2026, 6, 19): "Juneteenth",
        date(2026, 7, 3): "Independence Day",  # Jul 4 is a Saturday
        date(2026, 9, 7): "Labor Day",
        date(2026, 11, 26): "Thanksgiving Day",
        date(2026, 12, 25): "Christmas Day",
    }


@pytest.mark.parametrize(
    ("year", "good_friday"),
    [(2024, date(2024, 3, 29)), (2025, date(2025, 4, 18)), (2026, date(2026, 4, 3))],
)
def test_good_friday_from_easter_computus(year, good_friday):
    assert nyse_holidays(year)[good_friday] == "Good Friday"


def test_sunday_holidays_observed_monday():
    # Jul 4 2027 is a Sunday → observed Monday Jul 5.
    assert nyse_holidays(2027)[date(2027, 7, 5)] == "Independence Day"
    assert date(2027, 7, 4) not in nyse_holidays(2027)


def test_saturday_holidays_observed_friday():
    # Dec 25 2027 and Jun 19 2027 are Saturdays → observed the Friday before.
    assert nyse_holidays(2027)[date(2027, 12, 24)] == "Christmas Day"
    assert nyse_holidays(2027)[date(2027, 6, 18)] == "Juneteenth"


def test_new_years_on_saturday_is_not_observed():
    # Jan 1 2022 was a Saturday; NYSE kept Friday Dec 31 2021 as a trading day.
    assert date(2022, 1, 1) not in nyse_holidays(2022)
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert market_close(date(2021, 12, 31)) == REGULAR_CLOSE


def test_juneteenth_only_from_2022():
    assert not any(name == "Juneteenth" for name in nyse_holidays(2021).values())
    assert nyse_holidays(2022)[date(2022, 6, 20)] == "Juneteenth"  # Sun → Mon


def test_early_closes():
    assert is_early_close(date(2026, 11, 27))  # day after Thanksgiving
    assert is_early_close(date(2026, 12, 24))  # Thursday, Christmas on Friday
    assert is_early_close(date(2025, 7, 3))  # Thursday, Jul 4 on Friday
    assert not is_early_close(date(2026, 8, 6))  # ordinary Thursday
    assert not is_early_close(date(2027, 12, 23))  # Dec 24 2027 is observed Christmas


def test_market_close_times():
    assert market_close(date(2026, 8, 6)) == REGULAR_CLOSE  # ordinary Thursday
    assert market_close(date(2026, 11, 27)) == EARLY_CLOSE
    assert market_close(date(2026, 8, 8)) is None  # Saturday
    assert market_close(date(2026, 7, 3)) is None  # observed Independence Day
    assert market_close(date(2027, 12, 24)) is None  # observed Christmas beats early close


# ---------------------------------------------------------------------------
# verdict: window boundaries, DST, weekend/holiday reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expect_open"),
    [
        # Thu 2026-08-06 is EDT (UTC-4): window 11:00–20:00 UTC.
        (datetime(2026, 8, 6, 10, 59, tzinfo=UTC), False),  # 06:59 ET
        (datetime(2026, 8, 6, 11, 0, tzinfo=UTC), True),  # 07:00 ET
        (datetime(2026, 8, 6, 19, 59, tzinfo=UTC), True),  # 15:59 ET
        (datetime(2026, 8, 6, 20, 0, tzinfo=UTC), False),  # 16:00 ET close
        # Thu 2026-01-15 is EST (UTC-5): 12:00 UTC = 07:00 ET.
        (datetime(2026, 1, 15, 11, 59, tzinfo=UTC), False),
        (datetime(2026, 1, 15, 12, 0, tzinfo=UTC), True),
        # Fri 2026-11-27 closes early at 13:00 ET (18:00 UTC, EST).
        (datetime(2026, 11, 27, 17, 59, tzinfo=UTC), True),
        (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), False),
    ],
)
def test_verdict_window_boundaries(now, expect_open):
    market_open, reason = market_hours_verdict(now)
    assert market_open is expect_open, reason


def test_verdict_weekend_and_holiday_reasons():
    market_open, reason = market_hours_verdict(datetime(2026, 9, 12, 15, 0, tzinfo=UTC))
    assert not market_open and "weekend" in reason
    market_open, reason = market_hours_verdict(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))
    assert not market_open and "Thanksgiving Day" in reason


def test_verdict_naive_datetime_is_utc():
    market_open, _ = market_hours_verdict(datetime(2026, 8, 6, 14, 0))
    assert market_open


def test_verdict_default_clock_seam():
    # The autouse conftest fixture pins _now_utc inside the window.
    market_open, _ = market_hours_verdict()
    assert market_open


# ---------------------------------------------------------------------------
# submit gate: fail fast on live envs, override flag, dry-run report, exit 7
# ---------------------------------------------------------------------------

SATURDAY = datetime(2026, 8, 8, 14, 0, tzinfo=UTC)


def _pin_clock(monkeypatch, now: datetime) -> None:
    monkeypatch.setattr("ki_ops.kotl.market_hours._now_utc", lambda: now)


def test_live_submit_blocked_outside_hours(tmp_path, monkeypatch):
    """The gate fires before any S3/gRPC work — bogus inputs never load."""
    from ki_ops.kotl.store import KotlStore
    from ki_ops.kotl.submit import MarketClosedError, submit_kelai_shares

    _pin_clock(monkeypatch, SATURDAY)
    with pytest.raises(MarketClosedError, match="MARKET CLOSED.*weekend"):
        submit_kelai_shares(
            KotlStore(tmp_path / "kotl"),
            trade_date=date(2026, 8, 6),
            shares_file=tmp_path / "missing.csv",
            sod_source="flat",
            env="UAT",
        )


def test_live_submit_override_proceeds(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    from ki_ops.kotl.fake_flex import FakeFlexAdapter
    from ki_ops.kotl.flex_live import FlexConfig
    from ki_ops.kotl.store import KotlStore
    from ki_ops.kotl.submit import submit_kelai_shares
    from tests.kotl.fake_flex_sdk import FakeFlexBackend, install_fake_sdk, make_security
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    _pin_clock(monkeypatch, SATURDAY)
    backend = FakeFlexBackend()
    backend.security_master = [make_security("AAPL.US", 15), make_security("MSFT.US", 540)]
    install_fake_sdk(monkeypatch, backend)
    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        submit = submit_kelai_shares(
            KotlStore(tmp_path / "kotl"),
            trade_date=date(2026, 8, 6),
            shares_file=shares,
            ds2_h5=make_ds2_h5(tmp_path / "ds2.h5"),
            cache_dir=tmp_path / "cache",
            sod_source="flat",
            env="UAT",
            adapter=FakeFlexAdapter(),
            flex_config=FlexConfig(endpoint="127.0.0.1:50051", token="tok"),
            allow_outside_market_hours=True,
        )
    assert submit.ok
    assert "MARKET CLOSED — proceeding anyway" in buf.getvalue()


def test_dry_run_reports_but_never_blocks(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    from ki_ops.kotl.store import KotlStore
    from ki_ops.kotl.submit import submit_kelai_shares
    from tests.kotl.fake_flex_sdk import FakeFlexBackend, install_fake_sdk, make_security
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    _pin_clock(monkeypatch, SATURDAY)
    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "127.0.0.1:50051")
    monkeypatch.setenv("KOTL_FLEX_TOKEN", "tok")
    backend = FakeFlexBackend()
    backend.security_master = [make_security("AAPL.US", 15)]
    install_fake_sdk(monkeypatch, backend)
    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        submit = submit_kelai_shares(
            KotlStore(tmp_path / "kotl"),
            trade_date=date(2026, 8, 6),
            shares_file=shares,
            ds2_h5=make_ds2_h5(tmp_path / "ds2.h5"),
            cache_dir=tmp_path / "cache",
            sod_source="flat",
            env="UAT",
            dry_run=True,
        )
    assert not submit.flex_order_ids
    assert "a live submit would block (exit 7)" in buf.getvalue()


def test_fake_env_is_never_gated(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    from ki_ops.kotl.store import KotlStore
    from ki_ops.kotl.submit import submit_kelai_shares
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    _pin_clock(monkeypatch, SATURDAY)
    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\n")
    submit = submit_kelai_shares(
        KotlStore(tmp_path / "kotl"),
        trade_date=date(2026, 8, 6),
        shares_file=shares,
        ds2_h5=make_ds2_h5(tmp_path / "ds2.h5"),
        cache_dir=tmp_path / "cache",
        sod_source="flat",
        env="FAKE",
    )
    assert submit.ok


def test_cli_market_closed_exit_code(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    from ki_ops.kotl.cli import EXIT_MARKET_CLOSED, run_kotl
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    class Args:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _pin_clock(monkeypatch, SATURDAY)
    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "127.0.0.1:50051")
    monkeypatch.setenv("KOTL_FLEX_TOKEN", "tok")
    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_kotl(
            Args(
                kotl_command="submit-kelai",
                trade_date=date(2026, 8, 6),
                shares=str(shares),
                ds2=str(make_ds2_h5(tmp_path / "ds2.h5")),
                sod=None,
                assume_flat_sod=False,
                sod_source="flat",
                flex_env="UAT",
                data_dir=tmp_path / "kotl",
                cache_dir=tmp_path / "cache",
            )
        )
    assert rc == EXIT_MARKET_CLOSED
    out = buf.getvalue()
    assert "SUBMIT BLOCKED" in out
    assert "MARKET CLOSED" in out
