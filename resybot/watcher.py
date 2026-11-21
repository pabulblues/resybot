"""
Booking an already-released date ("cancellations").

The scheduler no longer polls for cancellations. It signs you up for Resy
Notify (resynotify.py), and when Resy's "a table opened" email arrives
(mailwatch.py) it calls `check()` a few times in quick succession. If a table
in your window is there, it books it (or, for captcha venues, opens the page
and alerts you).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import history
from .config import Config, Target
from .notify import Notifier
from .ranker import choose
from .release import ReleaseRule
from .resy import Booking, BookingRefused, ResyClient, ResyError, Slot

log = logging.getLogger("resybot.watcher")

DAY_EVERY_S = 120
NIGHT_EVERY_S = 600
NIGHT = (time(1, 0), time(7, 0))
MIN_LEAD = timedelta(minutes=45)   # don't grab a table you couldn't get to


@dataclass
class WatchState:
    checks: int = 0
    last_check: datetime | None = None
    next_check: float = 0.0               # epoch seconds
    last_alert: str | None = None         # manual mode: slot we last told you about
    refused: set = field(default_factory=set)
    result: str = "watching"


def watch_dates(t: Target, cfg: Config, now: datetime) -> list[date]:
    """Dates of `t` that are already released and still ahead of us."""
    rule = ReleaseRule(t.release, cfg.timezone)
    out = []
    for d in t.dates:
        if d < now.date():
            continue
        at = rule._drop_moment(d, now) if rule.kind != "now" else None
        if at is None or at <= now:
            out.append(d)
    return out


def next_delay(now: datetime, every_s: int = DAY_EVERY_S) -> float:
    base = NIGHT_EVERY_S if NIGHT[0] <= now.time() < NIGHT[1] else max(60, every_s)
    return base * random.uniform(0.7, 1.3)


def check(c: ResyClient, cfg: Config, t: Target, days: list[date], st: WatchState,
          notifier: Notifier, page_url) -> Booking | None:
    """One pass over the watched dates. Returns a Booking if it booked one."""
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    slots: list[Slot] = []
    for d in days:
        try:
            slots += c.find(t.venue_id, d, t.party_size)
        except ResyError as e:
            log.debug("%s: watch find failed: %s", t.name, e)
    st.checks += 1
    st.last_check = now
    # Only tables far enough in the future to actually make it to.
    slots = [s for s in slots
             if datetime.combine(s.day, s.start, tzinfo=tz) - now >= MIN_LEAD and s not in st.refused]
    # A cancellation is a second chance: accept the looser fallback window right away.
    pick = choose(slots, t, seconds_since_drop=float("inf"))
    if pick is None:
        if st.checks % 15 == 0:
            log.info("%s: still watching for a cancellation (%d checks)", t.name, st.checks)
        return None

    if t.book_mode == "manual":
        if st.last_alert != pick.label:
            log.info("%s: cancellation! %s is open", t.name, pick.label)
            notifier.found(t.name, pick.label, page_url(pick))
            if not c.dry_run:
                history.record("alert", t.name, slot=pick.label, party=t.party_size, url=page_url(pick),
                               note="cancellation")
            st.last_alert = pick.label
        return None

    log.info("%s: cancellation! trying %s", t.name, pick.label)
    try:
        det = c.details(pick)
        booking = c.book(pick, det, max_deposit_usd=t.max_deposit_usd)
    except BookingRefused as e:
        log.warning("%s: refused: %s", t.name, e)
        st.refused.add(pick)
        return None
    except ResyError as e:
        log.warning("%s: someone beat us to %s (%s)", t.name, pick.label, e)
        st.refused.add(pick)
        return None
    log.info("%s: BOOKED from a cancellation: %s → %s", t.name, pick.label, booking.reservation_id)
    if not c.dry_run:
        history.record("booked", t.name, slot=pick.label, reservation=booking.reservation_id,
                       party=t.party_size, url=page_url(pick), note="cancellation")
    notifier.booked(t.name, pick.label, booking.reservation_id)
    return booking
