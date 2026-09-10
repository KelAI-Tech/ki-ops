"""NYSE market-hours gate for live KOTL submits (rules-based, stdlib-only).

Live orders may only go out on NYSE trading days, from :data:`OPEN_GATE`
(07:00 America/New_York — pre-open staging for the VWAP schedule) up to the
market close (16:00, or 13:00 on early-close days). Outside that window a
live submit blocks (:class:`~ki_ops.kotl.submit.MarketClosedError`, CLI
exit 7) unless ``--allow-outside-market-hours`` is passed deliberately.

The trading calendar is computed from the published NYSE rules rather than a
static list or a calendar dependency, so it never goes stale:

- weekends;
- the ten NYSE holidays (New Year's Day, MLK Day, Washington's Birthday,
  Good Friday, Memorial Day, Juneteenth from 2022, Independence Day,
  Labor Day, Thanksgiving, Christmas), with Saturday holidays observed the
  Friday before and Sunday holidays the Monday after — except New Year's Day
  on a Saturday, which NYSE does not observe (the preceding Friday is a
  year-end trading day, e.g. 2021-12-31);
- 13:00 early closes: July 3 when it falls Mon–Thu, the day after
  Thanksgiving, and December 24 when it falls Mon–Thu.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
OPEN_GATE = time(7, 0)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

_MONDAY, _FRIDAY, _SATURDAY, _SUNDAY = 0, 4, 5, 6


def _now_utc() -> datetime:
    """Clock seam (tests pin it; production uses the real clock)."""
    return datetime.now(timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Easter Sunday (anonymous Gregorian computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date | None:
    """NYSE observance shift: Sat → Friday before, Sun → Monday after.

    New Year's Day on a Saturday is NOT observed — the shifted Friday would
    land on the prior year's Dec 31, which NYSE keeps as a trading day.
    """
    if d.weekday() == _SATURDAY:
        if d.month == 1 and d.day == 1:
            return None
        return d - timedelta(days=1)
    if d.weekday() == _SUNDAY:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict[date, str]:
    """Full-day NYSE closures for *year* (observed date → holiday name)."""
    fixed = [
        (date(year, 1, 1), "New Year's Day"),
        (date(year, 6, 19), "Juneteenth") if year >= 2022 else None,
        (date(year, 7, 4), "Independence Day"),
        (date(year, 12, 25), "Christmas Day"),
    ]
    floating = [
        (_nth_weekday(year, 1, _MONDAY, 3), "Martin Luther King Jr. Day"),
        (_nth_weekday(year, 2, _MONDAY, 3), "Washington's Birthday"),
        (_easter(year) - timedelta(days=2), "Good Friday"),
        (_last_weekday(year, 5, _MONDAY), "Memorial Day"),
        (_nth_weekday(year, 9, _MONDAY, 1), "Labor Day"),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving Day"),  # 4th Thursday
    ]
    holidays: dict[date, str] = {}
    for entry in fixed:
        if entry is None:
            continue
        observed = _observed(entry[0])
        if observed is not None:
            holidays[observed] = entry[1]
    holidays.update(floating)
    return holidays


def is_early_close(d: date) -> bool:
    """13:00 ET close: Jul 3 (Mon–Thu), day after Thanksgiving, Dec 24 (Mon–Thu)."""
    if d.month == 7 and d.day == 3 and d.weekday() <= 3:
        return True
    if d.month == 12 and d.day == 24 and d.weekday() <= 3:
        return True
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)
    return d == thanksgiving + timedelta(days=1)


def market_close(d: date) -> time | None:
    """Close time for *d* in ET, or ``None`` when the market is shut all day."""
    if d.weekday() >= _SATURDAY:
        return None
    if d in nyse_holidays(d.year):
        return None
    return EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE


def market_hours_verdict(now: datetime | None = None) -> tuple[bool, str]:
    """``(submits_allowed, reason)`` for the instant *now* (default: real clock).

    Naive datetimes are taken as UTC (the convention across the KOTL ledger).
    """
    if now is None:
        now = _now_utc()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_et = now.astimezone(EASTERN)
    d = now_et.date()
    stamp = now_et.strftime("%Y-%m-%d %H:%M ET")

    if d.weekday() >= _SATURDAY:
        return False, f"{stamp} is a weekend — NYSE is closed"
    holiday = nyse_holidays(d.year).get(d)
    if holiday is not None:
        return False, f"{stamp} is an NYSE holiday ({holiday})"
    close = market_close(d)
    assert close is not None  # weekend/holiday handled above
    window = f"{OPEN_GATE:%H:%M}–{close:%H:%M} ET"
    if now_et.time() < OPEN_GATE:
        return False, f"{stamp} is before the submit window ({window})"
    if now_et.time() >= close:
        return False, f"{stamp} is after the market close ({window})"
    return True, f"{stamp} is within the submit window ({window})"
