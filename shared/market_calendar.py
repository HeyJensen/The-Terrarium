"""US equity regular-hours calendar (NYSE/Nasdaq), America/New_York.

Holiday and early-close dates are hard-coded for 2026-2027 from the NYSE
published schedule. Re-check https://www.nyse.com/markets/hours-calendars
each year and extend the tables before they run out; is_trading_day raises
past the last covered year rather than guessing.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
COVERED_YEARS = {2026, 2027}


def is_trading_day(d: date) -> bool:
    if d.year not in COVERED_YEARS:
        raise RuntimeError(f"Market calendar has no holiday data for {d.year}; update shared/market_calendar.py")
    return d.weekday() < 5 and d not in HOLIDAYS


def session_close(d: date) -> time:
    return EARLY_CLOSE if d in EARLY_CLOSES else CLOSE


def to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(ET)


def is_regular_hours(ts: datetime) -> bool:
    local = to_et(ts)
    d = local.date()
    return is_trading_day(d) and OPEN <= local.time() < session_close(d)


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_between(start: date, end: date) -> int:
    """Number of trading days after `start` up to and including `end`."""
    n, d = 0, start
    while d < end:
        d = next_trading_day(d)
        if d <= end:
            n += 1
    return n
