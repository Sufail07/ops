"""One poll cycle: pull Notion, alert on slips, write outputs back, extract new meeting notes, then send any
follow-ups that are due. Used by the 5-minute scheduled job and the admin 'poll now' endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from core import services
from core.followups.hours import WorkingHours
from core.notion import sync
from core.runtime import Runtime
from core.settings import Settings


def run_poll(rt: Runtime, cfg: Settings, now: datetime | None = None) -> dict:
    if rt.notion is None:
        return {"skipped": "Notion not configured"}
    now = now or datetime.now(timezone.utc)
    hours = WorkingHours.from_settings(cfg)
    today = hours.local_today(now)
    with rt.db.try_lock("notion_sync") as got:   # the poll, a Slack click and the admin endpoint never overlap
        if not got:
            return {"skipped": "another sync is running"}
        res = sync.sync_notion(rt.db, rt.notifier, rt.notion, cfg, today)
        extracted = (sync.extract_new_meetings(rt.db, rt.notion, rt.gateway, cfg, today, notifier=rt.notifier)
                     if rt.gateway else 0)
    # Follow-ups right after the sync (not on a separate 15-minute clock): the unique dedupe key makes this safe.
    tick = services.followup_tick(rt.db, rt.notifier, now, hours)
    swept = services.sweep(rt.db, rt.notifier)
    return {"resent": swept["sent"] + swept["cards"], "tasks": res.tasks_seen, "changed": res.tasks_changed, "alerts": res.alerts, "writebacks": res.writebacks,
            "warnings": len(res.warnings), "proposals": extracted, "followups_sent": tick["sent"]}
