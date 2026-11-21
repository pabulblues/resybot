"""
Gmail listener for Resy Notify emails.

Uses IMAP IDLE: Gmail pushes "new mail" to us the instant it arrives, so
nothing is checked on a timer and Resy is never contacted while waiting.
Only emails from resy.com are opened (read-only, never marked as read).

Login: a Gmail *app password* (Google Account → Security → 2-Step
Verification → App passwords), stored in the macOS Keychain.
"""
from __future__ import annotations

import email
import html
import imaplib
import logging
import re
import socket
import threading
from email.header import decode_header, make_header
from typing import Callable

import keyring

log = logging.getLogger("resybot.mailwatch")

KEYCHAIN = "resybot-gmail"
HOST = "imap.gmail.com"
IDLE_S = 9 * 60          # Gmail drops idle connections after ~10 min: renew before that


# ---- credentials ----------------------------------------------------------------

def save(address: str, app_password: str) -> None:
    keyring.set_password(KEYCHAIN, "address", address)
    keyring.set_password(KEYCHAIN, address, app_password.replace(" ", ""))


def creds() -> tuple[str, str] | None:
    try:
        addr = keyring.get_password(KEYCHAIN, "address")
        pw = keyring.get_password(KEYCHAIN, addr) if addr else None
    except Exception:
        return None
    return (addr, pw) if addr and pw else None


def forget() -> None:
    c = creds()
    for k in ("address", c[0] if c else None):
        if k:
            try:
                keyring.delete_password(KEYCHAIN, k)
            except Exception:
                pass


def test_login(address: str, app_password: str) -> str | None:
    """None if it works, else a readable error."""
    try:
        m = imaplib.IMAP4_SSL(HOST, timeout=20)
        m.login(address, app_password.replace(" ", ""))
        m.select("INBOX", readonly=True)
        m.logout()
        return None
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
            return "Gmail said the email or app password is wrong (use an app password, not your normal one)."
        return f"Gmail refused: {msg}"
    except OSError as e:
        return f"Couldn't reach Gmail: {e}"


# ---- reading a message -------------------------------------------------------------

def _text(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            s = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html":
            links = " ".join(re.findall(r'href="([^"]+)"', s))
            s = re.sub(r"<(script|style).*?</\1>", " ", s, flags=re.S | re.I)
            s = html.unescape(re.sub(r"<[^>]+>", " ", s)) + " " + html.unescape(links)
        parts.append(s)
    return re.sub(r"\s+", " ", " ".join(parts))


class MailWatcher:
    """Background thread: calls on_mail(subject, text) for each new email from Resy."""

    def __init__(self, on_mail: Callable[[str, str], None]):
        self.on_mail = on_mail
        self.connected = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._last_uid: int | None = None

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self._run, name="mailwatch", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            c = creds()
            if not c:
                self.connected, self.error = False, "Gmail not connected"
                self._stop.wait(60)
                continue
            try:
                self._session(*c)
                backoff = 5
            except Exception as e:           # network drop, Gmail restart, … → reconnect
                self.connected, self.error = False, str(e)
                log.warning("Gmail connection lost (%s); reconnecting in %ds", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)

    def _session(self, address: str, pw: str) -> None:
        m = imaplib.IMAP4_SSL(HOST, timeout=30)
        try:
            m.login(address, pw)
            m.select("INBOX", readonly=True)
            if self._last_uid is None:
                self._last_uid = self._uidnext(m) - 1      # only mail from now on
            self.connected, self.error = True, None
            log.info("Gmail connected: listening for Resy Notify emails")
            while not self._stop.is_set():
                self._check(m)
                self._idle(m)
        finally:
            try:
                m.logout()
            except Exception:
                pass

    @staticmethod
    def _uidnext(m) -> int:
        typ, data = m.status("INBOX", "(UIDNEXT)")
        mm = re.search(rb"UIDNEXT (\d+)", data[0] or b"")
        return int(mm.group(1)) if mm else 1

    def _idle(self, m) -> None:
        """Block until Gmail says something changed (or IDLE_S passes)."""
        tag = m._new_tag()
        m.send(tag + b" IDLE\r\n")
        first = m.readline()
        if not first.startswith(b"+"):
            raise RuntimeError(f"IDLE refused: {first!r}")
        m.sock.settimeout(IDLE_S)
        try:
            while not self._stop.is_set():
                line = m.readline()
                if not line:
                    raise ConnectionError("Gmail closed the connection")
                if b"EXISTS" in line:
                    break
        except socket.timeout:
            pass
        finally:
            m.sock.settimeout(30)
        m.send(b"DONE\r\n")
        while True:                       # read up to the tagged OK
            line = m.readline()
            if not line:
                raise ConnectionError("Gmail closed the connection")
            if line.startswith(tag):
                break

    def _check(self, m) -> None:
        typ, data = m.uid("search", None, f"UID {self._last_uid + 1}:*", "FROM", "resy.com")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > self._last_uid]
        newest = self._uidnext(m) - 1
        for uid in uids:
            typ, msg_data = m.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            log.info("Resy email: %s", subject)
            try:
                self.on_mail(subject, _text(msg))
            except Exception as e:
                log.exception("handling Resy email failed: %s", e)
        self._last_uid = max([self._last_uid, newest, *uids])
