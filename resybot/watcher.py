"""
Cancellation watcher — STUB for a later phase.

Plan: a slow loop (every 2–5 min, randomized) over every target whose drop
has already passed. Calls find() for each date, runs the ranker with the
fallback window, books on a hit, then removes that target from the loop.

Deliberately not implemented yet: this is the part most likely to get an
account flagged if polling is too aggressive, so get the drop sniper
working and trusted first.
"""
from __future__ import annotations


def run(*args, **kwargs):
    raise SystemExit("Cancellation watcher isn't built yet — see watcher.py for the plan.")
