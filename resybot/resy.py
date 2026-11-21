"""
The ONLY file that talks to Resy.

Resy has no public API. Everything here mirrors what the resy.com website
does in your browser, reconstructed from how the private API has worked
historically. Every request shape is marked  # VERIFY  — before trusting
it, capture the same request in DevTools (README → "Verifying requests")
and compare field by field. When Resy changes something, this is the one
file you fix.

Safety:
  * `book()` refuses to run unless the client was created with
    dry_run=False AND the slot passed its deposit check.
  * Every request and response is appended to logs/resy.jsonl with the
    auth token stripped, so you can debug a failed drop afterwards.
"""
from __future__ import annotations

import json
import logging
import random
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("resybot.resy")

BASE_URL = "https://api.resy.com"

# Headers the website sends. Keeping them realistic matters.  # VERIFY
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data we extract from Resy's responses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """
    One bookable table, as returned by /4/find.
    VERIFIED 2026-10-24 against a live response:
      config.token  "rgs://resy/<venue>/<template>/1/<day>/<day>/<HH:MM:SS>/<size>/<Seat Type>"
      config.type   the SERVICE ("Brunch", "Dinner"), not the seating
      date.start    "2026-11-07 11:30:00"
      payment.deposit_fee / payment.cancellation_fee   dollars or null
      market.date.on   unix time the table was released (→ the venue's release rule)
    """
    venue_id: int
    day: date
    start: time
    seat_type: str            # "Dining Room", "Bar Seat", "Patio" — last segment of the token
    service: str              # "Brunch", "Dinner", ... — config.type
    config_token: str         # needed for /3/details
    party_size: int
    deposit_usd: float = 0.0          # charged at booking
    cancellation_fee_usd: float = 0.0 # charged only if you cancel late / no-show
    released_at: Optional[int] = None # market.date.on (unix seconds)
    raw: dict = field(repr=False, compare=False, hash=False, default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.day} {self.start:%H:%M} {self.service} ({self.seat_type})"


@dataclass(frozen=True)
class SlotDetails:
    """What /3/details tells us right before booking."""
    book_token: str
    deposit_usd: float        # 0 if none
    cancellation_fee_usd: float
    payment_method_id: Optional[int]
    raw: dict = field(repr=False, compare=False, default_factory=dict)


@dataclass(frozen=True)
class Booking:
    reservation_id: Optional[str]
    resy_token: Optional[str]
    raw: dict = field(repr=False, compare=False, default_factory=dict)


@dataclass(frozen=True)
class VenueInfo:
    """Facts about a venue that come back alongside /4/find results."""
    venue_id: int
    name: str
    url_slug: str
    city_slug: str
    recaptcha: bool       # True → the bot cannot complete /3/book here; use book_mode: manual

    def page_url(self, day: date, party_size: int) -> str:
        return (f"https://resy.com/cities/{self.city_slug}/venues/{self.url_slug}"
                f"?date={day.isoformat()}&seats={party_size}")


class ResyError(RuntimeError):
    pass


class BookingRefused(ResyError):
    """Raised when we *could* book but a safety rule said no."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ResyClient:
    def __init__(
        self,
        auth_token: str,
        api_key: str,
        *,
        dry_run: bool = True,
        log_dir: Path | str = "logs",
        timeout_s: float = 4.0,
        refresh: "Callable[[], str] | None" = None,
    ):
        self.dry_run = dry_run
        self._auth_token = auth_token
        # Called on 401/419 to get a fresh token (auth.refresh_token). Optional.
        self._refresh = refresh
        self._log_path = Path(log_dir) / "resy.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        # One persistent HTTP/2 connection: the TLS handshake happens once,
        # at startup, not at the drop moment.
        self._http = httpx.Client(
            base_url=BASE_URL,
            http2=True,
            timeout=timeout_s,
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',         # VERIFY
                "X-Resy-Auth-Token": auth_token,                           # VERIFY
                "X-Resy-Universal-Auth": auth_token,                       # VERIFY
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "User-Agent": _BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "X-Origin": "https://resy.com",
            },
        )

    # ---- low level --------------------------------------------------------

    def _request(self, method: str, path: str, *, retries: int = 3, **kw) -> dict:
        """
        Send a request with retry on 429 / 5xx / network errors.
        This is the 'resilience' piece: at drop time Resy's servers are
        slammed and one failed request must not kill the whole run.
        """
        last_exc: Exception | None = None
        refreshed = False
        for attempt in range(retries + 1):
            t0 = _time.perf_counter()
            try:
                r = self._http.request(method, path, **kw)
                ms = (_time.perf_counter() - t0) * 1000
                body: Any
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                self._jsonl(method, path, kw, r.status_code, body, ms)

                if r.status_code in (401, 419):
                    if self._refresh and not refreshed:
                        refreshed = True
                        self._set_token(self._refresh())   # log in again, then retry this request
                        continue
                    raise ResyError("401 Unauthorized: your Resy login expired. "
                                    "Use “Log in with email & password” in the menu (./resy).")
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                if r.status_code >= 400:
                    raise ResyError(f"{method} {path} → {r.status_code}: {str(body)[:300]}")
                return body
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                last_exc = e
                if attempt == retries:
                    break
                # short, jittered backoff: 0.15s, 0.3s, 0.6s (+ noise)
                _time.sleep(0.15 * (2 ** attempt) + random.uniform(0, 0.05))
        raise ResyError(f"{method} {path} failed after {retries + 1} attempts: {last_exc}")

    def _set_token(self, token: str) -> None:
        self._auth_token = token
        self._http.headers["X-Resy-Auth-Token"] = token
        self._http.headers["X-Resy-Universal-Auth"] = token

    def _jsonl(self, method, path, kw, status, body, ms):
        rec = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "method": method,
            "path": path,
            "params": kw.get("params"),
            "data": kw.get("data") or kw.get("json"),
            "status": status,
            "ms": round(ms, 1),
            "body": body,
        }
        try:
            s = json.dumps(rec, default=str).replace(self._auth_token, "<token>")
            with self._log_path.open("a") as f:
                f.write(s + "\n")
        except Exception:  # logging must never break a booking
            pass

    # ---- account ----------------------------------------------------------

    def whoami(self) -> dict:
        """Confirms the token works. Also returns saved payment methods."""
        return self._request("GET", "/2/user")                            # VERIFY

    def payment_methods(self) -> list[dict]:
        u = self.whoami()
        return u.get("payment_methods") or u.get("payment_method_id") or []  # VERIFY

    # ---- venue ------------------------------------------------------------

    def venue_by_slug(self, slug: str, location: str = "ny") -> dict:
        """
        Resolve the URL slug (resy.com/cities/ny/<slug>) to a venue id.
        Used by `resybot venue <slug>` so you don't have to dig in DevTools.
        """
        return self._request(                                               # VERIFY
            "GET", "/3/venue", params={"url_slug": slug, "location": location}
        )

    # ---- the three calls that matter --------------------------------------

    def venue_info(self, venue_id: int, day: date, party_size: int) -> VenueInfo:
        """
        Facts about the venue, pulled from a /4/find response.
        VERIFIED 2026-10-13: results.venues[0].venue.{id.resy, name, url_slug,
        location.url_slug, feature_recaptcha}.
        """
        body = self._find_raw(venue_id, day, party_size)
        venues = (body.get("results") or {}).get("venues") or []
        if not venues:
            raise ResyError(f"venue {venue_id}: /4/find returned no venue block")
        v = venues[0].get("venue") or {}
        return VenueInfo(
            venue_id=int((v.get("id") or {}).get("resy") or venue_id),
            name=v.get("name") or "?",
            url_slug=v.get("url_slug") or "",
            city_slug=(v.get("location") or {}).get("url_slug") or "new-york-ny",
            recaptcha=bool(v.get("feature_recaptcha")),
        )

    def _find_raw(self, venue_id: int, day: date, party_size: int) -> dict:
        # VERIFIED 2026-10-13: POST /4/find with a JSON body of exactly these fields.
        body = self._request(
            "POST",
            "/4/find",
            json={
                "lat": 0,
                "long": 0,
                "day": day.isoformat(),
                "party_size": party_size,
                "venue_id": venue_id,
            },
            retries=1,  # the sniper loop re-polls anyway; don't waste time here
        )
        return body

    def find(self, venue_id: int, day: date, party_size: int) -> list[Slot]:
        """
        Step 1: what's open? This is the call the sniper hammers.
        """
        body = self._find_raw(venue_id, day, party_size)
        venues = (body.get("results") or {}).get("venues") or []            # VERIFIED
        slots: list[Slot] = []
        for v in venues:
            for s in v.get("slots") or []:
                try:
                    cfg = s["config"]
                    token = cfg["token"]
                    start_dt = datetime.fromisoformat(s["date"]["start"])
                    pay = s.get("payment") or {}
                    slots.append(
                        Slot(
                            venue_id=venue_id,
                            day=start_dt.date(),
                            start=start_dt.time(),
                            seat_type=token.rsplit("/", 1)[-1] or "?",
                            service=cfg.get("type") or "?",
                            config_token=token,
                            party_size=party_size,
                            deposit_usd=float(pay.get("deposit_fee") or 0),
                            cancellation_fee_usd=float(pay.get("cancellation_fee") or 0),
                            released_at=((s.get("market") or {}).get("date") or {}).get("on"),
                            raw=s,
                        )
                    )
                except (KeyError, ValueError, TypeError) as e:
                    log.debug("skipping malformed slot %s: %s", s, e)
        return slots

    def details(self, slot: Slot) -> SlotDetails:
        """
        Step 2: exchange the slot's config token for a one-time book_token.
        Also tells us about deposits so the safety check can run.
        """
        body = self._request(                                               # VERIFY
            "GET",
            "/3/details",
            params={
                "config_id": slot.config_token,
                "day": slot.day.isoformat(),
                "party_size": slot.party_size,
            },
        )
        book_token = (body.get("book_token") or {}).get("value")           # VERIFY
        if not book_token:
            raise ResyError(f"/3/details returned no book_token: {str(body)[:300]}")

        # Deposit: the slot from /4/find already carries payment.deposit_fee
        # (verified). /3/details may repeat it under `payment`; take the larger
        # of the two so the safety check can only get stricter.        # VERIFY
        pay = body.get("payment") or {}
        deposit = max(slot.deposit_usd, float(pay.get("deposit_fee") or 0)) if isinstance(pay, dict) else slot.deposit_usd
        cancel_fee = slot.cancellation_fee_usd

        pm = (body.get("user") or {}).get("payment_methods") or []         # VERIFY
        pm_id = pm[0].get("id") if pm else None

        return SlotDetails(
            book_token=book_token,
            deposit_usd=deposit,
            cancellation_fee_usd=cancel_fee,
            payment_method_id=pm_id,
            raw=body,
        )

    def book(self, slot: Slot, det: SlotDetails, *, max_deposit_usd: float) -> Booking:
        """
        Step 3: the money call. Guarded twice:
          1. dry_run  → logs what it WOULD do and returns a fake Booking.
          2. deposit  → refuses if the slot's deposit exceeds your cap.
        """
        if det.deposit_usd > max_deposit_usd:
            raise BookingRefused(
                f"{slot.label}: deposit ${det.deposit_usd:.2f} exceeds max_deposit_usd={max_deposit_usd}"
            )
        if self.dry_run:
            log.warning("DRY RUN — would book %s (deposit $%.2f)", slot.label, det.deposit_usd)
            return Booking(reservation_id="DRY-RUN", resy_token=None, raw={})
        if det.payment_method_id is None:
            raise BookingRefused("No payment method on your Resy account; add a card in the Resy app first.")

        body = self._request(                                               # VERIFY
            "POST",
            "/3/book",
            data={
                "book_token": det.book_token,
                "struct_payment_method": json.dumps({"id": det.payment_method_id}),
                "source_id": "resy.com-venue-details",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            retries=2,
        )
        return Booking(
            reservation_id=str(body.get("reservation_id") or ""),           # VERIFY
            resy_token=body.get("resy_token"),                              # VERIFY
            raw=body,
        )

    def close(self):
        self._http.close()
