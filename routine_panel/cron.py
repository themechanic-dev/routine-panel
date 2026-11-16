"""Cron expressions (five fields: minute hour day-of-month month day-of-week), as the Claude app's routines use them:
when one fires next, when it fired last, and how to say it in words.

Times are the computer's local time, as cron's are. When both day fields are given (neither is "*"), a day matches
either of them, as in Vixie cron."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

_MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DAYS = {name: i for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_DAY_NAMES = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"]
_SEARCH_DAYS = 366 * 5  # far enough for any expression that ever matches (29 February included)


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset
    hours: frozenset
    days: frozenset  # of the month
    months: frozenset
    weekdays: frozenset  # 0 = Sunday
    any_day: bool  # day of the month is "*"
    any_weekday: bool  # day of the week is "*"

    # --- matching ----------------------------------------------------------------------------------------------
    def day_matches(self, day: _dt.date) -> bool:
        if day.month not in self.months:
            return False
        weekday = (day.weekday() + 1) % 7  # Python: Monday = 0; cron: Sunday = 0
        in_month = day.day in self.days
        in_week = weekday in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return in_week
        if self.any_weekday:
            return in_month
        return in_month or in_week

    def next_after(self, t: float) -> float | None:
        """The first time after `t` (epoch seconds) at which this fires; None if it never does."""
        start = _dt.datetime.fromtimestamp(t).replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        day = start.date()
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        for offset in range(_SEARCH_DAYS):
            current = day + _dt.timedelta(days=offset)
            if not self.day_matches(current):
                continue
            for hour in hours:
                for minute in minutes:
                    moment = _dt.datetime(current.year, current.month, current.day, hour, minute)
                    if moment >= start:
                        return moment.timestamp()
        return None

    def last_at_or_before(self, t: float) -> float | None:
        """The last time at or before `t` at which this fired; None if never within five years."""
        end = _dt.datetime.fromtimestamp(t).replace(second=0, microsecond=0)
        day = end.date()
        hours = sorted(self.hours, reverse=True)
        minutes = sorted(self.minutes, reverse=True)
        for offset in range(_SEARCH_DAYS):
            current = day - _dt.timedelta(days=offset)
            if not self.day_matches(current):
                continue
            for hour in hours:
                for minute in minutes:
                    moment = _dt.datetime(current.year, current.month, current.day, hour, minute)
                    if moment <= end:
                        return moment.timestamp()
        return None

    # --- words -------------------------------------------------------------------------------------------------
    def describe(self) -> str:
        """The schedule in a few words ("daily 05:00", "hourly at :17", "every 15 minutes"); the expression itself
        when it is not one of the common shapes."""
        fields = self.text.split()
        minute, hour, day, month, weekday = fields
        all_days = day == "*" and month == "*" and weekday == "*"
        if all_days and hour == "*":
            if minute == "*":
                return "every minute"
            if minute.startswith("*/") and minute[2:].isdigit():
                return f"every {int(minute[2:])} minutes"
            if minute.isdigit():
                return f"hourly at :{int(minute):02d}"
        if minute.isdigit() and hour.isdigit():
            at = f"{int(hour):02d}:{int(minute):02d}"
            if all_days:
                return f"daily {at}"
            if day == "*" and month == "*" and self.weekdays and not self.any_weekday:
                if self.weekdays == frozenset({1, 2, 3, 4, 5}):
                    return f"weekdays {at}"
                if len(self.weekdays) == 1:
                    return f"{_DAY_NAMES[next(iter(self.weekdays))]} {at}"
                names = ", ".join(_DAY_NAMES[d][:3] for d in sorted(self.weekdays))
                return f"{names} {at}"
            if day.isdigit() and month == "*" and weekday == "*":
                return f"monthly on day {int(day)}, {at}"
        if minute.isdigit() and hour.startswith("*/") and hour[2:].isdigit() and all_days:
            return f"every {int(hour[2:])} hours at :{int(minute):02d}"
        return f"cron {self.text}"


def parse(text: str) -> Cron:
    fields = (text or "").split()
    if len(fields) != 5:
        raise CronError(f"a cron expression has 5 fields: {text!r}")
    minute, hour, day, month, weekday = fields
    weekdays = _field(weekday, 0, 7, _DAYS)
    if 7 in weekdays:  # 7 is Sunday too
        weekdays = (weekdays - {7}) | {0}
    return Cron(text=" ".join(fields), minutes=_field(minute, 0, 59), hours=_field(hour, 0, 23),
                days=_field(day, 1, 31), months=_field(month, 1, 12, _MONTHS), weekdays=frozenset(weekdays),
                any_day=day == "*", any_weekday=weekday == "*")


def _field(text: str, low: int, high: int, names: dict | None = None) -> frozenset:
    values: set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"bad step in {text!r}")
            step = int(step_text)
        if part == "*":
            first, last = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            first, last = _value(a, names), _value(b, names)
        else:
            first = _value(part, names)
            last = high if step > 1 else first
        if not (low <= first <= high and low <= last <= high) or first > last:
            raise CronError(f"out of range in {text!r}")
        values.update(range(first, last + 1, step))
    if not values:
        raise CronError(f"empty field {text!r}")
    return frozenset(values)


def _value(text: str, names: dict | None) -> int:
    if text.isdigit():
        return int(text)
    if names and text in names:
        return names[text]
    raise CronError(f"not a number: {text!r}")
