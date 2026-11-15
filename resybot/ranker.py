"""
Pick the best slot from whatever Resy returned.

Pure logic: takes a list of Slots and a Target, returns the one to book
(or None). No network, no side effects — runs in microseconds, so it
costs nothing in the race.

Scoring (lower is better):
  1. Must be inside the active window (preferred, or fallback after N s).
  2. Must match an allowed seat type (if any were listed).
  3. Then: seat-type preference order, then closeness to the middle of
     the *preferred* window.
"""
from __future__ import annotations

from datetime import time

from .config import Target
from .resy import Slot


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _in_window(t: time, window: tuple[time, time]) -> bool:
    return window[0] <= t <= window[1]


def _seat_ok(seat: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    return any(a.lower() in seat.lower() for a in allowed)


def _seat_rank(seat: str, allowed: list[str]) -> int:
    for i, a in enumerate(allowed):
        if a.lower() in seat.lower():
            return i
    return len(allowed)


def choose(slots: list[Slot], target: Target, seconds_since_drop: float = 0.0) -> Slot | None:
    window = target.window_for(seconds_since_drop)
    pref_mid = (_minutes(target.time_window[0]) + _minutes(target.time_window[1])) / 2

    candidates = [
        s for s in slots
        if s.day in target.dates
        and _in_window(s.start, window)
        and _seat_ok(s.seat_type, target.seat_types)
        and s.deposit_usd <= target.max_deposit_usd   # cancellation fees don't count
    ]
    if not candidates:
        return None

    def score(s: Slot):
        return (
            _seat_rank(s.seat_type, target.seat_types),      # preferred seating first
            abs(_minutes(s.start) - pref_mid),               # then closest to ideal time
            target.dates.index(s.day),                       # then earlier-listed date
        )

    return min(candidates, key=score)
