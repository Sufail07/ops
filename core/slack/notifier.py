"""Deliver alerts over Slack: DM the person, or fall back to the alerts channel."""
from __future__ import annotations

import logging
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from core.alerts.format import Alert
from core.db import Database
from core.slack import blocks as B

log = logging.getLogger("ops.slack")


class SlackNotifier:
    def __init__(self, db: Database, bot_token: str, alerts_channel: str | None, client: Any | None = None,
                 people_ttl: float = 60.0):
        self.db = db
        self.client = client or WebClient(token=bot_token)
        self.channel = alerts_channel
        self._ttl = people_ttl
        self._people: list = []
        self._people_at = 0.0

    def _people_list(self) -> list:
        if time.monotonic() - self._people_at > self._ttl:
            self._people, self._people_at = self.db.load_people(), time.monotonic()
        return self._people

    def _target(self, owner: str | None) -> str | None:
        """Slack user id (DM) for a named owner, else None (use the channel)."""
        if not owner:
            return None
        for p in self._people_list():
            if p.name == owner and p.slack_user_id:
                return p.slack_user_id
        return None

    def post(self, channel: str, text: str, blocks: list[dict]) -> dict:
        return self.client.chat_postMessage(channel=channel, text=text[:3000], blocks=blocks)

    def send(self, alert: Alert) -> None:
        dm = self._target(alert.owner)
        blocks = B.alert_blocks(alert)
        text = alert.text
        if dm:
            try:
                self.post(dm, text, blocks)
                return
            except SlackApiError as e:
                log.warning("DM to %s failed (%s); falling back to the alerts channel", alert.owner, e.response.get("error"))
        if not self.channel:
            raise RuntimeError("Slack alerts channel not configured (SLACK_ALERTS_CHANNEL) and no DM possible")
        prefix = f"*For {B.esc(alert.owner)}* (no Slack account linked)\n" if alert.owner else ""
        self.post(self.channel, text, ([B.section(prefix)] if prefix else []) + blocks)

    def send_proposals(self, meeting_id: str, title: str, rows: list[dict], goal_names: dict[str, str]) -> None:
        """Approval card for tasks extracted from a meeting. Goes to the alerts channel (the whole team was there)."""
        if not self.channel:
            raise RuntimeError("Slack alerts channel not configured (SLACK_ALERTS_CHANNEL)")
        self.post(self.channel, f"{len(rows)} task(s) proposed from “{title}”",
                  B.proposals_card(meeting_id, title, rows, goal_names))
