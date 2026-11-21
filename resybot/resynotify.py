"""
Resy Notify: Resy's own "tell me when a table opens" signup.

Instead of checking Resy over and over for a cancellation, the bot signs you
up for Notify (the bell on a sold-out date in the Resy app). Resy then emails
you the moment a table in your window opens; mailwatch.py sees that email and
the scheduler books it.

The request shapes are the ones resy.com itself uses:
    sign up   POST   /3/notify   struct_data={"notifies": [{venue_id, day, time_preferred_start,
                                               time_preferred_end, num_seats, service_type_id}]}
    list      GET    /3/notify
    cancel    DELETE /2/notify?venue_id=&day=&num_seats=&service_type_id=
"""
from __future__ import annotations

import json
import logging
from datetime import date

from .resy import ResyClient, ResyError

log = logging.getLogger("resybot.resynotify")

LUNCH, DINNER = 1, 2


def _round(hhmm: str, up: bool) -> str:
    """Notify works in 30-minute steps: 18:15 → 18:00 (start) / 18:30 (end)."""
    h, m = map(int, hhmm[:5].split(":"))
    mins = h * 60 + m
    mins = (-(-mins // 30) * 30) if up else (mins // 30 * 30)
    mins = min(mins, 23 * 60 + 30)
    return f"{mins // 60:02d}:{mins % 60:02d}:00"


def service_types(start: str, end: str) -> list[int]:
    if end <= "16:00":
        return [LUNCH]
    if start >= "16:00":
        return [DINNER]
    return [LUNCH, DINNER]


def specs_for(venue_id: int, day: date, window: tuple[str, str], party: int) -> list[dict]:
    start, end = _round(window[0], up=False), _round(window[1], up=True)
    return [{"venue_id": venue_id, "day": day.isoformat(), "time_preferred_start": start,
             "time_preferred_end": end, "num_seats": party, "service_type_id": st}
            for st in service_types(start[:5], end[:5])]


def key(spec: dict) -> tuple:
    return (int(spec["venue_id"]), spec["day"], int(spec.get("num_seats") or spec.get("party_size")),
            int(spec["service_type_id"]))


def active(c: ResyClient) -> dict[tuple, dict]:
    """Your current Notify signups, keyed by (venue, day, party, service type)."""
    body = c._request("GET", "/3/notify", retries=1)
    out = {}
    for n in body.get("notify") or []:
        s = n.get("specs") or {}
        if s.get("venue_id") and s.get("day"):
            out[key(s)] = s
    return out


def signup(c: ResyClient, spec: dict) -> None:
    c._request("POST", "/3/notify", data={"struct_data": json.dumps({"notifies": [spec]})}, retries=1)
    log.info("Resy Notify on: venue %s %s %s–%s party %s", spec["venue_id"], spec["day"],
             spec["time_preferred_start"][:5], spec["time_preferred_end"][:5], spec["num_seats"])


def cancel(c: ResyClient, venue_id: int, day: str, party: int, service_type_id: int) -> None:
    c._request("DELETE", "/2/notify", params={"venue_id": venue_id, "day": day, "num_seats": party,
                                              "service_type_id": service_type_id}, retries=1)
    log.info("Resy Notify off: venue %s %s party %s", venue_id, day, party)


def ensure(c: ResyClient, venue_id: int, days: list[date], window, party: int,
           have: dict[tuple, dict] | None = None) -> list[str]:
    """Make sure a Notify exists for each date. Returns the dates newly signed up."""
    if have is None:
        have = active(c)
    added = []
    for d in days:
        for spec in specs_for(venue_id, d, window, party):
            k = key(spec)
            cur = have.get(k)
            if cur and cur.get("time_preferred_start") == spec["time_preferred_start"] \
                    and cur.get("time_preferred_end") == spec["time_preferred_end"]:
                continue
            signup(c, spec)          # same key with a different window: Resy updates it in place
            have[k] = spec
            added.append(d.isoformat())
    return sorted(set(added))


def cancel_for(c: ResyClient, venue_id: int, days: list[date], party: int) -> None:
    """Best effort: drop the Notify signups for these dates (after booking / turning off)."""
    try:
        have = active(c)
    except ResyError:
        return
    for k in list(have):
        v, day, p, st = k
        if v == venue_id and day in {d.isoformat() for d in days} and p == party:
            try:
                cancel(c, v, day, p, st)
            except ResyError as e:
                log.warning("couldn't cancel Notify for %s %s: %s", venue_id, day, e)
