"""
Reservation history: history.jsonl, one line per thing that happened.

    booked   the bot made a reservation (reservation number saved)
    alert    captcha restaurant: a table was open and the page was opened for you
    missed   the drop came and nothing in your times was free

Practice runs are never recorded. Written by the sniper, the cancellation
watcher and the scheduler; read by the "My reservations" screen.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("resybot.history")

HISTORY = Path("history.jsonl")
_lock = threading.Lock()


def record(kind: str, name: str, *, slot: str | None = None, reservation: str | None = None,
           party: int | None = None, url: str | None = None, note: str | None = None) -> None:
    if reservation == "DRY-RUN":
        return
    row = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "name": name}
    for k, v in (("slot", slot), ("reservation", reservation), ("party", party), ("url", url), ("note", note)):
        if v is not None:
            row[k] = v
    try:
        with _lock, HISTORY.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:          # history must never break a booking
        log.warning("couldn't write history: %s", e)


def load() -> list[dict]:
    if not HISTORY.exists():
        backfill()
    rows = []
    try:
        for line in HISTORY.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def slot_when(slot: str | None) -> datetime | None:
    """'2026-12-11 18:00 Dining Room (Dining Room)' → datetime(2026-12-11 18:00)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", slot or "")
    return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}") if m else None


def slot_place(slot: str | None) -> str:
    """'2026-12-11 18:00 Dining Room (Dining Room)' → 'Dining Room'."""
    rest = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*", "", slot or "")
    return re.sub(r"\s*\(.*\)$", "", rest)


def backfill() -> None:
    """First run: pull past bookings / alerts out of the old logs so history isn't empty."""
    rows, seen_alert = [], set()
    for p in sorted(Path("logs").glob("*.log")) + sorted(Path("logs").glob("*.out")):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = re.search(r"INFO\s+(.+?): BOOKED (?:from a cancellation: )?(.+?) → (\S+)\s*$", line)
            if m and m.group(3) != "DRY-RUN":
                rows.append({"kind": "booked", "name": m.group(1), "slot": m.group(2), "reservation": m.group(3)})
                continue
            m = re.search(r"INFO\s+(.+?): FOUND (.+?) \([\d.]+s after drop\) → (\S+)", line)
            if m and m.group(1) not in seen_alert:
                seen_alert.add(m.group(1))
                rows.append({"kind": "alert", "name": m.group(1), "slot": m.group(2), "url": m.group(3)})
    try:                                                           # party size from your targets
        import yaml
        parties = {t["name"]: t.get("party_size") for t in
                   (yaml.safe_load(Path("targets.yaml").read_text()) or {}).get("targets", [])}
    except Exception:
        parties = {}
    for r in rows:
        if parties.get(r["name"]):
            r["party"] = parties[r["name"]]
    uniq = {json.dumps(r, sort_keys=True): r for r in rows}       # same line in two log files
    try:
        with HISTORY.open("w") as f:
            for r in uniq.values():
                f.write(json.dumps({"at": None, **r}) + "\n")
    except OSError:
        pass
