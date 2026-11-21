"""
Arrow-key menus: `./resy` (or `python -m resybot` with no arguments).

Everything here is prompting and display. The logic is in service.py,
store.py and scheduler.py, shared with the Telegram bot and the classic
subcommands.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import questionary as q
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from questionary import Choice
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import os

from . import auth, background, catalog, history, mailwatch, onboarding, resynotify, service, store
from .notify import Notifier
from .resy import ResyError
from .scheduler import Scheduler
from . import watcher

con = Console()
STYLE = q.Style([("qmark", "fg:#f97316 bold"), ("pointer", "fg:#f97316 bold"),
                 ("highlighted", "fg:#f97316 bold"), ("selected", "fg:#f97316"), ("answer", "fg:#f97316")])


class _Back:
    """'Go back one step'. Falsy, so code that doesn't care treats it like cancel."""
    def __bool__(self):
        return False

    def __repr__(self):
        return "BACK"


BACK = _Back()


def back_choice():
    return Choice("← Back", BACK)


def ask(prompt):
    """Run a questionary prompt. Esc → BACK (one step back), Ctrl+C → None (out to the menu)."""
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result=BACK)

    app = prompt.application
    app.key_bindings = merge_key_bindings([app.key_bindings, kb]) if app.key_bindings else kb
    app.ttimeoutlen = 0.05      # make Esc respond instantly
    return prompt.ask(kbi_msg="")


def text(v) -> bool:
    """A real typed answer (not back / cancel)."""
    return isinstance(v, str)


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main() -> int:
    con.print(Panel.fit("[bold]resybot[/]  ·  arrow keys to move, Enter to pick, Esc or ← Back to go back a step",
                        border_style="#f97316"))
    migrate_practice_targets()
    if not onboarding.complete():
        checklist()                 # covers the Resy login too
    else:
        startup_login_check()
    while True:
        show_status()
        con.print(bot_line())
        todo = None if onboarding.complete() else checklist_items()
        choice = ask(q.select("What do you want to do?", style=STYLE, choices=[
            *([Choice(f"📋 Getting started  ({sum(i['done'] for i in todo)}/{len(todo)} done)", "setup")]
              if todo else []),
            Choice("Book a restaurant", "book"),
            Choice("My reservations", "manage"),
            Choice("Practice run (try it without booking)", "practice"),
            Choice("Settings & Resy login", "settings"),
            Choice("Quit", "quit"),
        ]))
        if choice is BACK:
            continue                     # Esc on the main menu: nothing to go back to
        if choice in (None, "quit"):
            if confirm_quit():
                return 0
            continue
        try:
            {"book": book_flow, "manage": manage_menu, "practice": practice_run,
             "settings": settings_menu, "setup": checklist}[choice]()
        except KeyboardInterrupt:
            con.print()
        except (ResyError, SystemExit) as e:
            con.print(f"[red]✗ {e}[/]")
        except Exception as e:  # keep the menu alive; show the problem
            con.print(f"[red]✗ {type(e).__name__}: {e}[/]")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def current_targets(cfg) -> list:
    """What the bot is working on right now: set to Book it, with a drop coming or a date to watch."""
    if not cfg:
        return []
    return [t for t in cfg.targets if t.armed and t.live and (service.next_drop(t, cfg) or watching(t, cfg))]


def status_table():
    """Main screen: only what's live right now (past and switched-off ones live in My reservations)."""
    cfg = store.load_config()
    now = current_targets(cfg)
    if not now:
        return Text.from_markup("[dim]Nothing going on right now. Pick “Book a restaurant” to start.[/]")
    tb = Table(expand=True, border_style="grey37", title="Right now", title_justify="left")
    for col in ("Restaurant", "For", "What the bot is doing", "In"):
        tb.add_column(col)
    for t in now:
        d = service.next_drop(t, cfg)
        if d:
            doing, until = f"books when tables open {service.fmt_drop(d.at)}", service.countdown(d.at)
        else:
            doing, until = notify_text(), ""
        if t.book_mode == "manual":
            doing += " [red](captcha: alerts you)[/]"
        tb.add_row(f"[bold]{t.name}[/]", service.describe(t), doing, until)
    return tb


def show_status() -> None:
    con.print(status_table())


# ---------------------------------------------------------------------------
# Start the bot (live dashboard)
# ---------------------------------------------------------------------------

def bot_line() -> str:
    st = background.status()
    if st["running"]:
        return f"[green]●[/] Bot is on (since {st['since']:%a %-I:%M %p})  [dim]keeps going if you quit · turn off in Settings[/]"
    n = len(upcoming_armed())
    if n:
        return f"[yellow]○[/] Bot is off  [yellow]({n} restaurant{'s' if n > 1 else ''} set to Book it will be missed)[/]"
    return "[dim]○ Bot is off[/]"


def upcoming_armed() -> list:
    cfg = store.load_config()
    if not cfg:
        return []
    return [t for t in cfg.targets if t.armed and t.live and (service.next_drop(t, cfg) or watching(t, cfg))]


def notify_text() -> str:
    """What happens for dates that are already out."""
    if mailwatch.creds():
        return "Resy Notify on: books it the moment a table opens"
    return "Resy Notify on: Resy alerts you [yellow](connect Gmail in Settings so the bot books it)[/]"


def signup_notify(t, cfg) -> None:
    """Sign up for Resy Notify right away (the bot also keeps it up to date)."""
    days = watching(t, cfg)
    if not days:
        return
    c = service.client()
    try:
        with con.status("Signing you up for Resy Notify…"):
            resynotify.ensure(c, t.venue_id, days, (f"{t.time_window[0]:%H:%M}", f"{t.time_window[1]:%H:%M}"),
                              t.party_size)
        def hm(x):
            return f"{x:%-I:%M%p}".replace(":00", "").lower()
        con.print(f"[green]✓ Signed up for Resy Notify[/] for {', '.join(f'{d:%a %b %-d}' for d in days)}, "
                  f"{hm(t.time_window[0])}–{hm(t.time_window[1])}, party of {t.party_size}.")
        if mailwatch.creds():
            con.print("  When Resy emails that a table opened, the bot books it within seconds.")
        else:
            con.print("  [yellow]Resy will alert your phone/email. To have the bot book it automatically, "
                      "connect Gmail: Settings → Connect Gmail.[/]")
    except ResyError as e:
        con.print(f"[red]Couldn't sign up for Resy Notify:[/] {e}")
    finally:
        c.close()


def drop_notify(t, cfg) -> None:
    """Turned off / removed: cancel its Resy Notify signups (best effort)."""
    days = watching(t, cfg)
    if not days:
        return
    try:
        c = service.client()
    except SystemExit:
        return
    try:
        resynotify.cancel_for(c, t.venue_id, days, t.party_size)
    finally:
        c.close()


def watching(t, cfg) -> list:
    """Already-open dates the bot will watch for cancellations."""
    return watcher.watch_dates(t, cfg, datetime.now(service.ZoneInfo(cfg.timezone)))


def confirm_quit() -> bool:
    """Leaving with armed drops and no bot running? Offer to start it."""
    if background.status()["running"] or not upcoming_armed():
        return True
    pick = ask(q.select("Some restaurants are set to Book it, but the bot is off.", style=STYLE, choices=[
        Choice("Turn on the bot, then quit", "start"),
        Choice("Quit anyway", "quit"),
        Choice("Go back", "back"),
    ]))
    if pick == "start":
        start_background()
        return True
    return pick == "quit"


def offer_start_bot(name: str) -> None:
    """Right after a restaurant is set to Book it: make sure the bot will be there."""
    if background.status()["running"]:
        con.print(f"[green]●[/] The bot is already running and will book {name} at the drop.")
        return
    if ask(q.confirm("Turn on the bot now? It runs in the background and books at the drop.",
                     default=True, style=STYLE)):
        st = background.start()
        con.print(f"[green]✓ Bot is on[/] (pid {st['pid']}). You can close this window. "
                  "Keep the Mac plugged in with the lid open at drop time.")
    else:
        con.print("[yellow]Okay. Nothing will book until the bot is on: Settings → Turn on the bot.[/]")


def start_background() -> None:
    live_ones = [t.name for t in upcoming_armed() if t.live]
    if live_ones and not ask(q.confirm(f"This will book: {', '.join(live_ones)}. Start?",
                                       default=True, style=STYLE)):
        return
    st = background.start()
    con.print(f"[green]✓ Bot started in the background[/] (pid {st['pid']}). "
              "You can close this window. Keep the Mac plugged in with the lid open at drop time.")


def watch_background() -> None:
    con.print("[dim]Ctrl+C to go back to the menu. The bot keeps running.[/]")
    try:
        with Live(console=con, refresh_per_second=2) as live:
            while True:
                st = background.status()
                head = bot_line() if st["running"] else "[red]● The bot stopped. See the log below.[/]"
                live.update(Group(Text.from_markup(head), status_table(),
                                  Panel("\n".join(background.tail(14)) or "[dim]no output yet[/]",
                                        title="log", border_style="grey37")))
                if not st["running"]:
                    break
                _time.sleep(0.5)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Book a restaurant: pick any restaurant → date → times → (details) → go
# ---------------------------------------------------------------------------

class _Always(str):
    """A menu label that survives the type-to-filter (so 'Search all of Resy' never disappears)."""
    def lower(self):
        return _MatchesAll(self)


class _Styled(list):
    """Coloured menu label (questionary tokens) that the type-to-filter can still search."""
    def __init__(self, tokens):
        super().__init__(tokens)
        self.plain = "".join(t[1] for t in tokens)

    def lower(self):
        return self.plain.lower()


class _MatchesAll(str):
    def __contains__(self, _):
        return True


def _choose_restaurant(c) -> tuple[dict, dict, object] | None:
    """
    One list: your restaurants first, then the restaurant list. Type to filter.
    Anything not listed: search all of Resy, or paste a link.
    Returns (venue, facts, existing_target_or_None).
    """
    cfg = store.load_config()
    targets = cfg.targets if cfg else []
    items = catalog.load()

    # Just names, A–Z, one entry per restaurant. Your own come with your last settings.
    entries: dict[int, tuple[str, tuple]] = {}
    for t in targets:
        entries.setdefault(t.venue_id, (t.name, ("target", t.name)))
    for r in items:
        entries.setdefault(r["venue_id"], (r["name"], ("catalog", r["venue_id"])))
    counts: dict[str, int] = {}
    for label, _ in entries.values():
        counts[label.lower()] = counts.get(label.lower(), 0) + 1
    by_id = {r["venue_id"]: r for r in items}
    manual = {t.venue_id for t in targets if t.book_mode == "manual"}
    choices = []
    for vid, (label, value) in sorted(entries.items(), key=lambda kv: kv[1][0].lower()):
        if counts[label.lower()] > 1 and vid in by_id:      # two places, same name: say which
            label += f" ({by_id[vid].get('neighborhood') or _city(by_id[vid])})"
        if by_id.get(vid, {}).get("captcha") or vid in manual:
            label = _Styled([("class:text", label), ("fg:#ef4444 bold", "  captcha")])
        choices.append(Choice(label, value))
    choices.append(Choice(_Always("🔎 Not here? Search all of Resy"), ("search", None)))
    choices.append(Choice(_Always("🔗 Paste a resy.com link"), ("link", None)))
    choices.append(Choice(_Always("← Back"), BACK))

    while True:
        got = _choose_once(c, choices, targets, items)
        if got is not None:      # a restaurant, or BACK out of the list
            return got
        # None from a sub-step means 'back to the list'


def _choose_once(c, choices, targets, items):
    pick = ask(q.select("Which restaurant?  (type to filter)", style=STYLE, choices=choices,
                        use_search_filter=True, use_jk_keys=False, use_shortcuts=False,
                        instruction=" "))
    if pick is BACK:
        return BACK
    if not pick:
        raise KeyboardInterrupt
    kind, key = pick
    if kind == "target":
        t = next(x for x in targets if x.name == key)
        fact = next((r for r in items if r["venue_id"] == t.venue_id), {})
        return {"id": t.venue_id, "name": t.name, "slug": t.url_slug, "city": t.city}, fact, t
    if kind == "catalog":
        r = next(x for x in items if x["venue_id"] == key)
        return _catalog_venue(r), r, None
    if kind == "link":
        link = ask(q.text("Paste the restaurant's resy.com link:", style=STYLE, instruction="(Esc = back)"))
        if not link:
            return _back_or_quit(link)
        with con.status("Looking it up…"):
            venue = service.venue_from_link(c, link)
        return venue, {}, _existing(targets, venue["id"])

    while True:
        query = ask(q.text("Restaurant name:", style=STYLE, instruction="(Esc = back)",
                           validate=lambda s: bool(s.strip()) or "type a name"))
        if not query:
            return _back_or_quit(query)
        with con.status(f"Searching Resy for “{query}”…"):
            hits = catalog.search(c, query.strip())
        if not hits:
            con.print(f"[yellow]Nothing on Resy matches “{query}”. Try a shorter name, or paste its resy.com link.[/]")
            continue
        r = ask(q.select("Which one?", style=STYLE, choices=[
            *[Choice(f"{h['name']}  ·  {h.get('where') or _city(h)}", h) for h in hits],
            Choice("None of these, search again", BACK), back_choice()]))
        if r is BACK:
            continue
        if not r:
            raise KeyboardInterrupt
        break
    return _catalog_venue(r), r, _existing(targets, r["venue_id"])


def _back_or_quit(v):
    """In a sub-step: Esc → None (back to the previous screen); Ctrl+C → leave the flow."""
    if v is BACK:
        return None
    raise KeyboardInterrupt


def _existing(targets, venue_id):
    return next((t for t in targets if t.venue_id == venue_id), None)


def _pick_date(c, venue, facts, existing, tz, party) -> tuple[list[str], str] | None:
    """Ask for the date. Returns (dates, release_rule)."""
    rule = (existing.release if existing else None) or facts.get("release")
    if not rule or facts.get("release_days") is None:
        with con.status("Checking when Resy releases tables…"):
            facts = {**catalog.probe(c, venue["id"], tz, party), **{k: v for k, v in facts.items() if v is not None}}
        rule = rule or facts.get("release")
    if facts.get("captcha") is not None:
        venue["captcha"] = facts["captcha"]

    today = datetime.now(ZoneInfo(tz)).date()
    choices = []
    if existing and any(d >= today for d in existing.dates):
        keep = [d for d in existing.dates if d >= today]
        choices.append(Choice("Same as before: " + ", ".join(f"{d:%a %b %-d}" for d in keep),
                              [d.isoformat() for d in keep]))
    nxt = service.next_release_date(rule, tz) if rule else None
    if nxt:
        choices.append(Choice(f"Next release: {nxt[0]:%a %b %-d} (tables open {service.fmt_drop(nxt[1])})",
                              [nxt[0].isoformat()]))
    choices.append(Choice("Pick a date", "type"))
    choices.append(back_choice())
    while True:
        dates = ask(q.select("Which date?", style=STYLE, choices=choices))
        if not dates:
            return dates                       # BACK → previous step, None → cancel
        if dates != "type":
            break
        s = ask(q.text("Date (or a few, any of them is fine):", style=STYLE,
                       instruction="e.g. dec 11, dec 12  (Esc = back)",
                       validate=_valid(lambda s: service.parse_dates(s, tz))))
        if s is BACK:
            continue
        if not s:
            return None
        dates = service.parse_dates(s, tz)
        break

    if not rule:
        # No release pattern known. If the date is inside Resy's calendar it's open now;
        # otherwise ask when tables come out.
        reach = facts.get("release_days")
        last = max(datetime.fromisoformat(d).date() for d in dates)
        if reach is not None and (last - today).days <= reach:
            rule = "now"
        else:
            hint = f"Resy's calendar currently runs {reach} days ahead. " if reach is not None else ""
            con.print(f"[dim]{hint}Check the restaurant's Resy page for when reservations open.[/]")
            rule = ask(q.text("When do tables for that date come out?",
                              default=f"{reach or 30} days ahead at 10:00", style=STYLE,
                              instruction="'N days ahead at HH:MM', 'every monday at 10:00' or 'monthly on the 15th at 12:00'  (Esc = back)",
                              validate=_valid(lambda s: service.ReleaseRule(s))))
            if not rule:
                return rule
    return dates, rule


def _details_loop(name: str, d: dict, practice: bool = False) -> bool:
    """The rest of the settings with sensible defaults. Enter on 'Go' to accept."""
    while True:
        when = ", ".join(f"{datetime.fromisoformat(x):%a %b %-d}" for x in d["dates"])
        fb = "none" if not d["fallback"] else "–".join(d["fallback"])
        pick = ask(q.select(f"{name} · {when} · {d['window'][0]}–{d['window'][1]}", style=STYLE, choices=[
            Choice("✅ Go (practice: nothing gets booked)" if practice else "✅ Go", "go"),
            Choice(f"Party size  ({d['party']})", "party"),
            Choice(f"Seating  ({', '.join(d['seats']) or 'any'})", "seats"),
            Choice(f"Backup times if those are gone  ({fb})", "fallback"),
            Choice(f"Max deposit  (${d['deposit']:.0f}{', refuse any deposit' if not d['deposit'] else ''})", "deposit"),
            Choice(f"Who books  ({'you click, captcha' if d['book_mode'] == 'manual' else 'the bot'})", "mode"),
            back_choice(),
        ]))
        if not pick:
            return pick             # BACK → previous step, None → cancel
        if pick == "go":
            return True
        if pick == "party":
            s = ask(q.text("Party size:", default=str(d["party"]), style=STYLE,
                           validate=lambda s: s.isdigit() and 1 <= int(s) <= 20 or "1–20"))
            if s:
                d["party"] = int(s)
        elif pick == "seats":
            s = ask(q.text("Seating, best first (blank = any):", default=", ".join(d["seats"]), style=STYLE,
                           instruction="e.g. Dining Room, Bar  (Esc = back)"))
            if text(s):
                d["seats"] = [x.strip() for x in s.split(",") if x.strip()]
        elif pick == "fallback":
            s = ask(q.text("Backup times, a wider range you'd settle for (blank = none):",
                           default="-".join(d["fallback"] or []), style=STYLE, instruction="e.g. 5pm-10:30pm",
                           validate=lambda s: not s or _check_fallback(s, d["window"])))
            if text(s):
                d["fallback"] = service.parse_window(s) if s else None
        elif pick == "deposit":
            s = ask(q.text("Max deposit in $ (0 = refuse any deposit):", default=str(int(d["deposit"])), style=STYLE,
                           validate=lambda s: s.replace(".", "", 1).isdigit() or "a number"))
            if s:
                d["deposit"] = float(s)
        elif pick == "mode":
            m = ask(q.select("Who books?", style=STYLE, choices=[
                Choice("The bot books it", "auto"),
                Choice("Alert me and open the page, I click (needed for captcha restaurants)", "manual"),
                back_choice()]))
            if m:
                d["book_mode"] = m


def _check_fallback(s: str, window: list[str]):
    try:
        fs, fe = service.parse_window(s)
    except Exception as e:
        return str(e)
    return (fs <= window[0] and fe >= window[1]) or f"must include your times ({window[0]}–{window[1]})"


def practice_run() -> None:
    book_flow(practice=True)


def book_flow(practice: bool = False) -> None:
    """Pick any restaurant → date → times → Go. The bot then does whatever fits:
    book at the drop, book right now, or watch for a cancellation.
    practice=True: the same questions, then a trial run that stops right before booking."""
    raw = store.load_raw()
    tz = raw["timezone"]
    if practice:
        con.print("[cyan]Practice run:[/] same steps as booking, but it stops right before booking. "
                  "Nothing is reserved or saved.")
    c = service.client()
    try:
        step, got, picked, w = 0, None, None, None
        while True:
            if step == 0:
                got = _choose_restaurant(c)
                if not got:
                    return                          # Back from the list → main menu
                venue, facts, existing = got
                party = existing.party_size if existing else raw["party_size"]
                step = 1
            elif step == 1:
                picked = _pick_date(c, venue, facts, existing, tz, party)
                if picked is BACK:
                    step = 0
                    continue
                if not picked:
                    return
                dates, rule = picked
                step = 2
            elif step == 2:
                default_w = w or (f"{existing.time_window[0]:%-I:%M%p}-{existing.time_window[1]:%-I:%M%p}".lower()
                                  if existing else "6pm-9:30pm")
                w = ask(q.text("What times?", default=default_w, style=STYLE, instruction="(Esc = back)",
                               validate=_valid(service.parse_window)))
                if w is BACK:
                    w, step = None, 1
                    continue
                if not w:
                    return
                captcha = venue.get("captcha")
                d = {
                    "dates": dates, "window": service.parse_window(w), "party": party,
                    "seats": list(existing.seat_types) if existing else [],
                    "fallback": ([f"{x:%H:%M}" for x in existing.fallback_window]
                                 if existing and existing.fallback_window else None),
                    "deposit": existing.max_deposit_usd if existing else 0.0,
                    "book_mode": existing.book_mode if existing else ("manual" if captcha else "auto"),
                }
                if d["fallback"] and not (d["fallback"][0] <= d["window"][0] and d["fallback"][1] >= d["window"][1]):
                    d["fallback"] = None        # old backup range no longer covers the new times
                if captcha and d["book_mode"] == "manual":
                    con.print("[yellow]This restaurant has a captcha: when a table shows up, the bot opens the page "
                              "and plays a sound, and you click to confirm.[/]")
                step = 3
            else:
                ok = _details_loop(venue["name"], d, practice)
                if ok is BACK:
                    step = 2
                    continue
                if not ok:
                    return
                break
        if practice:
            return _practice(c, venue, d, rule, tz)
    finally:
        c.close()

    # Save it (off for the moment, so a running bot doesn't jump in while we try).
    patch = {"dates": d["dates"], "time_window": d["window"], "release": rule, "party_size": d["party"],
             "seat_types": d["seats"], "fallback_window": d["fallback"], "max_deposit_usd": d["deposit"],
             "book_mode": d["book_mode"], "armed": False, "live": False}
    if existing:
        name, is_new = existing.name, False
        store.update_target(name, **patch)
    else:
        t = service.new_target(name=venue["name"], venue=venue, dates=d["dates"], window=d["window"],
                               release=rule, party=d["party"], seats=d["seats"], live=False, armed=False,
                               book_mode=d["book_mode"], max_deposit=d["deposit"])
        if d["fallback"]:
            t["fallback_window"] = d["fallback"]
        name, n = t["name"], 2
        while store.find(store.load_raw(), name):
            name, n = f"{t['name']} {n}", n + 1
        t["name"], is_new = name, True
        store.add_target(t)

    cfg = store.load_config()
    t = next(x for x in cfg.targets if x.name == name)
    drop, watch = service.next_drop(t, cfg), watching(t, cfg)
    days = ", ".join(f"{x:%a %b %-d}" for x in t.dates)

    if drop:
        store.update_target(name, armed=True, live=True)
        con.print(f"[green]✓[/] {name}, {days}: the bot books it when tables open, {service.fmt_drop(drop.at)} "
                  f"(in {service.countdown(drop.at)}).")
        offer_start_bot(name)
        return
    if not watch:
        con.print(f"[yellow]{days} has already passed.[/]")
        if is_new:
            store.remove_target(name)
        return

    # Tables for that date are already out: try right now.
    sched = Scheduler(Notifier())
    run = sched.run_now(name, live=True)
    with con.status(f"Looking for a table at {name}…"):
        run.thread.join()
    if run.state == "booked":
        con.print(f"[green]🎉 Booked {name}[/] (reservation {run.detail}). It's in your Resy account.")
        if is_new:
            store.remove_target(name)
        return
    if run.state == "found":
        con.print("[green]A table is open. The Resy page just opened in your browser: click the time and confirm.[/]")
        if is_new:
            store.remove_target(name)
        return
    if run.state != "missed":
        con.print(f"[red]Something went wrong:[/] {run.state} {run.detail}")
        if is_new:
            store.remove_target(name)
        return
    if ask(q.confirm("Nothing open in your times right now. Sign up for Resy Notify and book it when a table opens?",
                     default=True, style=STYLE)):
        store.update_target(name, armed=True, live=True)
        cfg = store.load_config()
        signup_notify(next(x for x in cfg.targets if x.name == name), cfg)
        if mailwatch.creds():
            offer_start_bot(name)
    elif is_new:
        store.remove_target(name)


CITY_NAMES = {"new-york-ny": "New York", "miami-fl": "Miami", "washington-dc": "Washington DC",
              "chicago-il": "Chicago", "jersey-city-nj": "Jersey City", "sea-bright-nj": "Sea Bright"}


def _city(r: dict) -> str:
    return CITY_NAMES.get(r.get("city", ""), (r.get("city") or "").rsplit("-", 1)[0].replace("-", " ").title())


def _catalog_venue(r: dict) -> dict:
    return {"id": r["venue_id"], "name": r["name"], "slug": r.get("url_slug"), "city": r.get("city"),
            "captcha": r.get("captcha")}


def _ask_rule(suggested: str | None) -> str | None:
    if suggested:
        use = ask(q.select(f"Resy's data says tables open “{suggested}”.", style=STYLE, choices=[
            Choice(f"Use “{suggested}”", suggested), Choice("Type a different rule", "_"), back_choice()]))
        if use is None:
            return None
        if use != "_":
            return use
    else:
        con.print("[dim]Couldn't detect a release rule (no open tables to read it from). "
                  "Check the restaurant's Resy page for “reservations open N days in advance”.[/]")
    return ask(q.text("Release rule:", default="30 days ahead at 10:00", style=STYLE,
                      instruction="'N days ahead at HH:MM', 'every monday at 10:00' or 'monthly on the 15th at 12:00'",
                      validate=_valid(lambda s: service.ReleaseRule(s))))


def _ask_dates(rule: str, tz: str) -> list[str] | None:
    nxt = service.next_release_date(rule, tz)
    choices = []
    if nxt:
        day, at = nxt
        choices.append(Choice(f"Next release: {day:%a %b %-d} (opens {service.fmt_drop(at)})", "next"))
    choices.append(Choice("Type date(s)", "type"))
    choices.append(back_choice())
    pick = ask(q.select("Which date?", style=STYLE, choices=choices))
    if not pick:
        return None
    if pick == "next":
        return [nxt[0].isoformat()]
    s = ask(q.text("Date(s): (any of them is fine)", style=STYLE, instruction="e.g. dec 11, dec 12",
                   validate=_valid(lambda s: service.parse_dates(s, tz))))
    return service.parse_dates(s, tz) if s else None


def _valid(fn):
    def check(s):
        try:
            fn(s)
            return True
        except Exception as e:
            return str(e)
    return check


# ---------------------------------------------------------------------------
# Check / toggle / edit / remove / book now
# ---------------------------------------------------------------------------

def check_one(cfg, t) -> None:
    c = service.client()
    try:
        with con.status("Asking Resy…"):
            lines = service.check_target(c, cfg, t)
    finally:
        c.close()
    con.print(Panel("\n".join(lines), title=t.name, border_style="grey37"))


MODE_LABEL = {"live": "[bold red]Book it[/]", "off": "[dim]Off[/]"}
MODE_PLAIN = {"live": "Book it", "off": "Off"}


def mode_of(t) -> str:
    return "live" if t.armed and t.live else "off"


def migrate_practice_targets() -> None:
    """Practice is no longer a per-restaurant setting: anything left in that
    state (on, but not booking) becomes Off, so it can't run half-armed."""
    raw = store.load_raw()
    changed = [t["name"] for t in raw["targets"] if t.get("armed", True) and not t.get("live", False)]
    for name in changed:
        store.update_target(name, armed=False)


def set_mode(cfg, t) -> None:
    drop, watch = service.next_drop(t, cfg), watching(t, cfg)
    if not drop and not watch:
        con.print(f"[yellow]{t.name}: its dates have passed. Change the dates first.[/]")
        return
    if not drop:
        con.print(f"[dim]{t.name}'s dates are already open. With Book it, the bot signs you up for Resy Notify "
                  "and books it when a table opens.[/]")
    mode = ask(q.select(f"{t.name} should…", style=STYLE, default=mode_of(t), choices=[
        Choice("Book it", "live"), Choice("Off", "off"), back_choice()]))
    if not mode:
        return
    was_on = t.armed and t.live
    store.update_target(t.name, armed=mode == "live", live=mode == "live")
    if mode == "live":
        if not drop:
            cfg = store.load_config()
            signup_notify(next(x for x in cfg.targets if x.name == t.name), cfg)
        offer_start_bot(t.name)
    elif was_on and not drop:
        drop_notify(t, cfg)


def _pretty_slot(slot: str | None) -> str:
    when = history.slot_when(slot)
    if not when:
        return slot or ""
    place = history.slot_place(slot)
    return f"{when:%a %b %-d}, {when:%-I:%M%p}".replace(":00", "").replace("AM", "am").replace("PM", "pm") + \
        (f" · {place}" if place else "")


def _ago(at: str | None) -> str:
    if not at:
        return "—"
    d = datetime.fromisoformat(at)
    return f"{d:%b %-d}"


def reservations_view() -> None:
    """Only what's current: upcoming bookings, what's waiting, and the last week's activity."""
    cfg = store.load_config()
    rows = history.load()
    now = datetime.now()
    bot_on = background.status()["running"]

    # Booked
    booked = [r for r in rows if r["kind"] == "booked" and (history.slot_when(r.get("slot")) or now) >= now]
    booked.sort(key=lambda r: history.slot_when(r.get("slot")) or now)
    tb = Table(expand=True, border_style="green", title="✅ Booked", title_justify="left")
    for col in ("Restaurant", "When", "Party", "Reservation #"):
        tb.add_column(col)
    for r in booked:
        tb.add_row(r["name"], _pretty_slot(r.get("slot")), str(r.get("party") or ""), r.get("reservation") or "")
    if not booked:
        tb.add_row("[dim]no upcoming reservations from the bot[/]", "", "", "")
    con.print(tb)

    # Waiting
    wt = Table(expand=True, border_style="#f97316", title="⏳ Waiting", title_justify="left")
    for col in ("Restaurant", "For", "What it's doing"):
        wt.add_column(col)
    waiting = current_targets(cfg)
    for t in waiting:
        drop = service.next_drop(t, cfg)
        doing = (f"books when tables open {service.fmt_drop(drop.at)} (in {service.countdown(drop.at)})" if drop
                 else notify_text())
        if t.book_mode == "manual":
            doing += ", alerts you (captcha)"
        if not bot_on:
            doing += "  [red]· bot is off![/]"
        wt.add_row(t.name, service.describe(t), doing)
    if not waiting:
        wt.add_row("[dim]nothing waiting[/]", "", "")
    con.print(wt)

    # Recent activity (alerts + misses)
    week_ago = (now - timedelta(days=7)).isoformat()
    events = [r for r in rows if r["kind"] in ("alert", "missed") and (r.get("at") or "") >= week_ago][-8:]
    if events:
        at = Table(expand=True, border_style="grey37", title="Last 7 days", title_justify="left")
        for col in ("Day", "Restaurant", "What happened"):
            at.add_column(col)
        for r in reversed(events):
            if r["kind"] == "alert":
                what = f"table open ({_pretty_slot(r.get('slot'))}), alerted you to click and book"
                if r.get("note") == "cancellation":
                    what = "cancellation: " + what
            else:
                what = f"nothing free in your times at the drop{' for ' + r['note'] if r.get('note') else ''}"
            at.add_row(_ago(r.get("at")), r["name"], what)
        con.print(at)


def manage_menu() -> None:
    """My reservations: the history screen, then pick one to change it."""
    while True:
        reservations_view()
        cfg = store.load_config()
        targets = current_targets(cfg)
        pick = ask(q.select("Change one of the waiting ones?" if targets else "", style=STYLE, choices=[
            *[Choice(f"{t.name}  ·  {service.describe(t)}", t.name) for t in targets], back_choice()]))
        if not pick:
            return
        edit_menu(cfg, next(t for t in targets if t.name == pick))   # returns on ← Back


def edit_menu(cfg, t) -> None:
    """One restaurant's settings. Stays here after each change; ← Back returns to the list."""
    while True:
        cfg = store.load_config()
        t = next((x for x in cfg.targets if x.name == t.name), None) if cfg else None
        if t is None:
            return
        if _edit_once(cfg, t) is BACK:
            return


def _edit_once(cfg, t):
    raw = store.load_raw()
    tz = raw["timezone"]
    field = ask(q.select(f"{t.name}:", style=STYLE, choices=[
        Choice(f"Book it / Off  (now: {MODE_PLAIN[mode_of(t)]})", "toggle"),
        Choice(f"Dates  ({', '.join(map(str, t.dates))})", "dates"),
        Choice(f"Times  ({t.time_window[0]:%H:%M}–{t.time_window[1]:%H:%M})", "window"),
        Choice(f"Fallback times  ({'none' if not t.fallback_window else f'{t.fallback_window[0]:%H:%M}–{t.fallback_window[1]:%H:%M}'})", "fallback"),
        Choice(f"Party size  ({t.party_size})", "party"),
        Choice(f"Seating  ({', '.join(t.seat_types) or 'any'})", "seats"),
        Choice(f"Release rule  ({t.release})", "release"),
        Choice(f"Max deposit  (${t.max_deposit_usd:.0f})", "deposit"),
        Choice(f"Booking mode  ({t.book_mode})", "mode"),
        Choice(f"Name  ({t.name})", "name"),
        Choice("Check what's open now", "check"),
        Choice("Remove this restaurant", "remove"),
        back_choice(),
    ]))
    if field is None:
        raise KeyboardInterrupt
    if not field:
        return BACK
    if field == "toggle":
        return set_mode(cfg, t)
    if field == "check":
        return check_one(cfg, t)
    if field == "remove":
        if ask(q.confirm(f"Remove {t.name}?", default=False, style=STYLE)):
            if t.armed and t.live:
                drop_notify(t, cfg)
            store.remove_target(t.name)
            con.print("[green]✓ Removed.[/]")
            return BACK
        return
    patch: dict = {}
    if field == "dates":
        d = _ask_dates(t.release, tz)
        if d:
            patch["dates"] = d
    elif field == "window":
        s = ask(q.text("Times:", default=f"{t.time_window[0]:%H:%M}-{t.time_window[1]:%H:%M}", style=STYLE,
                       validate=_valid(service.parse_window)))
        if s:
            patch["time_window"] = service.parse_window(s)
    elif field == "fallback":
        s = ask(q.text("Fallback times (what you'd settle for after a few seconds; blank = none):", style=STYLE,
                       validate=lambda s: not s or _valid(service.parse_window)(s)))
        if text(s):
            patch["fallback_window"] = service.parse_window(s) if s else None
            if s:
                secs = ask(q.text("Settle after how many seconds?", default=str(int(t.fallback_after)), style=STYLE,
                                  validate=lambda x: x.replace(".", "", 1).isdigit() or "a number"))
                if secs:
                    patch["fallback_after"] = float(secs)
    elif field == "party":
        s = ask(q.text("Party size:", default=str(t.party_size), style=STYLE,
                       validate=lambda s: s.isdigit() and 1 <= int(s) <= 20 or "1–20"))
        if s:
            patch["party_size"] = int(s)
    elif field == "seats":
        s = ask(q.text("Seating, best first (blank = any):", default=", ".join(t.seat_types), style=STYLE))
        if text(s):
            patch["seat_types"] = [x.strip() for x in s.split(",") if x.strip()]
    elif field == "release":
        r = _ask_rule(None)
        if r:
            patch["release"] = r
    elif field == "deposit":
        s = ask(q.text("Max deposit in $ (0 = refuse any deposit):", default=str(int(t.max_deposit_usd)), style=STYLE,
                       validate=lambda s: s.replace(".", "", 1).isdigit() or "a number"))
        if s:
            patch["max_deposit_usd"] = float(s)
    elif field == "mode":
        m = ask(q.select("Booking mode:", style=STYLE, choices=[
            Choice("auto: the bot books it", "auto"),
            Choice("manual: alert me with a link (captcha venues)", "manual"), back_choice()]))
        if m:
            patch["book_mode"] = m
    elif field == "name":
        s = ask(q.text("Name:", default=t.name, style=STYLE))
        if s and s != t.name:
            patch["name"] = s
    if patch:
        store.update_target(t.name, **patch)
        con.print("[green]✓ Saved.[/]")
        if "name" in patch:
            t.name = patch["name"]          # keep editing it under its new name


def _practice(c, venue, d, rule, tz) -> None:
    """Trial run with exactly what you picked. Uses a throwaway entry, deleted afterwards."""
    from datetime import date as _date
    days = [_date.fromisoformat(x) for x in d["dates"]]
    if service.ReleaseRule(rule, tz).drops_for(days):      # those dates aren't out yet
        with con.status("Those dates aren't out yet. Finding a date that's open now…"):
            day = service.latest_open_date(c, venue["id"], d["party"])
        if not day:
            con.print(f"[yellow]{venue['name']} has no open dates right now to practise on.[/]")
            return
        con.print(f"[dim]Your date isn't released yet, so practising on {day:%a %b %-d} instead.[/]")
        days = [day]

    t = service.new_target(name=f"Practice: {venue['name']}", venue=venue, dates=[x.isoformat() for x in days],
                           window=d["window"], release="now", party=d["party"], seats=d["seats"],
                           live=False, armed=False, book_mode=d["book_mode"], max_deposit=d["deposit"])
    if d["fallback"]:
        t["fallback_window"] = d["fallback"]
    name, n = t["name"], 2
    while store.find(store.load_raw(), name):
        name, n = f"{t['name']} {n}", n + 1
    t["name"] = name
    store.add_target(t)
    try:
        run = Scheduler(Notifier(quiet=True)).run_now(name, live=False)     # no alerts, no booking
        with con.status("Practising…"):
            run.thread.join()
    finally:
        store.remove_target(name)

    would = found = [run.slot] if run.slot else []
    if run.state in ("booked", "found", "missed"):
        onboarding.mark("practice_done")
    if run.state == "booked":
        con.print("[green]✓ Practice worked.[/] It stopped right before booking."
                  + (f"\n  It would have booked: [bold]{_pretty_slot(would[-1])}[/]" if would else ""))
    elif run.state == "found":
        con.print("[green]✓ Practice worked.[/] A table was open"
                  + (f": [bold]{_pretty_slot(found[0])}[/]" if found else "")
                  + ".\n  This restaurant has a captcha, so for real the bot would open the Resy page "
                    "and play a sound for you to click.")
    elif run.state == "missed":
        con.print("[yellow]Practice ran fine, but nothing was open in your times on that date.[/] "
                  "That's a real result, not an error. Try other times or another date.")
    else:
        con.print(f"[red]Practice hit a problem:[/] {run.state} {run.detail}")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings_menu() -> None:
    """Stays in Settings after each action; ← Back returns to the main menu."""
    while True:
        if _settings_once() is BACK:
            return


# ---------------------------------------------------------------------------
# First-run checklist
# ---------------------------------------------------------------------------

def _resy_account() -> dict | None:
    """Your Resy profile if the login works, else None (no prompts)."""
    try:
        c = service.client()
    except SystemExit:
        return None
    try:
        return c.whoami()
    except ResyError:
        return None
    finally:
        c.close()


def checklist_items(me: dict | None = None, check_resy: bool = False) -> list[dict]:
    st = onboarding.load()
    if check_resy:
        me = _resy_account()
        st["_resy_ok"], st["_card"] = bool(me), bool(me and me.get("payment_methods"))
        onboarding.mark("_resy_ok", st["_resy_ok"])
        onboarding.mark("_card", st["_card"])
    cfg = store.load_config()
    booked_before = bool(cfg and cfg.targets) or any(r["kind"] == "booked" for r in history.load())
    return [
        {"key": "login", "title": "Log in to Resy", "done": bool(st.get("_resy_ok"))},
        {"key": "card", "title": "Save a card in your Resy account", "done": bool(st.get("_card"))},
        {"key": "practice", "title": "Try a practice run", "done": bool(st.get("practice_done"))},
        {"key": "notify", "title": "Turn on Resy's Notify emails", "done": bool(st.get("notify_emails"))},
        {"key": "gmail", "title": "Connect Gmail (books cancellations for you)",
         "done": bool(mailwatch.creds() or st.get("gmail_skipped")),
         "note": "skipped" if st.get("gmail_skipped") and not mailwatch.creds() else ""},
        {"key": "book", "title": "Book your first restaurant", "done": booked_before},
    ]


STEPS = {
    "login": ("Log in with the email and password you use for Resy. Your password is stored in "
              "your Mac's Keychain (never in a file), and the bot uses it to log itself back in "
              "whenever Resy's login expires.",
              [("Log in now", "do")]),
    "card": ("Resy needs a card on file to hold reservations (most don't charge it).\n\n"
             "  1. Open the Resy app (or resy.com) and go to your Profile\n"
             "  2. Payment Methods → Add a card\n"
             "  3. Come back here and pick “I've added it”",
             [("I've added it, check again", "recheck")]),
    "practice": ("A practice run goes through Book a restaurant exactly as normal (pick a restaurant, "
                 "date and times) but stops right before booking and tells you which table it would "
                 "have taken. Nothing is reserved.",
                 [("Start a practice run now", "do")]),
    "notify": ("When a sold-out date gets a cancellation, Resy Notify tells you by email. The bot "
               "watches for that email, so Resy has to send it.\n\n"
               "  1. Open the Resy app → Profile → notification settings\n"
               "  2. Turn on email for Notify\n"
               "  3. Come back here and pick “Done”",
               [("Done, it's on", "mark")]),
    "gmail": ("So the bot can see Resy's “a table opened” email the instant it arrives and book it. "
              "It only opens emails from Resy and never marks anything read.\n\n"
              "You need a Gmail app password (your normal password won't work):\n"
              "  1. Turn on 2-Step Verification: myaccount.google.com/security\n"
              "     (it only asks for the extra step when you sign in on a new device)\n"
              "  2. Make an app password: myaccount.google.com/apppasswords, name it “resybot”\n"
              "  3. Pick “Connect now” and paste the 16 letters\n\n"
              "Skip it and Resy still alerts your phone; you book cancellations yourself.",
              [("Connect now", "do"), ("Skip, I'll book cancellations myself", "skip")]),
    "book": ("Pick a restaurant, a date and your times, then Go. The bot books at the drop, books "
             "right away if a table's free, or signs you up for Resy Notify. Say yes when it offers "
             "to turn on the bot, and keep the Mac plugged in with the lid open at drop time.",
             [("Book a restaurant now", "do")]),
}


def checklist() -> None:
    """Getting-started list. Pick an item to see how; disappears once everything is done."""
    first = True
    while True:
        with con.status("Checking your setup…") if first else _null():
            items = checklist_items(check_resy=True)
        first = False
        done = sum(i["done"] for i in items)
        if done == len(items):
            onboarding.mark("complete")
            con.print(Panel("[green]✓ All set![/] Everything's ready, so this checklist won't show again.",
                            border_style="green"))
            return
        con.print(Panel.fit(f"[bold]Getting started[/]  ·  {done} of {len(items)} done\n"
                            "[dim]Pick an item to see how. This goes away once everything is ticked.[/]",
                            border_style="#f97316"))
        choices = []
        for i in items:
            mark = "[✓]" if i["done"] else "[ ]"
            note = f"  ({i['note']})" if i.get("note") else ""
            label = f"{mark} {i['title']}{note}"
            choices.append(Choice(label, i["key"]) if not i["done"]
                           else Choice(_Styled([("fg:#22c55e", mark), ("class:text", f" {i['title']}{note}")]),
                                       i["key"]))
        choices.append(Choice("Continue to the main menu →", "_go"))
        default = next((i["key"] for i in items if not i["done"]), "_go")
        pick = ask(q.select("Checklist:", style=STYLE, choices=choices, default=default))
        if not pick or pick == "_go":
            return
        _checklist_item(next(i for i in items if i["key"] == pick))


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _checklist_item(item: dict) -> None:
    key = item["key"]
    text, actions = STEPS[key]
    con.print(Panel(text, title=item["title"], border_style="green" if item["done"] else "#f97316"))
    if item["done"] and key != "gmail":
        con.print("[green]✓ Already done.[/]")
    act = ask(q.select("", style=STYLE, choices=[*[Choice(a, v) for a, v in actions], back_choice()]))
    if not act:
        return
    if act == "mark":
        onboarding.mark("notify_emails")
    elif act == "skip":
        onboarding.mark("gmail_skipped")
    elif act == "recheck":
        pass                                   # the list re-checks Resy when it comes back
    elif key == "login":
        _safe_login()
    elif key == "practice":
        practice_run()
    elif key == "gmail":
        gmail_flow(mailwatch.creds())
    elif key == "book":
        book_flow()


def _settings_once():
    raw = store.load_raw()
    email = os.environ.get("RESY_EMAIL")
    saved = bool(email and auth.get_password(email))
    running = background.status()["running"]
    gmail = mailwatch.creds()
    pick = ask(q.select("Settings:", style=STYLE, choices=[
        *([Choice("Watch the bot (live countdown + log)", "watch"), Choice("Turn off the bot", "stop")] if running
          else [Choice("Turn on the bot", "start")]),
        Choice(f"Gmail for Resy Notify  ({gmail[0] + ', connected' if gmail else 'not connected'})", "gmail"),
        Choice("Test Resy login", "test"),
        Choice(f"Log in with email & password{f'  (saved for {email})' if saved else '  (recommended)'}", "login"),
        *([Choice("Forget my saved password", "forget")] if saved else []),
        Choice("Paste a new Resy token (the DevTools way)", "token"),
        Choice(f"Default party size  ({raw['party_size']})", "party"),
        back_choice(),
    ]))
    if pick is None:
        raise KeyboardInterrupt
    if not pick:
        return BACK
    if pick == "watch":
        watch_background()
    elif pick == "stop":
        if ask(q.confirm("Turn off the bot? Nothing will book until you turn it on again.", default=False, style=STYLE)):
            background.stop()
            con.print("[yellow]Bot is off.[/]")
    elif pick == "start":
        if not upcoming_armed():
            con.print("[yellow]No restaurant is set to Book it with an upcoming drop.[/]")
        else:
            st = background.start()
            con.print(f"[green]✓ Bot is on[/] (pid {st['pid']}).")
    elif pick == "gmail":
        gmail_flow(gmail)
    elif pick == "login":
        login_flow()
    elif pick == "forget":
        auth.forget(email)
        con.print("[green]✓ Removed your password from the Keychain.[/] The current token keeps working until it expires.")
    elif pick == "test":
        c = service.client()
        try:
            con.print(f"[green]✓[/] {service.whoami_line(c)}")
        finally:
            c.close()
    elif pick == "token":
        con.print("[dim]On resy.com (logged in): DevTools → Network → any api.resy.com request → "
                  "Request Headers → x-resy-auth-token[/]")
        tok = ask(q.password("X-Resy-Auth-Token:", style=STYLE))
        if tok:
            ok, msg = service.try_token(tok.strip())
            if ok:
                store.set_secret("RESY_AUTH_TOKEN", tok.strip())
                con.print(f"[green]✓ Saved and working:[/] {msg}")
            else:
                con.print(f"[red]✗ Resy rejected it, so I kept the old token.[/] {msg}")
    elif pick == "party":
        s = ask(q.text("Default party size:", default=str(raw["party_size"]), style=STYLE,
                       validate=lambda s: s.isdigit() and 1 <= int(s) <= 20 or "1–20"))
        if s:
            store.update_settings(party_size=int(s))



# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def gmail_flow(gmail) -> None:
    if gmail:
        pick = ask(q.select(f"Gmail ({gmail[0]}) is connected.", style=STYLE, choices=[
            Choice("Test it", "test"), Choice("Disconnect", "off"), back_choice()]))
        if pick == "test":
            err = mailwatch.test_login(*gmail)
            con.print("[green]✓ Gmail works.[/]" if not err else f"[red]✗ {err}[/]")
        elif pick == "off":
            mailwatch.forget()
            con.print("[yellow]Disconnected.[/] Resy Notify alerts will just go to your phone/email.")
        return
    con.print(Panel(
        "When a table opens, Resy emails you. With Gmail connected, the bot sees that email "
        "the instant it arrives and books the table. It only opens emails from Resy, never marks "
        "anything read, and never checks Resy while it waits.\n\n"
        "You need a Gmail [bold]app password[/] (not your normal password):\n"
        "  1. Turn on 2-Step Verification: myaccount.google.com/security\n"
        "  2. Make an app password: myaccount.google.com/apppasswords (name it \"resybot\")\n"
        "  3. Paste the 16 letters below. It's stored in your Mac's Keychain.",
        title="Connect Gmail", border_style="#f97316"))
    addr = ask(q.text("Gmail address (the one your Resy account uses):",
                      default=os.environ.get("RESY_EMAIL", ""), style=STYLE, instruction="(Esc = back)",
                      validate=lambda s: "@" in s or "an email address"))
    if not addr:
        return
    pw = ask(q.password("App password (16 letters):", style=STYLE))
    if not pw:
        return
    with con.status("Checking with Gmail…"):
        err = mailwatch.test_login(addr.strip(), pw)
    if err:
        con.print(f"[red]✗ {err}[/]")
        return
    mailwatch.save(addr.strip(), pw)
    con.print("[green]✓ Gmail connected.[/] The bot will book Resy Notify tables for you.")
    if background.status()["running"]:
        background.stop()
        background.start()
        con.print("[dim]Restarted the bot so it starts listening.[/]")


def login_flow() -> bool:
    con.print("[dim]Your password goes into the macOS Keychain, never into a file. When Resy's token "
              "expires, the bot logs in again by itself.[/]")
    email = ask(q.text("Resy email:", default=os.environ.get("RESY_EMAIL", ""), style=STYLE,
                       validate=lambda s: "@" in s or "enter your email"))
    if not email:
        return False
    password = ask(q.password("Resy password:", style=STYLE))
    if not password:
        return False
    from .config import PUBLIC_API_KEY
    api_key = os.environ.get("RESY_API_KEY") or PUBLIC_API_KEY
    with con.status("Logging in…"):
        body = auth.login(email.strip(), password, api_key)
    auth.save_password(email.strip(), password)
    store.set_secret("RESY_EMAIL", email.strip())
    store.set_secret("RESY_AUTH_TOKEN", body["token"])
    con.print(f"[green]✓ Logged in as {body.get('first_name', '')} {body.get('last_name', '')}.[/] "
              "Saved. You won't need to do this again unless you change your password.")
    return True


def startup_login_check() -> None:
    """On launch: is the login good? If not, fix it now, not at the drop."""
    try:
        c = service.client()
    except SystemExit:
        con.print("[yellow]No Resy login yet.[/]")
        if ask(q.confirm("Log in with your Resy email & password now?", default=True, style=STYLE)):
            _safe_login()
        return
    try:
        with con.status("Checking your Resy login…"):
            line = service.whoami_line(c)
        auto = "  [dim](auto-renews)[/]" if auth.can_auto_login() else \
               "  [dim](tip: Settings → “Log in with email & password” so it renews itself)[/]"
        con.print(f"[green]●[/] Resy: {line}{auto}")
    except ResyError as e:
        con.print(f"[red]● Resy login isn't working:[/] {e}")
        if ask(q.confirm("Log in with your Resy email & password now?", default=True, style=STYLE)):
            _safe_login()
    finally:
        c.close()


def _safe_login() -> None:
    try:
        login_flow()
    except ResyError as e:
        con.print(f"[red]✗ {e}[/]")
