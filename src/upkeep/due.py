"""When a tool is due under `up --auto`.

Day intervals are anchored to `schedule.at`: a daily tool runs once per day at
or after that time, however late the machine wakes. Hour intervals count from
the last attempt. Every attempt counts, whatever its result, so a broken tool
is retried at its next slot rather than every hour.

All datetimes here are naive local time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from upkeep.config import Interval


def _slot(day: date, at: time) -> datetime:
    return datetime.combine(day, at)


def _slot_day(moment: datetime, at: time) -> date:
    """The day whose slot `moment` falls in: before `at`, that is the previous day."""
    return (moment - timedelta(hours=at.hour, minutes=at.minute)).date()


def next_due(interval: Interval, last: datetime | None, now: datetime, at: time) -> datetime:
    """When the tool is next due; a time at or before `now` means it is due."""
    if interval.unit == "h":
        return now if last is None else last + timedelta(hours=interval.count)
    if last is None:
        return _slot(now.date(), at)
    return _slot(_slot_day(last, at) + timedelta(days=interval.count), at)


def is_due(interval: Interval, last: datetime | None, now: datetime, at: time) -> bool:
    return next_due(interval, last, now, at) <= now
