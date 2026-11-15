"""
Push notifications via ntfy.sh — no account, no API key.

Install the ntfy app on your phone, subscribe to the topic you put in
.env as NTFY_TOPIC, and every call here lands on your phone.
Pick an obscure topic name: anyone who knows it can read it.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("resybot.notify")


class Notifier:
    def __init__(self, topic: str | None):
        self.topic = topic

    def send(self, message: str, *, title: str = "resybot", priority: str = "default") -> None:
        log.info("NOTIFY: %s — %s", title, message)
        if not self.topic:
            return  # notifications are optional; never block the bot on them
        try:
            httpx.post(
                f"https://ntfy.sh/{self.topic}",
                content=message.encode(),
                headers={"Title": title, "Priority": priority},
                timeout=5,
            )
        except httpx.HTTPError as e:
            log.warning("ntfy failed (ignored): %s", e)

    def booked(self, target_name: str, slot_label: str, reservation_id: str | None):
        self.send(f"Booked {target_name}: {slot_label} (#{reservation_id})",
                  title="🎉 Reservation booked", priority="high")

    def found(self, target_name: str, slot_label: str, url: str):
        """Manual mode: a slot is open; the person taps the link and books."""
        log.info("NOTIFY: found %s %s → %s", target_name, slot_label, url)
        if not self.topic:
            return
        try:
            httpx.post(
                f"https://ntfy.sh/{self.topic}",
                content=f"{target_name}: {slot_label} is open. Tap to book.".encode(),
                headers={"Title": "⚡ Slot open — book it now", "Priority": "urgent",
                         "Click": url, "Tags": "rotating_light"},
                timeout=5,
            )
        except httpx.HTTPError as e:
            log.warning("ntfy failed (ignored): %s", e)

    def failed(self, target_name: str, reason: str):
        self.send(f"{target_name}: {reason}", title="❌ No reservation", priority="high")
