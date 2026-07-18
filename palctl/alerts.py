"""
Generic outbound webhook alerting.

The Discord bot and the local GUI/log are palctl's built-in notification paths —
but the channel most likely to be down when something breaks is the one you need
most. This adds a second, dependency-free channel: one HTTP POST to any URL you
configure. It's deliberately format-agnostic so the common receivers accept the
same payload unchanged:

    {"content": msg, "text": msg, "message": msg, "kind": kind, "data": {...}, "at": iso}

  * a Discord incoming webhook reads `content`
  * a Slack incoming webhook reads `text`
  * ntfy / a custom endpoint can read whichever field (or the whole JSON body)

Only operationally interesting events go out — outages, the watchdog, errors,
updates, restores, backups — never the join/leave/levelup chatter that would
turn an ops alert channel into a firehose.
"""

from __future__ import annotations

import logging

import httpx

from .config import Config
from .events import Event, EventBus

# The kinds worth waking someone for. Deliberately excludes join/leave/levelup.
DEFAULT_ALERT_KINDS = frozenset(
    {
        "server_up",
        "server_down",
        "watchdog",
        "error",
        "update_available",
        "restore",
        "backup",
    }
)


class WebhookAlerter:
    """Subscribes to the event bus once and POSTs the interesting events to the
    configured webhook. reconfigure() applies a config reload in place, so the
    subscription is never stacked or dropped."""

    def __init__(self, cfg: Config, bus: EventBus, timeout: float = 10.0) -> None:
        self._log = logging.getLogger("palctl.alerts")
        self._timeout = timeout
        self._enabled = False
        self._url = ""
        self._kinds = DEFAULT_ALERT_KINDS
        self.reconfigure(cfg)
        bus.on_any(self._on_event)

    def reconfigure(self, cfg: Config) -> None:
        # Fire only when switched on AND a target is set — the same on/off vs.
        # path split the backup mirror uses, so the URL survives a toggle.
        self._url = cfg.alert_webhook_url
        self._enabled = bool(cfg.alert_webhook_enabled and cfg.alert_webhook_url)

    async def _on_event(self, e: Event) -> None:
        if not self._enabled or e.kind not in self._kinds:
            return
        payload = {
            "content": e.message,  # Discord webhook
            "text": e.message,     # Slack webhook
            "message": e.message,
            "kind": e.kind,
            "data": e.data,
            "at": e.at.isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self._url, json=payload)
                if r.status_code >= 400:
                    self._log.warning(
                        "alert webhook returned HTTP %s: %s",
                        r.status_code,
                        r.text[:200],
                    )
        except Exception as ex:  # noqa: BLE001 — a bad webhook must never affect the daemon
            self._log.warning("alert webhook POST failed: %s", ex)
