"""
Notifications.

  * On a Mac: a notification with sound for bookings / misses / login
    problems, and for captcha restaurants (manual mode) the Resy page opens
    straight in your browser at the right date, with an alert sound, so
    you can click the time and confirm.
  * On the server: the Telegram bot registers itself as a channel.

A channel is any callable (title, message, url, priority) -> None.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Callable, Optional

log = logging.getLogger("resybot.notify")

Channel = Callable[[str, str, Optional[str], str], None]


class Notifier:
    def __init__(self, *, quiet: bool = False):
        """quiet=True: log only (practice runs)."""
        self.channels: list[Channel] = []
        if not quiet and sys.platform == "darwin":
            self.channels.append(MacAlerts())

    def add_channel(self, ch: Channel) -> None:
        self.channels.append(ch)

    def send(self, message: str, *, title: str = "resybot", priority: str = "default",
             url: str | None = None) -> None:
        log.info("NOTIFY: %s: %s%s", title, message, f" → {url}" if url else "")
        for ch in self.channels:
            try:
                ch(title, message, url, priority)
            except Exception as e:  # a notification must never break a booking
                log.warning("notify channel failed (ignored): %s", e)

    def booked(self, target_name: str, slot_label: str, reservation_id: str | None):
        if reservation_id == "DRY-RUN":
            self.send(f"{target_name}: would have booked {slot_label}. Nothing was reserved.",
                      title="Practice worked")
            return
        self.send(f"Booked {target_name}: {slot_label} (#{reservation_id})",
                  title="🎉 Reservation booked", priority="high")

    def found(self, target_name: str, slot_label: str, url: str):
        """Captcha restaurant: a slot is open; open the page so you can confirm."""
        self.send(f"{target_name}: {slot_label} is open. Click the time and confirm.",
                  title="⚡ Table open: book it now", priority="urgent", url=url)

    def failed(self, target_name: str, reason: str):
        self.send(f"{target_name}: {reason}", title="❌ No reservation", priority="high")


class MacAlerts:
    """macOS notification + sound; urgent alerts with a link open it in the browser."""

    SOUND = "/System/Library/Sounds/Glass.aiff"
    URGENT_SOUND = "/System/Library/Sounds/Sosumi.aiff"

    def __init__(self):
        self._opened: dict[str, float] = {}

    def __call__(self, title: str, message: str, url: str | None, priority: str) -> None:
        # Message text goes in as osascript arguments, never spliced into the script.
        subprocess.Popen(
            ["osascript", "-e", "on run argv", "-e",
             "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
             title, message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if priority == "urgent":
            # Open the booking page once per page (the sniper re-alerts if the best slot changes).
            if url and time.time() - self._opened.get(url, 0) > 120:
                self._opened[url] = time.time()
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.Popen(["sh", "-c", f"for i in 1 2 3; do afplay {self.URGENT_SOUND}; done"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif priority == "high":
            subprocess.Popen(["afplay", self.SOUND], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
