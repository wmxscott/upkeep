from __future__ import annotations

from datetime import datetime, time

from upkeep.config import DAILY, Interval
from upkeep.due import is_due, next_due

AT = time(8, 0)
WEEKLY = Interval(7, "d")


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute)


def test_never_run_waits_for_at():
    assert not is_due(DAILY, None, dt(10, 7, 59), AT)
    assert is_due(DAILY, None, dt(10, 8, 0), AT)
    assert is_due(DAILY, None, dt(10, 23), AT)


def test_daily_once_per_day():
    last = dt(10, 8, 5)
    assert not is_due(DAILY, last, dt(10, 20), AT)
    assert not is_due(DAILY, last, dt(11, 7, 59), AT)
    assert is_due(DAILY, last, dt(11, 8), AT)
    assert next_due(DAILY, last, dt(10, 20), AT) == dt(11, 8)


def test_daily_catches_up_after_sleep_without_drifting():
    # Asleep at 08:00; the first tick after waking at 14:10 runs it.
    last = dt(10, 8, 1)
    assert is_due(DAILY, last, dt(11, 14, 10), AT)
    # The late run doesn't push the next day's run to 14:10.
    assert next_due(DAILY, dt(11, 14, 10), dt(11, 15), AT) == dt(12, 8)


def test_days_off_catch_up_once():
    assert is_due(DAILY, dt(1, 8), dt(9, 9), AT)
    assert next_due(DAILY, dt(9, 9), dt(9, 10), AT) == dt(10, 8)


def test_run_before_at_counts_for_the_previous_day():
    last = dt(10, 7)  # a manual run before 08:00
    assert is_due(DAILY, last, dt(10, 8), AT)


def test_weekly():
    last = dt(2, 9)
    assert not is_due(WEEKLY, last, dt(8, 23), AT)
    assert not is_due(WEEKLY, last, dt(9, 7), AT)
    assert is_due(WEEKLY, last, dt(9, 8), AT)
    assert is_due(WEEKLY, last, dt(20, 12), AT)


def test_hours_ignore_at():
    six = Interval(6, "h")
    assert is_due(six, None, dt(10, 3), AT)
    assert not is_due(six, dt(10, 3), dt(10, 8, 59), AT)
    assert is_due(six, dt(10, 3), dt(10, 9), AT)
