"""
First-run checklist state: .resybot-state.json (per install, never shared).

Most items are detected automatically (Resy login, card, Gmail, a booking);
the rest are ticked when you do them (practice run) or say they're done
(Resy's Notify emails). Once everything is ticked, `complete` is set and the
checklist never shows again.
"""
from __future__ import annotations

import json
from pathlib import Path

STATE = Path(".resybot-state.json")


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def mark(key: str, value=True) -> None:
    st = load()
    st[key] = value
    try:
        STATE.write_text(json.dumps(st, indent=2))
    except OSError:
        pass


def complete() -> bool:
    return bool(load().get("complete"))
