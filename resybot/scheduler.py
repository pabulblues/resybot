"""
The always-on scheduler behind `resybot serve`, the menus' live screen,
and the Telegram bot.

Unlike `run` (arm everything once, exit when done), this loops forever:
  * re-reads targets.yaml every few seconds, so edits from the menus or
    Telegram take effect without a restart;
  * starts a Sniper ~10 minutes before each armed drop (it then waits
    for the exact second itself);
  * stands a sniper down if its target is disarmed or deleted;
  * disarms a target once it books, so it never books twice;
  * checks the Resy login every few hours and warns you before it bites;
  * for dates that are already out: signs you up for Resy Notify and books
    the moment Resy emails you that a table opened (no repeated checking).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import mailwatch, resynotify, service, store
from .config import Config, Target
from .notify import Notifier
from .release import Drop, ReleaseRule
from .resy import ResyError
from .sniper import Sniper
from . import watcher

log = logging.getLogger("resybot.scheduler")

ARM_LEAD = timedelta(minutes=10)     # start the sniper thread this early
LOGIN_CHECK_EVERY = 6 * 3600          # seconds


@dataclass
class Run:
    target: str
    drop: Drop
    live: bool
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    slot: str | None = None          # the table it found / booked
    state: str = "armed"             # armed → running → booked | missed | found | stopped | error
    detail: str = ""
    adhoc: bool = False              # started by run_now, not by the schedule


class Scheduler:
    def __init__(self, notifier: Notifier, config_path: Path | str = "targets.yaml",
                 force_dry_run: bool = False):
        self.notifier = notifier
        self.config_path = Path(config_path)
        self.force_dry_run = force_dry_run
        self.runs: dict[tuple[str, datetime], Run] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_login_check = 0.0
        self.login_ok: bool | None = None
        self.login_msg = ""
        self.watches: dict[str, watcher.WatchState] = {}
        self.mail: mailwatch.MailWatcher | None = None
        self._notify_have: dict | None = None      # Resy Notify signups we know about
        self._notify_synced = 0.0
        self._notified: dict[str, tuple] = {}       # target name → what we signed up for
        self._grabbing: set[str] = set()

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.loop, name="scheduler", daemon=True)
        th.start()
        self.mail = mailwatch.MailWatcher(self.on_resy_email)
        self.mail.start()
        return th

    def stop(self) -> None:
        self._stop.set()
        if self.mail:
            self.mail.stop()
        with self._lock:
            for r in self.runs.values():
                r.cancel.set()

    def loop(self) -> None:
        log.info("scheduler started%s", " (dry runs only)" if self.force_dry_run else "")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:   # a bad edit to targets.yaml must not kill the server
                log.error("scheduler tick failed: %s", e)
            self._stop.wait(5)

    # ---- one pass --------------------------------------------------------------

    def tick(self) -> None:
        cfg = store.load_config(self.config_path)
        now = datetime.now(ZoneInfo(cfg.timezone if cfg else "America/New_York"))

        if _time.time() - self._last_login_check > LOGIN_CHECK_EVERY:
            self.check_login()

        wanted: dict[tuple[str, datetime], tuple[Target, Drop]] = {}
        if cfg:
            for t in cfg.targets:
                if not t.armed:
                    continue
                for d in ReleaseRule(t.release, cfg.timezone).drops_for(t.dates, now):
                    wanted[(t.name, d.at)] = (t, d)

        with self._lock:
            # Stand down runs whose target was disarmed / deleted / re-dated.
            for key, r in self.runs.items():
                if not r.adhoc and r.state in ("armed", "running") and key not in wanted and r.drop.at > now:
                    r.cancel.set()
                    r.state, r.detail = "stopped", "disarmed or edited"
            # Start runs whose drop is close.
            for key, (t, d) in wanted.items():
                if key in self.runs or d.at - now > ARM_LEAD:
                    continue
                self._launch(cfg, t, d, live=t.live and not self.force_dry_run)

        if cfg:
            self._tick_watches(cfg, now)

    # ---- cancellation watching -------------------------------------------------------

    def _tick_watches(self, cfg: Config, now: datetime) -> None:
        """Dates already out: keep a Resy Notify signup for each. No polling."""
        active = set()
        for t in cfg.targets:
            if not (t.armed and t.live):
                continue
            days = watcher.watch_dates(t, cfg, now)
            if not days:
                continue
            with self._lock:   # a drop sniper is mid-run for this target: let it finish first
                if any(r.target == t.name and r.state == "running" for r in self.runs.values()):
                    continue
            active.add(t.name)
            want = (t.venue_id, tuple(days), tuple(f"{x:%H:%M}" for x in t.time_window), t.party_size)
            if self._notified.get(t.name) == want and _time.time() - self._notify_synced < 6 * 3600:
                continue
            self._sync_notify(t, days, want)
        for name in list(self._notified):          # disarmed, booked, or dates passed
            if name not in active:
                del self._notified[name]

    def _sync_notify(self, t: Target, days, want) -> None:
        try:
            c = service.client(dry_run=True)
        except SystemExit as e:
            log.warning("%s: can't sign up for Resy Notify: %s", t.name, e)
            return
        try:
            if self._notify_have is None or _time.time() - self._notify_synced > 6 * 3600:
                self._notify_have = resynotify.active(c)
                self._notify_synced = _time.time()
            added = resynotify.ensure(c, t.venue_id, days, (f"{t.time_window[0]:%H:%M}", f"{t.time_window[1]:%H:%M}"),
                                      t.party_size, self._notify_have)
            self._notified[t.name] = want
            if added:
                log.info("%s: signed up for Resy Notify (%s); will book when Resy emails a table",
                         t.name, ", ".join(added))
            if not mailwatch.creds():
                log.info("%s: Gmail isn't connected, so Resy's alert goes to your phone/email "
                         "and the bot can't book it for you (Settings → Connect Gmail)", t.name)
        except ResyError as e:
            log.warning("%s: Resy Notify signup failed: %s", t.name, e)
        finally:
            c.close()

    # ---- a Resy email arrived ----------------------------------------------------------

    def on_resy_email(self, subject: str, text: str) -> None:
        cfg = store.load_config(self.config_path)
        if not cfg:
            return
        now = datetime.now(ZoneInfo(cfg.timezone))
        blob = f"{subject} {text}".lower()
        for t in cfg.targets:
            if not (t.armed and t.live) or t.name in self._grabbing:
                continue
            days = watcher.watch_dates(t, cfg, now)
            if not days:
                continue
            names = {t.name.lower(), (t.url_slug or "").replace("-", " ").lower()}
            hit = (f"venue_id={t.venue_id}" in blob or (t.url_slug and f"/{t.url_slug}" in blob)
                   or any(n and len(n) >= 3 and n in blob for n in names))
            if hit:
                log.info("%s: Resy says a table opened (%s) — booking", t.name, subject)
                self._grabbing.add(t.name)
                threading.Thread(target=self._grab, args=(cfg, t, days), daemon=True,
                                 name=f"grab:{t.name}").start()

    def _grab(self, cfg: Config, t: Target, days) -> None:
        """A few fast tries right after the email (the table may take a second to show)."""
        live = not self.force_dry_run
        st = self.watches.setdefault(t.name, watcher.WatchState())
        try:
            c = service.client(dry_run=not live)
        except SystemExit as e:
            log.warning("%s: can't book: %s", t.name, e)
            self._grabbing.discard(t.name)
            return
        def page_url(slot):
            return (f"https://resy.com/cities/{t.city}/venues/{t.url_slug or t.venue_id}"
                    f"?date={slot.day.isoformat()}&seats={t.party_size}")
        try:
            for wait in (0, 1, 2, 4, 8, 15):
                _time.sleep(wait)
                booking = watcher.check(c, cfg, t, days, st, self.notifier, page_url)
                if booking:
                    if live:
                        st.result = f"booked {booking.reservation_id}"
                        store.update_target(t.name, self.config_path, armed=False)   # never book twice
                        resynotify.cancel_for(c, t.venue_id, days, t.party_size)
                    return
                if t.book_mode == "manual" and st.last_alert:
                    return                  # page opened for you; nothing more the bot can do
            log.info("%s: the table from Resy's email was gone before we got it; still signed up", t.name)
        except Exception as e:
            log.warning("%s: booking after Resy email failed: %s", t.name, e)
        finally:
            c.close()
            self._grabbing.discard(t.name)

    def _launch(self, cfg: Config, t: Target, d: Drop, *, live: bool, key=None) -> Run:
        r = Run(target=t.name, drop=d, live=live, adhoc=key is not None)
        self.runs[key or (t.name, d.at)] = r
        r.thread = threading.Thread(target=self._run, args=(cfg, t, r), name=f"snipe:{t.name}", daemon=True)
        r.thread.start()
        log.info("%s: sniper started for %s (%s)", t.name, d, "LIVE" if live else "dry run")
        return r

    def _run(self, cfg: Config, t: Target, r: Run) -> None:
        r.state = "running"
        try:
            c = service.client(dry_run=not r.live)
        except SystemExit as e:
            r.state, r.detail = "error", str(e)
            self.notifier.failed(t.name, str(e))
            return
        try:
            # Touch the login ~10 min early: an expired token gets refreshed
            # now, not at the drop second.
            try:
                c.whoami()
            except ResyError as e:
                self.notifier.send(f"{t.name}: {e}", title="⚠ Resy login problem", priority="high")
            sniper = Sniper(cfg, t, r.drop, c, self.notifier, cancel=r.cancel)
            booking = sniper.run()
            r.slot = sniper.slot
        except Exception as e:
            r.state, r.detail = "error", str(e)
            log.exception("%s: sniper crashed", t.name)
            self.notifier.failed(t.name, f"error: {e}")
            return
        finally:
            c.close()
        if r.cancel.is_set() and booking is None:
            r.state = "stopped"
        elif booking is None:
            r.state, r.detail = ("found" if getattr(sniper, "pinged", False) else "missed"), ""
        else:
            r.state, r.detail = "booked", booking.reservation_id or ""
            if r.live:
                try:
                    store.update_target(t.name, self.config_path, armed=False)   # never book twice
                except Exception as e:
                    log.warning("couldn't disarm %s after booking: %s", t.name, e)

    # ---- on demand -----------------------------------------------------------------

    def run_now(self, name: str, *, live: bool, dates: tuple | None = None) -> Run:
        """Snipe already-released dates immediately (like book_now.py)."""
        cfg = store.load_config(self.config_path)
        t = next((x for x in cfg.targets if x.name.lower() == name.lower()), None) if cfg else None
        if t is None:
            raise KeyError(name)
        cfg = cfg.model_copy(update={"poll_duration_s": 10})
        if dates:
            t = t.model_copy(update={"dates": list(dates)})
        d = Drop(at=datetime.now(ZoneInfo(cfg.timezone)) + timedelta(seconds=1), dates=tuple(t.dates))
        with self._lock:
            return self._launch(cfg, t, d, live=live and not self.force_dry_run, key=(t.name, d.at))

    def check_login(self) -> bool:
        self._last_login_check = _time.time()
        try:
            c = service.client()
            try:
                self.login_msg = service.whoami_line(c)
            finally:
                c.close()
            if self.login_ok is False:
                self.notifier.send("Resy login works again.", title="✅ resybot")
            self.login_ok = True
        except (ResyError, SystemExit) as e:
            if self.login_ok is not False:
                self.notifier.send(f"{e}\nSend a fresh token to fix it (Telegram: /token <token>).",
                                   title="⚠ Resy login problem", priority="high")
            self.login_ok, self.login_msg = False, str(e)
        return bool(self.login_ok)

    # ---- for display -------------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """Every armed target's next drop + every run, for status screens."""
        cfg = store.load_config(self.config_path)
        rows = []
        if cfg:
            for t in cfg.targets:
                d = service.next_drop(t, cfg)
                with self._lock:
                    mine = [r for (name, _), r in self.runs.items() if name == t.name]
                run = max(mine, key=lambda r: r.drop.at) if mine else None
                rows.append({"target": t, "drop": d, "run": run})
        return rows
