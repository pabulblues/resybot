"""
The drop-time loop: wait → warm up → poll → rank → book → stop.

One Sniper instance handles one Drop (one venue, one release moment,
possibly several dates). The CLI creates one per upcoming drop.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import history
from .config import Config, Target
from .notify import Notifier
from .ranker import choose
from .release import Drop
from .resy import Booking, BookingRefused, ResyClient, ResyError, Slot

log = logging.getLogger("resybot.sniper")


class Sniper:
    def __init__(self, cfg: Config, target: Target, drop: Drop, client: ResyClient, notifier: Notifier,
                 cancel: threading.Event | None = None):
        self.cfg = cfg
        self.cancel = cancel or threading.Event()   # set() to stand down (e.g. target disarmed)
        self.target = target
        self.drop = drop
        self.client = client
        self.notifier = notifier
        self.tz = ZoneInfo(cfg.timezone)

    # ------------------------------------------------------------------

    def wait_for_drop(self) -> None:
        """Coarse sleep until T-5s, then spin on the clock for precision."""
        while not self.cancel.is_set():
            remaining = (self.drop.at - datetime.now(self.tz)).total_seconds()
            if remaining <= 0:
                return
            if remaining > 5:
                if remaining > 60:
                    log.info("%s: drop in %s", self.target.name, timedelta(seconds=int(remaining)))
                self.cancel.wait(min(remaining - 5, 30))
                if 3 <= remaining - 5 <= 30:
                    self._warm_up()
            else:
                _time.sleep(0.002)  # 2 ms spin: precise without pinning the CPU

    def _warm_up(self) -> None:
        """One cheap request so the TLS connection is hot at the drop moment."""
        try:
            self.client.find(self.target.venue_id, self.drop.dates[0], self.target.party_size)
            log.info("%s: connection warmed", self.target.name)
        except ResyError as e:
            log.warning("%s: warm-up failed (continuing): %s", self.target.name, e)

    # ------------------------------------------------------------------

    def run(self) -> Booking | None:
        t = self.target
        log.info("%s: armed for %s", t.name, self.drop)
        self.wait_for_drop()
        if self.cancel.is_set():
            log.info("%s: stood down before the drop", t.name)
            return None
        log.info("%s: GO", t.name)

        t0 = _time.perf_counter()
        deadline = t0 + self.cfg.poll_duration_s
        interval = self.cfg.poll_interval_ms / 1000
        refused: set[Slot] = set()     # slots we tried and were told no (deposit, etc.)
        last_pinged: Slot | None = None  # manual mode: what we last told you about
        self.pinged = False
        self.slot: str | None = None      # what it found / booked, for the menus
        polls = 0

        while _time.perf_counter() < deadline and not self.cancel.is_set():
            elapsed = _time.perf_counter() - t0
            slots: list[Slot] = []
            for day in self.drop.dates:
                try:
                    slots += self.client.find(t.venue_id, day, t.party_size)
                except ResyError as e:
                    log.debug("find failed: %s", e)
            polls += 1

            pick = choose([s for s in slots if s not in refused], t, elapsed)
            if pick is None:
                if polls % 20 == 0:
                    log.info("%s: %d polls, %d slots seen, none in window yet", t.name, polls, len(slots))
                _time.sleep(interval)
                continue

            if t.book_mode == "manual":
                # Captcha venue: we can't finish the booking, but we can put the
                # exact slot in your hand the moment it exists. Keep watching
                # and re-ping only when the best available slot changes.
                if pick != last_pinged:
                    url = self._page_url(pick)
                    log.info("%s: FOUND %s (%.1fs after drop) → %s", t.name, pick.label, elapsed, url)
                    self.notifier.found(t.name, pick.label, url)
                    if last_pinged is None:
                        self.slot = pick.label
                    if last_pinged is None and not self.client.dry_run:
                        history.record("alert", t.name, slot=pick.label, party=t.party_size, url=url)
                    last_pinged = pick
                _time.sleep(interval)
                continue

            log.info("%s: trying %s (%.1fs after drop, poll #%d)", t.name, pick.label, elapsed, polls)
            try:
                det = self.client.details(pick)
                booking = self.client.book(pick, det, max_deposit_usd=t.max_deposit_usd)
            except BookingRefused as e:
                log.warning("%s: refused — %s", t.name, e)
                refused.add(pick)
                continue
            except ResyError as e:
                # Most likely someone beat us to this exact slot. Re-poll immediately.
                log.warning("%s: book failed (%s) — re-polling", t.name, e)
                refused.add(pick)
                continue

            log.info("%s: BOOKED %s → %s", t.name, pick.label, booking.reservation_id)
            self.slot = pick.label
            if not self.client.dry_run:
                history.record("booked", t.name, slot=pick.label, reservation=booking.reservation_id,
                               party=t.party_size, url=self._page_url(pick))
            self.notifier.booked(t.name, pick.label, booking.reservation_id)
            return booking      # <- stop. Never book twice.

        if t.book_mode == "manual" and last_pinged is not None:
            self.pinged = True
            log.info("%s: manual mode — stopped watching after %ds; last slot sent: %s",
                     t.name, self.cfg.poll_duration_s, last_pinged.label)
            return None
        msg = f"nothing in window after {self.cfg.poll_duration_s}s ({polls} polls)"
        log.warning("%s: %s", t.name, msg)
        if not self.client.dry_run and not self.cancel.is_set():
            history.record("missed", t.name, party=t.party_size,
                           note=", ".join(f"{d:%a %b %-d}" for d in self.drop.dates))
        self.notifier.failed(t.name, msg)
        return None

    def _page_url(self, slot: Slot) -> str:
        t = self.target
        slug = t.url_slug or self._slug_cache()
        return (f"https://resy.com/cities/{t.city}/venues/{slug}"
                f"?date={slot.day.isoformat()}&seats={t.party_size}")

    def _slug_cache(self) -> str:
        if not hasattr(self, "_slug"):
            try:
                self._slug = self.client.venue_info(self.target.venue_id, self.drop.dates[0],
                                                    self.target.party_size).url_slug
            except ResyError:
                self._slug = str(self.target.venue_id)
        return self._slug
