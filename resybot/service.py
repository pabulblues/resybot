"""
Operations shared by every interface (classic CLI, menus, Telegram).

Interfaces only prompt and display; the logic lives here so all three
behave identically.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Config, Secrets, Target
from .release import Drop, ReleaseRule, infer_rule
from .resy import ResyClient, ResyError

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def client(dry_run: bool = True) -> ResyClient:
    """A Resy client that logs in again by itself if the token expires
    (when an email + Keychain password are saved)."""
    from . import auth
    try:
        sec = Secrets.from_env()
    except SystemExit:
        if not auth.can_auto_login():
            raise
        auth.refresh_token()          # first run with only email + password
        sec = Secrets.from_env()
    refresh = auth.refresh_token if auth.can_auto_login() else None
    return ResyClient(sec.resy_auth_token, sec.resy_api_key, dry_run=dry_run, refresh=refresh)


# ---------------------------------------------------------------------------
# Parsing what people type
# ---------------------------------------------------------------------------

def parse_time(s: str) -> str:
    """'6pm', '6:30 pm', '18:30', '1830', 'noon' → 'HH:MM'."""
    s = s.strip().lower().replace(".", "")
    if s in ("noon", "midday"):
        return "12:00"
    if s == "midnight":
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm|a|p)?", s)
    if not m:
        raise ValueError(f"Can't read the time {s!r}. Try '6pm' or '18:30'.")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            raise ValueError(f"{s!r}: hour must be 1–12 with am/pm")
        h = h % 12 + (12 if ap.startswith("p") else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"{s!r} isn't a valid time")
    return f"{h:02d}:{mi:02d}"


def parse_window(s: str) -> list[str]:
    """'6pm-9:30pm', '18:00 to 21:30', '6-9pm' → ['18:00', '21:30']."""
    parts = re.split(r"\s*(?:-|–|—|to)\s*", s.strip().lower(), maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Give a range like '6pm-9:30pm' or '18:00-21:30'.")
    a, b = parts
    # '6-9pm' → the am/pm on the end applies to both
    if re.search(r"(am|pm)$", b) and not re.search(r"(am|pm|:)", a):
        a += b[-2:]
    start, end = parse_time(a), parse_time(b)
    if start >= end:
        raise ValueError(f"Start ({start}) must be before end ({end}).")
    return [start, end]


def parse_day(s: str, today: date) -> date:
    """'2026-12-11', '12/11', 'dec 11', '11 dec', 'december 11 2026' → date (next occurrence)."""
    s = s.strip().lower().replace(",", " ")
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return _resolve(today, mo, d, int(y) + (2000 if y and len(y) == 2 else 0) if y else None)
    words = s.split()
    mo = d = y = None
    for w in words:
        if w[:3] in MONTHS and w.isalpha():
            mo = MONTHS[w[:3]]
        elif w.rstrip("stndrh").isdigit():
            n = int(w.rstrip("stndrh"))
            if n > 31:
                y = n
            else:
                d = n
    if mo and d:
        return _resolve(today, mo, d, y)
    raise ValueError(f"Can't read the date {s!r}. Try 'dec 11' or '2026-12-11'.")


def _resolve(today: date, mo: int, d: int, y: int | None) -> date:
    if y:
        return date(y, mo, d)
    cand = date(today.year, mo, d)
    return cand if cand >= today else date(today.year + 1, mo, d)


def parse_dates(s: str, tz: str = "America/New_York") -> list[str]:
    today = datetime.now(ZoneInfo(tz)).date()
    chunks = [c for c in re.split(r"\s*(?:,|;|\band\b|\n)\s*", s.strip()) if c]
    days = sorted({parse_day(c, today) for c in chunks})
    return [d.isoformat() for d in days]


# ---------------------------------------------------------------------------
# Release rules
# ---------------------------------------------------------------------------

def next_release_date(rule: str, tz: str, now: datetime | None = None) -> tuple[date, datetime] | None:
    """The reservation date that opens at the NEXT drop, and when that is."""
    r = ReleaseRule(rule, tz)
    now = now or datetime.now(ZoneInfo(tz))
    if r.kind == "days_ahead":
        for offset in (0, 1):
            open_day = now.date() + timedelta(days=offset)
            at = datetime.combine(open_day, r.at, tzinfo=ZoneInfo(tz))
            if at > now:
                return open_day + timedelta(days=r.days_ahead), at
    if r.kind == "weekly":
        for offset in range(0, 8):
            open_day = now.date() + timedelta(days=offset)
            at = datetime.combine(open_day, r.at, tzinfo=ZoneInfo(tz))
            if open_day.weekday() == r.weekday and at > now:
                return open_day, at   # that whole week opens; first day shown
    if r.kind == "monthly":
        for months in (0, 1):
            y, m = now.year, now.month + months
            if m > 12:
                y, m = y + 1, m - 12
            at = datetime.combine(date(y, m, r.month_day), r.at, tzinfo=ZoneInfo(tz))
            if at > now:
                ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
                return date(ny, nm, 1), at   # the whole next month opens; its 1st shown
    return None


def suggest_rule(c: ResyClient, venue_id: int, party: int, tz: str) -> str | None:
    """
    Guess the venue's release rule from Resy's own release stamps
    (market.date.on) on currently open slots. Probes a few dates ahead and
    takes the most common answer. Treat it as a strong hint.
    """
    today = datetime.now(ZoneInfo(tz)).date()
    seen: Counter[str] = Counter()
    for ahead in (35, 28, 21, 14, 7):
        try:
            slots = c.find(venue_id, today + timedelta(days=ahead), party)
        except ResyError:
            continue
        for s in slots:
            if s.released_at:
                seen[infer_rule(s.day, s.released_at, tz)] += 1
        if sum(seen.values()) >= 6:
            break
    return seen.most_common(1)[0][0] if seen else None


# ---------------------------------------------------------------------------
# Venues
# ---------------------------------------------------------------------------

def venue_from_link(c: ResyClient, link: str) -> dict:
    """
    resy.com link (or slug, or numeric venue id) → {id, name, slug, city}.
    """
    link = link.strip()
    if link.isdigit():
        vi = c.venue_info(int(link), datetime.now().date(), 2)
        return {"id": vi.venue_id, "name": vi.name, "slug": vi.url_slug, "city": vi.city_slug}
    m = re.search(r"resy\.com/cities/([^/]+)/(?:venues/)?([^/?#]+)", link)
    city, slug = (m.group(1), m.group(2)) if m else ("new-york-ny", link.strip("/ "))
    v = c.venue_by_slug(slug, city)
    vid = (v.get("id") or {}).get("resy") if isinstance(v.get("id"), dict) else v.get("id")
    if not vid:
        raise ResyError(f"Resy didn't return a venue id for {slug!r}")
    return {"id": int(vid), "name": v.get("name") or slug, "slug": slug, "city": city}


# ---------------------------------------------------------------------------
# Describing / checking targets
# ---------------------------------------------------------------------------

def next_drop(t: Target, cfg: Config) -> Drop | None:
    try:
        drops = ReleaseRule(t.release, cfg.timezone).drops_for(t.dates)
    except ValueError:
        return None
    return drops[0] if drops else None


def countdown(at: datetime) -> str:
    s = int((at - datetime.now(at.tzinfo)).total_seconds())
    if s <= 0:
        return "now"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {sec}s"


def fmt_drop(at: datetime) -> str:
    return f"{at:%a %b %-d, %-I:%M %p}".replace(":00 ", " ")


def describe(t: Target) -> str:
    seats = " > ".join(t.seat_types) if t.seat_types else "any seat"
    win = f"{t.time_window[0]:%H:%M}–{t.time_window[1]:%H:%M}"
    days = ", ".join(f"{d:%a %b %-d}" for d in t.dates)
    return f"{days} · party {t.party_size} · {win} · {seats}"


def latest_open_date(c: ResyClient, venue_id: int, party: int) -> date | None:
    """The furthest-out date that currently has tables (from Resy's calendar)."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    cal = c._request("GET", "/4/venue/calendar", params={
        "venue_id": venue_id, "num_seats": party, "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=90)).isoformat()}, retries=1)
    days = [d["date"] for d in cal.get("scheduled") or []
            if (d.get("inventory") or {}).get("reservation") == "available"]
    return date.fromisoformat(days[-1]) if days else None


def try_token(auth_token: str) -> tuple[bool, str]:
    """Test a candidate token without saving it."""
    sec = Secrets.from_env()
    c = ResyClient(auth_token, sec.resy_api_key, dry_run=True)
    try:
        return True, whoami_line(c)
    except ResyError as e:
        return False, str(e)
    finally:
        c.close()


def whoami_line(c: ResyClient) -> str:
    me = c.whoami()
    pms = me.get("payment_methods") or []
    card = f"{pms[0].get('type', 'card')} ****{pms[0].get('display', '')}" if pms else "NO CARD ON FILE"
    return f"{me.get('first_name', '')} {me.get('last_name', '')} · {card}"


def check_target(c: ResyClient, cfg: Config, t: Target) -> list[str]:
    """Same information as `resybot check`, as lines of text."""
    out: list[str] = []
    try:
        vi = c.venue_info(t.venue_id, t.dates[0], t.party_size)
        out.append(f"Resy name: {vi.name} · captcha {'ON (use manual mode)' if vi.recaptcha else 'off (bot can book)'}")
        if vi.recaptcha and t.book_mode == "auto":
            out.append("⚠ Captcha venue: set book mode to manual, auto will fail at the book step.")
    except ResyError as e:
        out.append(f"Venue lookup failed: {e}")
    d = next_drop(t, cfg)
    out.append(f"Next drop: {fmt_drop(d.at)} (in {countdown(d.at)}) → {', '.join(map(str, d.dates))}"
               if d else "No upcoming drop: these dates are already released.")
    inferred: set[str] = set()
    for day in t.dates:
        try:
            slots = c.find(t.venue_id, day, t.party_size)
        except ResyError as e:
            out.append(f"{day}: find failed: {e}")
            continue
        shown = ", ".join(f"{s.start:%H:%M} {s.seat_type}" for s in slots[:8])
        out.append(f"{day:%a %b %-d}: {len(slots)} open now{'  ' + shown if shown else ''}")
        for dep, canc in sorted({(s.deposit_usd, s.cancellation_fee_usd) for s in slots}):
            out.append(f"   deposit ${dep:.0f} · late-cancel fee ${canc:.0f}")
        inferred |= {infer_rule(s.day, s.released_at, cfg.timezone) for s in slots if s.released_at}
    for r in sorted(inferred):
        mark = "✓ matches" if r.lower() == t.release.lower() else "← differs from your rule"
        out.append(f"Resy's release stamp suggests '{r}' {mark}")
    return out


def new_target(*, name: str, venue: dict, dates: list[str], window: list[str], release: str,
               party: int | None, seats: list[str], live: bool, armed: bool = True,
               book_mode: str = "auto", max_deposit: float = 0) -> dict:
    t = {
        "name": name,
        "venue_id": venue["id"],
        "dates": dates,
        "time_window": window,
        "seat_types": seats,
        "release": release,
        "max_deposit_usd": max_deposit,
        "book_mode": book_mode,
        "armed": armed,
        "live": live,
    }
    if party:
        t["party_size"] = party
    if venue.get("slug"):
        t["url_slug"] = venue["slug"]
    if venue.get("city"):
        t["city"] = venue["city"]
    return t


def parse_hhmm(s: str) -> time:
    return time.fromisoformat(parse_time(s))
