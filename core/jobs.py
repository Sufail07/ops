"""Procrastinate jobs. Thin wrappers: real logic lives in core/services.py. Every job is idempotent."""
from __future__ import annotations

from datetime import date

from procrastinate import RetryStrategy

from core.models import Goal, Status, Task
from core.queue import app
from core.runtime import get_runtime
from core import services

RETRY = RetryStrategy(max_attempts=5, exponential_wait=2)


def _task(d: dict) -> Task:
    return Task(d["id"], d["title"], d.get("owner"), date.fromisoformat(d["due"]) if d.get("due") else None,
                d.get("estimate_days", 1), Status(d.get("status", "todo")), d.get("goal_id"),
                tuple(d.get("depends_on", ())),
                date.fromisoformat(d["last_update"]) if d.get("last_update") else None)


def _goal(d: dict) -> Goal:
    return Goal(d["id"], d["name"], d.get("weight", 3),
                date.fromisoformat(d["target_date"]) if d.get("target_date") else None)


@app.task(name="apply_snapshot", retry=RETRY, queueing_lock=None)
def apply_snapshot(tasks: list[dict], goals: list[dict], today: str) -> int:
    rt = get_runtime()
    alerts = services.apply_snapshot(rt.db, rt.notifier, [_task(t) for t in tasks], [_goal(g) for g in goals],
                                     date.fromisoformat(today))
    return len(alerts)


@app.task(name="daily_plan", retry=RETRY)
def daily_plan(today: str) -> int:
    return len(services.run_daily_plan(get_runtime().db, date.fromisoformat(today)))


# ----- scheduled and event jobs ------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

from core.followups.hours import WorkingHours  # noqa: E402
from core.settings import get_settings  # noqa: E402


@app.periodic(cron="*/15 * * * *")
@app.task(name="followup_tick", queueing_lock="followup_tick", retry=RETRY)
def followup_tick(timestamp: int) -> dict:
    rt = get_runtime()
    return services.followup_tick(rt.db, rt.notifier, datetime.now(timezone.utc),
                                  WorkingHours.from_settings(get_settings()))


@app.periodic(cron="30 3 * * *")  # 09:00 IST
@app.task(name="daily_plan_cron", queueing_lock="daily_plan_cron", retry=RETRY)
def daily_plan_cron(timestamp: int) -> int:
    today = WorkingHours.from_settings(get_settings()).local_today(datetime.now(timezone.utc))
    return len(services.run_daily_plan(get_runtime().db, today))


@app.task(name="process_inbox_event", retry=RETRY)
def process_inbox_event(event_id: str, source: str, payload: dict) -> None:
    """Webhook events land here. The Notion page sync is the next build step; for now the event is
    recorded in the append-only log so nothing is lost, and the inbox row is marked processed."""
    db = get_runtime().db
    db.append_event(f"{source}.event_received", payload.get("entity", {}).get("id"), {"type": payload.get("type")},
                    "webhook")
    db.mark_inbox_processed(event_id)


@app.periodic(cron="*/5 * * * *")
@app.task(name="reconcile_poll", queueing_lock="reconcile_poll", retry=RETRY)
def reconcile_poll(timestamp: int) -> dict:
    """Every 5 minutes: sync Notion, alert on slips, extract meeting notes, send due follow-ups."""
    from core.poll import run_poll
    return run_poll(get_runtime(), get_settings())


@app.periodic(cron="* * * * *")
@app.task(name="sweep_outbox", queueing_lock="sweep_outbox", retry=RETRY)
def sweep_outbox(timestamp: int) -> dict:
    """Every minute: resend any alert or approval card that was saved but never delivered."""
    rt = get_runtime()
    return services.sweep(rt.db, rt.notifier)
