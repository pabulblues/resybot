"""
Loads and validates targets.yaml.

Everything the bot needs to know about *what* you want is in here.
Everything about *how* to talk to Resy is in resy.py. Keep them apart.

Validation happens once at startup so a typo in the YAML fails loudly
right away, not silently at 10:00:00 on drop day.
"""
from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Secrets (from .env, never from targets.yaml)
# ---------------------------------------------------------------------------

class Secrets(BaseModel):
    """Loaded from environment variables / .env. Never logged."""
    resy_auth_token: str
    resy_api_key: str
    ntfy_topic: Optional[str] = None

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> "Secrets":
        load_dotenv(dotenv_path)
        try:
            return cls(
                resy_auth_token=os.environ["RESY_AUTH_TOKEN"],
                resy_api_key=os.environ["RESY_API_KEY"],
                ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
            )
        except KeyError as e:
            raise SystemExit(
                f"Missing {e.args[0]} in .env — see README section 'Getting your token'."
            )

    def __repr__(self) -> str:  # make sure a stray print() never leaks the token
        return "Secrets(<redacted>)"


# ---------------------------------------------------------------------------
# One restaurant you want
# ---------------------------------------------------------------------------

class Target(BaseModel):
    name: str
    venue_id: int
    dates: list[date] = Field(min_length=1)
    time_window: tuple[time, time]
    fallback_window: Optional[tuple[time, time]] = None
    fallback_after: float = Field(default=8.0, ge=0)  # seconds after drop
    seat_types: list[str] = []                           # [] = accept any
    release: str                                         # parsed by release.py
    max_deposit_usd: float = Field(default=0.0, ge=0)    # 0 = refuse any deposit
    party_size: Optional[int] = None                     # overrides global
    # "auto"   = bot books the slot itself.
    # "manual" = bot finds the slot and pings your phone with a link; YOU
    #            tap it and confirm on resy.com. Use for venues with
    #            feature_recaptcha: true (the `check` command shows this).
    book_mode: str = Field(default="auto", pattern="^(auto|manual)$")
    url_slug: Optional[str] = None                       # e.g. "tatiana"; used to build the link
    city: str = "new-york-ny"                            # resy.com/cities/<city>/venues/<slug>

    # ---- validators -------------------------------------------------------

    @field_validator("time_window", "fallback_window", mode="before")
    @classmethod
    def _parse_window(cls, v):
        """Accept ["18:30", "20:30"] from YAML and turn it into (time, time)."""
        if v is None:
            return v
        if not (isinstance(v, (list, tuple)) and len(v) == 2):
            raise ValueError("must be a two-item list like ['18:30', '20:30']")
        return tuple(time.fromisoformat(str(x)) for x in v)

    @model_validator(mode="after")
    def _windows_make_sense(self):
        start, end = self.time_window
        if start >= end:
            raise ValueError(f"{self.name}: time_window start must be before end")
        if self.fallback_window:
            fs, fe = self.fallback_window
            if fs >= fe:
                raise ValueError(f"{self.name}: fallback_window start must be before end")
            if not (fs <= start and fe >= end):
                raise ValueError(
                    f"{self.name}: fallback_window must contain time_window "
                    "(it's the *looser* set of times you'd accept)"
                )
        return self

    # ---- helpers ----------------------------------------------------------

    def window_for(self, seconds_since_drop: float) -> tuple[time, time]:
        """Which window applies right now? Preferred first, then fallback."""
        if self.fallback_window and seconds_since_drop >= self.fallback_after:
            return self.fallback_window
        return self.time_window


# ---------------------------------------------------------------------------
# Whole file
# ---------------------------------------------------------------------------

class Config(BaseModel):
    party_size: int = Field(ge=1, le=20)
    timezone: str = "America/New_York"   # release rules are read in this tz
    poll_interval_ms: int = Field(default=250, ge=100)
    poll_duration_s: int = Field(default=90, ge=5)
    targets: list[Target] = Field(min_length=1)

    @model_validator(mode="after")
    def _fill_party_sizes(self):
        for t in self.targets:
            if t.party_size is None:
                t.party_size = self.party_size
        return self

    @classmethod
    def load(cls, path: Path | str = "targets.yaml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise SystemExit(f"No {path} found. Copy targets.example.yaml to get started.")
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
        return cls.model_validate(raw)
