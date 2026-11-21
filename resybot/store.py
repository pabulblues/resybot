"""
Read and write targets.yaml and .env from code (menus, Telegram).

Every write is validated through Config first, so a bad edit can never
leave a file the bot can't load. Writes are atomic (temp file + rename)
and serialized with a lock because the Telegram thread and the scheduler
thread both touch the file.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml
from dotenv import set_key

from .config import Config

_lock = threading.RLock()

HEADER = (
    "# resybot targets. Edited by the menus / Telegram; hand edits are fine too.\n"
    "# Times are in `timezone`. Dates are YYYY-MM-DD.\n\n"
)

DEFAULT_RAW: dict[str, Any] = {
    "party_size": 2,
    "timezone": "America/New_York",
    "poll_interval_ms": 250,
    "poll_duration_s": 90,
    "targets": [],
}


def load_raw(path: Path | str = "targets.yaml") -> dict:
    path = Path(path)
    with _lock:
        if not path.exists():
            return {**DEFAULT_RAW, "targets": []}
        raw = yaml.safe_load(path.read_text()) or {}
    raw.setdefault("targets", [])
    for k, v in DEFAULT_RAW.items():
        raw.setdefault(k, v)
    # Normalize dates to strings so they round-trip identically.
    for t in raw["targets"]:
        t["dates"] = [str(d) for d in t.get("dates", [])]
    return raw


def validate(raw: dict) -> Config:
    """Raises pydantic.ValidationError (or ValueError) if raw isn't loadable."""
    if not raw.get("targets"):
        # Config requires >= 1 target; an empty file is still valid for us.
        return Config.model_validate({**raw, "targets": [_placeholder()]})
    return Config.model_validate(raw)


def save_raw(raw: dict, path: Path | str = "targets.yaml") -> None:
    validate(raw)
    path = Path(path)
    text = HEADER + yaml.safe_dump(raw, sort_keys=False, default_flow_style=None, allow_unicode=True, width=100)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".targets.", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)


def find(raw: dict, name: str) -> dict | None:
    for t in raw["targets"]:
        if t["name"].lower() == name.lower():
            return t
    return None


def add_target(target: dict, path: Path | str = "targets.yaml") -> None:
    with _lock:
        raw = load_raw(path)
        if find(raw, target["name"]):
            raise ValueError(f"A target named {target['name']!r} already exists.")
        raw["targets"].append(target)
        save_raw(raw, path)


def update_target(name: str, path: Path | str = "targets.yaml", **patch) -> dict:
    with _lock:
        raw = load_raw(path)
        t = find(raw, name)
        if t is None:
            raise KeyError(name)
        t.update(patch)
        save_raw(raw, path)
        return t


def remove_target(name: str, path: Path | str = "targets.yaml") -> None:
    with _lock:
        raw = load_raw(path)
        raw["targets"] = [t for t in raw["targets"] if t["name"].lower() != name.lower()]
        save_raw(raw, path)


def update_settings(path: Path | str = "targets.yaml", **patch) -> None:
    with _lock:
        raw = load_raw(path)
        raw.update(patch)
        save_raw(raw, path)


def load_config(path: Path | str = "targets.yaml") -> Config | None:
    """Config for the current file, or None if it has no targets yet."""
    raw = load_raw(path)
    if not raw["targets"]:
        return None
    return Config.model_validate(raw)


def set_secret(key: str, value: str, env_path: Path | str = ".env") -> None:
    env_path = Path(env_path)
    with _lock:
        env_path.touch(mode=0o600, exist_ok=True)
        set_key(str(env_path), key, value, quote_mode="never")
        os.environ[key] = value


def _placeholder() -> dict:
    return {"name": "_", "venue_id": 1, "dates": ["2000-01-01"], "time_window": ["18:00", "19:00"], "release": "now"}
