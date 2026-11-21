"""
Command-line entry point.  Usage:

    ./resy                                  # arrow-key menus (same as no arguments)
    python -m resybot serve                 # always-on scheduler (the server mode)
    python -m resybot serve --telegram      # ...controlled from Telegram
    python -m resybot check                 # token OK? next drops? current availability?
    python -m resybot venue <slug-or-url>   # look up a venue_id for targets.yaml
    python -m resybot run                   # DRY RUN: full routine, stops before booking
    python -m resybot run --live            # actually books
    python -m resybot run --live --only "Don Angie"
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, Secrets
from .notify import Notifier
from .release import ReleaseRule, infer_rule
from .resy import ResyClient, ResyError
from .sniper import Sniper


def _setup_logging(verbose: bool, quiet: bool = False):
    Path("logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s"
    handlers: list[logging.Handler] = [logging.FileHandler("logs/resybot.log")]
    if not quiet:   # the menus draw their own screen; log only to the file
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _client(dry_run: bool) -> tuple[ResyClient, Secrets]:
    from . import service
    c = service.client(dry_run=dry_run)     # auto-refreshes an expired token if a password is saved
    return c, Secrets.from_env()


# ---------------------------------------------------------------------------

def cmd_check(args):
    cfg = Config.load(args.config)
    client, sec = _client(dry_run=True)
    tz = ZoneInfo(cfg.timezone)

    print("== Account")
    try:
        me = client.whoami()
        print(f"   token OK — {me.get('first_name', '')} {me.get('last_name', '')} ({me.get('em_address', me.get('email', '?'))})")
        pms = me.get("payment_methods") or []
        if pms:
            for pm in pms:
                print(f"   payment method id={pm.get('id')}  {pm.get('type', '')} ****{pm.get('display', pm.get('last4', ''))}")
        else:
            print("   ⚠ no payment method on file — add a card in the Resy app before a live run")
    except ResyError as e:
        print(f"   ✗ {e}")
        return 1

    print(f"\n== Targets  (now {datetime.now(tz):%a %Y-%m-%d %H:%M %Z})")
    for t in cfg.targets:
        rule = ReleaseRule(t.release, cfg.timezone)
        drops = rule.drops_for(t.dates)
        print(f"\n   {t.name}  (venue {t.venue_id}, party {t.party_size}, rule '{t.release}', book_mode {t.book_mode})")
        try:
            vi = client.venue_info(t.venue_id, t.dates[0], t.party_size)
            cap = "ON  ⚠ bot can't complete bookings here" if vi.recaptcha else "off (bot can book)"
            print(f"     resy name: {vi.name}   captcha: {cap}")
            if vi.recaptcha and t.book_mode == "auto":
                print("     ✗ set `book_mode: manual` for this target — auto will fail at the book step")
            if not vi.recaptcha and t.book_mode == "manual":
                print("     (no captcha here; `book_mode: auto` would book it for you)")
        except ResyError as e:
            print(f"     venue lookup failed — {e}")
        if not drops:
            print("     no upcoming drops — all dates already released (or rule is 'now')")
        for d in drops:
            print(f"     drop: {d}")
        if args.availability:
            inferred: set[str] = set()
            for day in t.dates:
                try:
                    slots = client.find(t.venue_id, day, t.party_size)
                    shown = ", ".join(f"{s.start:%H:%M} {s.seat_type}" for s in slots[:8])
                    print(f"     {day}: {len(slots)} slot(s) open now  {shown}")
                    fees = {(s.deposit_usd, s.cancellation_fee_usd) for s in slots}
                    for dep, canc in sorted(fees):
                        print(f"       deposit ${dep:.0f}, late-cancel fee ${canc:.0f}")
                    inferred |= {infer_rule(s.day, s.released_at, cfg.timezone)
                                 for s in slots if s.released_at}
                except ResyError as e:
                    print(f"     {day}: find failed — {e}")
            for rule in sorted(inferred):
                mark = "✓ matches" if rule.lower() == t.release.lower() else "← differs from your rule"
                print(f"     Resy's release stamp suggests: '{rule}'  {mark}")
    return 0


def cmd_venue(args):
    client, _ = _client(dry_run=True)
    m = re.search(r"resy\.com/cities/([^/]+)/(?:venues/)?([^/?#]+)", args.slug)
    location, slug = (m.group(1), m.group(2)) if m else ("ny", args.slug)
    try:
        v = client.venue_by_slug(slug, location)
    except ResyError as e:
        print(f"✗ {e}")
        return 1
    vid = (v.get("id") or {}).get("resy") if isinstance(v.get("id"), dict) else v.get("id")   # VERIFY
    print(f"name:     {v.get('name')}")
    print(f"venue_id: {vid}")
    print(f"rule hint: check the venue page on resy.com for text like 'reservations open N days in advance'")
    return 0


def cmd_run(args):
    cfg = Config.load(args.config)
    dry_run = not args.live
    client, sec = _client(dry_run=dry_run)
    notifier = Notifier()

    if dry_run:
        logging.warning("DRY RUN — will stop before booking. Pass --live to book for real.")
    else:
        logging.warning("LIVE — this will book and may charge deposits up to each target's max_deposit_usd.")

    try:
        client.whoami()
    except ResyError as e:
        logging.error("Token check failed: %s", e)
        return 1

    snipers: list[Sniper] = []
    for t in cfg.targets:
        if args.only and t.name.lower() != args.only.lower():
            continue
        if not t.armed and not args.only:
            continue
        for d in ReleaseRule(t.release, cfg.timezone).drops_for(t.dates):
            snipers.append(Sniper(cfg, t, d, client, notifier))

    if not snipers:
        logging.error("No upcoming drops for any target. Check dates/release rules with `check`.")
        return 1

    for s in snipers:
        logging.info("armed: %s — %s", s.target.name, s.drop)

    # Each drop runs on its own thread so two venues dropping at 10:00 don't
    # queue behind each other. The client's connection pool is thread-safe.
    threads = [threading.Thread(target=s.run, name=s.target.name, daemon=True) for s in snipers]
    for th in threads:
        th.start()
    try:
        for th in threads:
            th.join()
    except KeyboardInterrupt:
        logging.warning("stopped by user")
        return 130
    return 0


def cmd_serve(args):
    """Always-on: arm every armed target's drops as they come; optional Telegram control."""
    import os
    from .scheduler import Scheduler

    Secrets.from_env()          # fail fast if the login is missing
    notifier = Notifier()
    sched = Scheduler(notifier, args.config, force_dry_run=args.dry_run)
    sched.check_login()
    logging.info("Resy login: %s", sched.login_msg)

    bot = None
    if args.telegram:
        try:
            from .telegram_bot import Bot
        except ImportError:
            logging.error("Telegram remote control isn't included in this version.")
            return 1
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            logging.error("TELEGRAM_BOT_TOKEN missing from .env (see deploy/README.md).")
            return 1
        bot = Bot(token, sched)
        notifier.add_channel(bot.notify_channel)

    sched.start()
    try:
        if bot:
            bot.run_forever()
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        logging.warning("stopped by user")
        sched.stop()
    return 0


def cmd_catalog(args):
    """Resolve a list of restaurant names on Resy and add them to restaurants.yaml."""
    from . import catalog, service
    entries = []
    for line in Path(args.file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, hint = line.partition("|")
        entries.append((name.strip(), hint.strip() or None))
    c = service.client()

    def progress(i, n, name, rec):
        if rec is None:
            print(f"[{i:3}/{n}] ✗ {name}: not on Resy (or no exact match)", flush=True)
        elif isinstance(rec, str):
            print(f"[{i:3}/{n}] = {name}: {rec}", flush=True)
        else:
            rule = rec.get("release") or (f"{rec['release_days']} days ahead (time unknown)" if rec.get("release_days") else "rule unknown")
            print(f"[{i:3}/{n}] ✓ {name} → {rec['name']} (venue {rec['venue_id']}, {rec['city']}) · {rule}"
                  f"{' · CAPTCHA' if rec.get('captcha') else ''}", flush=True)

    added, missing = catalog.import_names(c, entries, source=args.source, on_progress=progress)
    print(f"\nAdded {len(added)} to {catalog.CATALOG}. Not found on Resy: {len(missing)}")
    for m in missing:
        print(f"   - {m}")
    return 0


def cmd_menu(args):
    from .tui import main as tui_main
    return tui_main()


# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="resybot")
    p.add_argument("--config", default="targets.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    p.set_defaults(fn=cmd_menu)

    cat = sub.add_parser("catalog", help="add restaurants to restaurants.yaml from a list of names")
    cat.add_argument("file", help="one restaurant per line: Name  or  Name|resy-slug-hint")
    cat.add_argument("--source", default="list")
    cat.set_defaults(fn=cmd_catalog)

    m = sub.add_parser("menu", help="arrow-key menus (the default)")
    m.set_defaults(fn=cmd_menu)

    s = sub.add_parser("serve", help="always-on scheduler; arms drops as they come")
    s.add_argument("--telegram", action="store_true", help="remote control via Telegram")
    s.add_argument("--dry-run", action="store_true", help="never book, even targets marked live")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("check", help="verify token, show next drops and current availability")
    c.add_argument("--no-availability", dest="availability", action="store_false")
    c.set_defaults(fn=cmd_check)

    v = sub.add_parser("venue", help="look up a venue_id from a resy.com URL or slug")
    v.add_argument("slug")
    v.set_defaults(fn=cmd_venue)

    r = sub.add_parser("run", help="arm the sniper for every upcoming drop")
    r.add_argument("--live", action="store_true", help="actually book (default is dry run)")
    r.add_argument("--only", help="only this target name")
    r.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    _setup_logging(args.verbose, quiet=args.fn is cmd_menu)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
