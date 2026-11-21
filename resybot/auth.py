"""
Email + password login, so you never copy tokens out of DevTools again.

  * Your Resy email goes in .env (RESY_EMAIL). Your password goes in the
    macOS Keychain (service "resybot"), never in a file or a log.
  * When Resy says the token expired (401/419), ResyClient calls
    refresh_token(), which logs in again, saves the new token to .env and
    retries the request. Bookings don't fail because a token went stale.

The login request deliberately bypasses ResyClient._request so the
password can never end up in logs/resy.jsonl.
"""
from __future__ import annotations

import logging
import os
import threading

import httpx

from .resy import ResyError

log = logging.getLogger("resybot.auth")

KEYCHAIN_SERVICE = "resybot"
LOGIN_URL = "https://api.resy.com/3/auth/password"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
_lock = threading.Lock()


class LoginError(ResyError):
    pass


# ---- keychain ------------------------------------------------------------------

def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def save_password(email: str, password: str) -> None:
    kr = _keyring()
    if kr is None:
        raise LoginError("The 'keyring' package isn't installed (pip install -r requirements.txt).")
    kr.set_password(KEYCHAIN_SERVICE, email, password)


def get_password(email: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYCHAIN_SERVICE, email)
    except Exception:          # no keychain backend (e.g. a headless server)
        return None


def forget(email: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYCHAIN_SERVICE, email)
        except Exception:
            pass


def can_auto_login() -> bool:
    email = os.environ.get("RESY_EMAIL")
    return bool(email and get_password(email))


# ---- login -----------------------------------------------------------------------

def login(email: str, password: str, api_key: str) -> dict:
    """
    POST /3/auth/password. Returns Resy's user payload (with `token`).
    Raises LoginError with a human message on failure.
    """
    try:
        r = httpx.post(
            LOGIN_URL,
            data={"email": email, "password": password},
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "X-Origin": "https://resy.com",
                "User-Agent": _BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=10,
        )
    except httpx.HTTPError as e:
        raise LoginError(f"Couldn't reach Resy: {e}") from e
    if r.status_code in (401, 419):
        raise LoginError("Resy said the email or password is wrong.")
    if r.status_code == 429:
        raise LoginError("Resy is rate-limiting logins. Wait a few minutes and try again.")
    if r.status_code >= 400:
        raise LoginError(f"Resy login failed ({r.status_code}).")
    try:
        body = r.json()
    except ValueError as e:
        raise LoginError("Resy sent back something unexpected.") from e
    token = body.get("token") or (body.get("user") or {}).get("token")
    if not token:
        raise LoginError("Resy accepted the login but didn't return a token.")
    body["token"] = token
    return body


def refresh_token() -> str:
    """
    Log in again with the saved email + Keychain password, save the new
    token to .env, and return it. Used automatically on 401/419.
    """
    from . import store   # avoid an import cycle

    with _lock:
        email = os.environ.get("RESY_EMAIL")
        from .config import PUBLIC_API_KEY
        api_key = os.environ.get("RESY_API_KEY") or PUBLIC_API_KEY
        password = get_password(email) if email else None
        if not (email and password and api_key):
            raise LoginError("No saved email/password. Use “Log in with email & password” in the menu.")
        body = login(email, password, api_key)
        store.set_secret("RESY_AUTH_TOKEN", body["token"])
        log.info("Resy token refreshed automatically")
        return body["token"]
