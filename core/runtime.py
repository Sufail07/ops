"""Process-wide wiring (DB, notifier, Notion, model gateway). Tests replace it with set_runtime()."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from core.db import Database, PgRunLog
from core.notifier import ConsoleNotifier, Notifier
from core.settings import ROOT, get_settings


@dataclass
class Runtime:
    db: Database
    notifier: Notifier
    notion: Any | None = None       # NotionClient (None = Notion sync disabled)
    gateway: Any | None = None      # core.gateway.Gateway (None = meeting extraction disabled)
    slack: Any | None = None        # core.slack.notifier.SlackNotifier (None = Slack disabled)


_rt: Runtime | None = None


def get_runtime() -> Runtime:
    global _rt
    if _rt is None:
        s = get_settings()
        db = Database(s.database_url)
        notion = gateway = None
        if s.notion_pat:
            from core.notion.client import NotionClient
            notion = NotionClient(s.notion_pat)
        if s.groq_api_key:
            os.environ.setdefault("GROQ_API_KEY", s.groq_api_key)  # LiteLLM reads provider keys from the environment
            from core.gateway import Gateway, Routing
            gateway = Gateway(Routing.load(ROOT / "config" / "routing.yaml"), run_log=PgRunLog(db))
        notifier: Notifier = ConsoleNotifier()
        slack = None
        if s.slack_oauth_token:
            from core.slack.notifier import SlackNotifier
            slack = notifier = SlackNotifier(db, s.slack_oauth_token, s.slack_alerts_channel)
        _rt = Runtime(db, notifier, notion, gateway, slack)
    return _rt


def set_runtime(rt: Runtime | None) -> None:
    global _rt
    _rt = rt
