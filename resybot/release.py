"""
Turns a human-readable release rule into concrete drop timestamps.

Supported rule formats (case-insensitive):

    "30 days ahead at 10:00"      most common on Resy: the table for date D
                                  becomes bookable at (D - 30 days) 10:00
    "14 days ahead at 00:00"      midnight drops
    "every monday at 10:00"       weekly batch release: a whole week opens
                                  at once on that weekday
    "monthly on the 15th at 12:00"
                                  monthly batch: the whole NEXT month opens
                                  on that day of the month at that time
    "now"                         no scheduled drop; only the cancellation
                                  watcher makes sense for this target

Times are read in the config's timezone (default America/New_York).
Pure logic, no network. Easy to unit test and easy to port.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_DAYS_AHEAD = re.compile(r"^\s*(\d+)\s*days?\s*ahead\s*at\s*(\d{1,2}):(\d{2})\s*$", re.I)
_WEEKLY = re.compile(r"^\s*every\s+([a-z]+)\s*at\s*(\d{1,2}):(\d{2})\s*$", re.I)
_MONTHLY = re.compile(r"^\s*monthly\s+on\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s*at\s*(\d{1,2}):(\d{2})\s*$", re.I)
_NOW = re.compile(r"^\s*now\s*$", re.I)


@dataclass(frozen=True)
class Drop:
    """One moment at which one or more target dates become bookable."""
    at: datetime            # timezone-aware
    dates: tuple[date, ...]  # which reservation dates open at this moment

    def __str__(self) -> str:
        ds = ", ".join(d.isoformat() for d in self.dates)
        return f"{self.at:%a %Y-%m-%d %H:%M %Z} → opens {ds}"


def infer_rule(day: date, released_at: int, tz: str = "America/New_York") -> str:
    """
    Resy stamps each slot with market.date.on = when it was released.
    Turn that into a rule string you can paste into targets.yaml.
    One data point, so treat it as a strong hint, not proof.
    """
    at = datetime.fromtimestamp(released_at, ZoneInfo(tz))
    days = (day - at.date()).days
    return f"{days} days ahead at {at:%H:%M}"


class ReleaseRule:
    def __init__(self, rule: str, tz: str = "America/New_York"):
        self.raw = rule
        self.tz = ZoneInfo(tz)
        self.kind: str
        self.days_ahead: int | None = None
        self.weekday: int | None = None
        self.month_day: int | None = None
        self.at: time | None = None

        if _NOW.match(rule):
            self.kind = "now"
        elif m := _DAYS_AHEAD.match(rule):
            self.kind = "days_ahead"
            self.days_ahead = int(m.group(1))
            self.at = time(int(m.group(2)), int(m.group(3)))
        elif m := _WEEKLY.match(rule):
            wd = m.group(1).lower()
            if wd not in WEEKDAYS:
                raise ValueError(f"Unknown weekday in release rule: {rule!r}")
            self.kind = "weekly"
            self.weekday = WEEKDAYS.index(wd)
            self.at = time(int(m.group(2)), int(m.group(3)))
        elif m := _MONTHLY.match(rule):
            self.kind = "monthly"
            self.month_day = int(m.group(1))
            if not 1 <= self.month_day <= 28:
                raise ValueError("Monthly release day must be 1–28.")
            self.at = time(int(m.group(2)), int(m.group(3)))
        else:
            raise ValueError(
                f"Can't parse release rule {rule!r}. "
                "Use '30 days ahead at 10:00', 'every monday at 10:00', "
                "'monthly on the 15th at 12:00', or 'now'."
            )

    # ------------------------------------------------------------------

    def drops_for(self, dates: list[date], now: datetime | None = None) -> list[Drop]:
        """
        Given the reservation dates you want, return the future drop moments
        (sorted, soonest first). Dates whose drop already passed are skipped;
        for those, the cancellation watcher is your only option.
        """
        now = now or datetime.now(self.tz)
        if self.kind == "now":
            return []

        drops: dict[datetime, list[date]] = {}
        for d in dates:
            at = self._drop_moment(d, now)
            if at is None or at <= now:
                continue
            drops.setdefault(at, []).append(d)

        return sorted(
            (Drop(at=at, dates=tuple(sorted(ds))) for at, ds in drops.items()),
            key=lambda x: x.at,
        )

    def _drop_moment(self, d: date, now: datetime) -> datetime | None:
        assert self.at is not None
        if self.kind == "days_ahead":
            return datetime.combine(d - timedelta(days=self.days_ahead), self.at, tzinfo=self.tz)

        if self.kind == "weekly":
            # The batch covering date d is the LAST occurrence of `weekday` at
            # or before d. If that's in the past, the date is already open.
            delta = (d.weekday() - self.weekday) % 7
            batch_day = d - timedelta(days=delta)
            return datetime.combine(batch_day, self.at, tzinfo=self.tz)

        if self.kind == "monthly":
            # Date d opens on `month_day` of the month BEFORE d's month.
            first = d.replace(day=1)
            prev = (first - timedelta(days=1)).replace(day=self.month_day)
            return datetime.combine(prev, self.at, tzinfo=self.tz)

        return None

    def __repr__(self) -> str:
        return f"ReleaseRule({self.raw!r})"
