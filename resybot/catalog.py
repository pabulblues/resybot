"""
The restaurant catalog: restaurants.yaml.

A list of restaurants you might want, with everything needed to make a
target from one in a few keystrokes: Resy venue id, link, city, release
rule and captcha. Unlike targets.yaml (what you're actively booking, with
dates), the catalog is just reference data.

    python -m resybot catalog import names.txt    # resolve names on Resy, add to catalog
    python -m resybot catalog refresh             # re-check release rules

Matching is strict on purpose: a wrong match would snipe the wrong
restaurant. Exact Resy slug first, then a search hit whose name matches
exactly; anything else is reported as not found instead of guessed.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .release import infer_rule
from .resy import ResyClient, ResyError

log = logging.getLogger("resybot.catalog")

CATALOG = Path("restaurants.yaml")
PAUSE_S = 0.5          # between Resy requests: be gentle

# Resy location slug + map point per city.
CITIES = {
    "new york": ("new-york-ny", 40.7128, -74.0060),
    "miami": ("miami-fl", 25.7617, -80.1918),
    "chicago": ("chicago-il", 41.8781, -87.6298),
    "washington": ("washington-dc", 38.9072, -77.0369),
    "london": ("london-england", 51.5072, -0.1276),
    "paris": ("paris-france", 48.8566, 2.3522),
    "cannes": ("cannes-france", 43.5528, 7.0174),
    "jersey city": ("jersey-city-nj", 40.7178, -74.0431),
    "sea bright": ("sea-bright-nj", 40.3615, -73.9740),
    "kuwait city": ("kuwait-city-kuwait", 29.3759, 47.9774),
}
CITY_WORDS = {"miami": "miami", "london": "london", "mayfair": "london", "cannes": "cannes",
              "paris": "paris", "chicago": "chicago", "dc": "washington", "kuwait": "kuwait city",
              "jersey": "jersey city"}


# ---- file -----------------------------------------------------------------------

def load(path: Path = CATALOG) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("restaurants", [])


def save(items: list[dict], path: Path = CATALOG) -> None:
    items = sorted(items, key=lambda r: r["name"].lower())
    header = ("# Restaurant catalog: reference data for adding targets quickly.\n"
              "# `release` is read from Resy's own release stamps; treat it as a strong hint.\n\n")
    path.write_text(header + yaml.safe_dump({"restaurants": items}, sort_keys=False, allow_unicode=True, width=100))


# ---- matching ------------------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    drop = {"the", "restaurant", "nyc", "ny", "new", "york", "by"}
    return " ".join(w for w in s.split() if w not in drop)


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def guess_city(name: str) -> str:
    low = name.lower()
    for word, city in CITY_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return city
    return "new york"


def resolve(c: ResyClient, name: str, hint_slug: str | None = None) -> dict | None:
    """Find `name` on Resy. Returns {id, name, url_slug, city, ...} or None."""
    city = guess_city(name)
    loc, lat, lng = CITIES[city]
    target = norm(name)

    # 1. Exact slugs: the hint, the name slugified, and the name without a city suffix.
    base = re.sub(r"\s*[-–]?\s*\b(nyc|new york|miami|london|cannes|paris|mayfair)\b\s*$", "", name, flags=re.I)
    for slug in dict.fromkeys(s for s in (hint_slug, slugify(name), slugify(base)) if s):
        time.sleep(PAUSE_S)
        try:
            v = c.venue_by_slug(slug, loc)
        except ResyError:
            continue
        vid = (v.get("id") or {}).get("resy") if isinstance(v.get("id"), dict) else v.get("id")
        if vid and _names_match(target, norm(v.get("name") or "")):
            return _record(v, vid, slug, loc)

    # 2. Search, accepting only an exact (normalized) name match in the right city.
    time.sleep(PAUSE_S)
    try:
        body = c._request("POST", "/3/venuesearch/search", json={
            "geo": {"latitude": lat, "longitude": lng}, "query": base or name,
            "types": ["venue"], "per_page": 8}, retries=1)
    except ResyError:
        return None
    for hit in (body.get("search") or {}).get("hits") or []:
        if _names_match(target, norm(hit.get("name") or "")) and _city_ok(city, hit):
            vid = (hit.get("id") or {}).get("resy")
            if vid:
                return _record(hit, vid, hit.get("url_slug"), loc)
    return None


def _names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # "carbone miami" vs "carbone" (city stripped) / "jua" vs "jua"
    strip = lambda s: " ".join(w for w in s.split() if w not in CITY_WORDS)   # noqa: E731
    return strip(a) == strip(b) and len(strip(a)) >= 3


def _city_ok(city: str, hit: dict) -> bool:
    where = f"{hit.get('locality', '')} {hit.get('region', '')}".lower()
    want = {"new york": ("new york", "brooklyn", "queens", " ny"), "washington": ("washington", " dc")}
    return any(w in where for w in want.get(city, (city,)))


def _record(v: dict, vid, slug, loc) -> dict:
    loc_obj = v.get("location") if isinstance(v.get("location"), dict) else {}
    return {
        "name": v.get("name"),
        "venue_id": int(vid),
        "url_slug": slug or v.get("url_slug"),
        "city": loc_obj.get("url_slug") or loc,
        "neighborhood": v.get("neighborhood") or loc_obj.get("neighborhood"),
        "cuisine": ", ".join(v["cuisine"]) if isinstance(v.get("cuisine"), list) else v.get("cuisine") or v.get("type"),
    }


# ---- facts that need availability -----------------------------------------------------

def probe(c: ResyClient, venue_id: int, tz: str = "America/New_York", party: int = 2) -> dict:
    """
    Release rule + captcha in two requests:
      1. /4/venue/calendar → last_calendar_day = how far ahead the books open,
         and the latest date that has tables.
      2. /4/find on that date → Resy's release stamp (market.date.on) gives
         the time of day, and the venue block says whether there's a captcha.
    """
    today = datetime.now(ZoneInfo(tz)).date()
    out: dict = {"release": None, "release_days": None, "captcha": None}
    time.sleep(PAUSE_S)
    try:
        cal = c._request("GET", "/4/venue/calendar", params={
            "venue_id": venue_id, "num_seats": party, "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=90)).isoformat()}, retries=1)
    except ResyError:
        return out
    last = cal.get("last_calendar_day")
    if last:
        out["release_days"] = (date.fromisoformat(last) - today).days
    open_days = [d["date"] for d in cal.get("scheduled") or []
                 if (d.get("inventory") or {}).get("reservation") == "available"]
    probe_day = open_days[-1] if open_days else last
    if not probe_day:
        return out
    time.sleep(PAUSE_S)
    try:
        body = c._find_raw(venue_id, date.fromisoformat(probe_day), party)
    except ResyError:
        return out
    venues = (body.get("results") or {}).get("venues") or []
    if venues:
        out["captcha"] = bool((venues[0].get("venue") or {}).get("feature_recaptcha"))
    rules: Counter[str] = Counter()
    for v in venues:
        for s in v.get("slots") or []:
            on = ((s.get("market") or {}).get("date") or {}).get("on")
            start = (s.get("date") or {}).get("start", "")
            if on and start:
                rules[infer_rule(date.fromisoformat(start[:10]), on, tz)] += 1
    if rules:
        out["release"] = rules.most_common(1)[0][0]
    return out


# ---- import -------------------------------------------------------------------------------

def import_names(c: ResyClient, entries: list[tuple[str, str | None]], *, source: str,
                 on_progress=None) -> tuple[list[dict], list[str]]:
    """Resolve each (name, hint_slug) and merge into the catalog. Returns (added, not_found)."""
    items = load()
    known = {r["venue_id"] for r in items}
    added, missing = [], []
    for i, (name, hint) in enumerate(entries, 1):
        rec = resolve(c, name, hint)
        if not rec:
            missing.append(name)
            if on_progress:
                on_progress(i, len(entries), name, None)
            continue
        if rec["venue_id"] in known:
            if on_progress:
                on_progress(i, len(entries), name, "already in catalog")
            continue
        rec.update(probe(c, rec["venue_id"]))
        rec["source"] = source
        rec["checked"] = date.today().isoformat()
        items.append(rec)
        known.add(rec["venue_id"])
        added.append(rec)
        save(items)            # save as we go: an interruption loses nothing
        if on_progress:
            on_progress(i, len(entries), name, rec)
    return added, missing


# ---- live search (anything on Resy, not just the catalog) ---------------------------

def search(c: ResyClient, query: str, limit: int = 10) -> list[dict]:
    """Search all of Resy by name. Returns catalog-shaped records (no release facts)."""
    city = guess_city(query)
    loc, lat, lng = CITIES[city]
    body = c._request("POST", "/3/venuesearch/search", json={
        "geo": {"latitude": lat, "longitude": lng}, "query": query,
        "types": ["venue"], "per_page": limit}, retries=1)
    out = []
    for hit in (body.get("search") or {}).get("hits") or []:
        vid = (hit.get("id") or {}).get("resy")
        if not vid:
            continue
        rec = _record(hit, vid, hit.get("url_slug"), loc)
        rec["where"] = ", ".join(x for x in (hit.get("neighborhood"), hit.get("locality")) if x)
        out.append(rec)
    return out
