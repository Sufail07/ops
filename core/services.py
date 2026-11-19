"""Orchestration: DB + graph engine + alerts. Plain functions so they are testable without the queue."""
from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, timezone
from typing import Iterable

from core.alerts.format import Alert, AlertItem, alert_from_row, build_slip_alerts
from core.db import Database
from core.followups.hours import WorkingHours
from core.followups.rules import FollowUpConfig, actions_for, due_followups, proposal_followups, resolve_recipient
from core.graph.engine import CycleError, Impact, Risk, RiskParams, TaskGraph, slip_impacts
from core.models import Goal, Task
from core.notifier import Notifier
from core.priority.engine import PriorityParams, prioritise

log = logging.getLogger("ops.services")


def apply_snapshot(db: Database, notifier: Notifier, tasks: Iterable[Task], goals: Iterable[Goal],
                   today: date, params: RiskParams = RiskParams(), *, quiet: bool = False,
                   state: tuple[list[Task], list[Goal], set] | None = None) -> list[Alert]:
    """Merge changed tasks into the cache, compare risk before vs after, alert everyone newly affected.

    Nothing is auto-changed: this only reads dates people set and tells people what they put at risk.
    `state` = (cached tasks, cached goals, recently-notified keys) if the caller already loaded them.
    """
    tasks, goals = list(tasks), list(goals)
    old_tasks, old_goals, recent = state if state is not None else db.load_snapshot_state()
    merged_tasks = {t.id: t for t in old_tasks} | {t.id: t for t in tasks}
    merged_goals = {g.id: g for g in old_goals} | {g.id: g for g in goals}

    try:
        after_graph = TaskGraph(merged_tasks.values(), merged_goals.values())
    except CycleError as e:
        # Refuse the write; tell the lead in plain language.
        db.append_event("graph.cycle_rejected", None, {"members": e.members}, "system")
        alert = Alert(None, "off_track",
                      "These tasks depend on each other in a circle, so the change was not saved: "
                      + ", ".join(merged_tasks[m].title if m in merged_tasks else m for m in e.members)
                      + ". Remove one dependency in Notion.", tuple(e.members))
        ids = db.enqueue_alerts([alert])
        deliver_outbox(db, notifier, ids)
        return [alert]

    before = TaskGraph(old_tasks, old_goals).analyze(today, params)
    if quiet:  # first load of existing data: record it, don't alert about history
        db.commit_snapshot(goals, tasks, [], [])
        return []
    after = after_graph.analyze(today, params)

    impacts = slip_impacts(before, after, after_graph)
    fresh = [i for i in impacts if (i.task_id, i.new_risk.value, i.new_delay) not in recent]  # 1 alert/cause/hour
    alerts = build_slip_alerts(fresh, after_graph, after) if fresh else []
    # The change AND the alerts about it are saved in one atomic write (transactional outbox). From here a crash
    # cannot lose an alert: anything not delivered below is still in alert_outbox and the sweeper will send it.
    ids = db.commit_snapshot(goals, tasks,
                             [(i.task_id, i.cause_chain[0], i.old_risk.value, i.new_risk.value, i.new_delay,
                               after[i.task_id].slack_days) for i in fresh],
                             [("risk.changed", i.task_id, {"new": i.new_risk.value, "cause": list(i.cause_chain)}, "system")
                              for i in fresh], alerts)
    if ids:
        deliver_outbox(db, notifier, ids)
    return alerts


def deliver_outbox(db: Database, notifier: Notifier, ids: list[int] | None = None, grace_seconds: float = 0) -> dict[str, int]:
    """Send alerts from the outbox. Safe to call from anywhere, any number of times:
      * each alert is claimed first, so two callers can never send the same one;
      * a failed send is recorded and retried by the sweeper with growing back-off (for up to 24 hours), never raised.
    ids=None means 'everything unsent' (the sweeper); grace_seconds skips alerts saved only moments ago."""
    rows = sorted(db.claim_outbox(ids, grace_seconds), key=lambda r: r["id"])  # RETURNING has no order guarantee
    sent, failed = [], []
    notified: list[str] = []
    for row in rows:
        try:
            notifier.send(_mark_delayed(alert_from_row(row), row["created_at"]))
        except Exception as e:  # noqa: BLE001  (Slack down, bad token, ...): keep it queued and try again later
            log.warning("alert %s not delivered (attempt %s): %s", row["id"], row["attempts"], e)
            failed.append((row["id"], f"{type(e).__name__}: {e}"))
            continue
        sent.append(row["id"])
        notified += list(row["task_ids"])
    db.finish_outbox(sent, failed, notified)
    return {"claimed": len(rows), "sent": len(sent), "failed": len(failed)}


def _mark_delayed(alert: Alert, saved_at: datetime, now: datetime | None = None) -> Alert:
    """An alert that had to wait (Slack was down) says so, so nobody mistakes an old slip for a new one."""
    age = ((now or datetime.now(timezone.utc)) - saved_at).total_seconds()
    if age < 600:
        return alert
    ago = f"{int(age // 3600)} h" if age >= 3600 else f"{int(age // 60)} min"
    head, sep, rest = alert.text.partition("\n")
    return dataclasses.replace(alert, text=f"{head} (delayed: saved {ago} ago){sep}{rest}")


def sweep(db: Database, notifier: Notifier, grace_seconds: float = 60) -> dict[str, int]:
    """Resend anything that was saved but never delivered: alerts (outbox) and meeting approval cards.
    The grace period keeps it away from work that is still being delivered live."""
    out = deliver_outbox(db, notifier, None, grace_seconds)
    cards = 0
    pending = db.unposted_cards(grace_seconds)
    if pending:
        goals = {g.id: g.name for g in db.load_graph_inputs()[1]}
        by_meeting: dict[str, list[dict]] = {}
        for r in pending:
            by_meeting.setdefault(r["meeting_id"], []).append(r)
        for mid, rows in by_meeting.items():
            try:
                notifier.send_proposals(mid, rows[0]["payload"].get("meeting_title") or "meeting", rows, goals)
            except Exception as e:  # noqa: BLE001
                log.warning("approval card for %s not posted: %s", mid, e)
                continue
            db.mark_card_sent(mid)
            cards += 1
    return {**out, "cards": cards}


def run_daily_plan(db: Database, today: date, params: PriorityParams = PriorityParams()):
    tasks, goals, previous = db.load_daily_state(today)
    g = TaskGraph(tasks, goals)
    items = prioritise(g, g.analyze(today), today, params, previous)
    db.save_priority_snapshot(today, items)
    return items


def followup_tick(db: Database, notifier: Notifier, now: datetime, hours: WorkingHours,
                  cfg: FollowUpConfig = FollowUpConfig(), params: RiskParams = RiskParams()) -> dict[str, int]:
    """Queue what is due, cancel what no longer applies, send what is queued and inside working hours.

    Idempotent: the unique dedupe_key means a repeated tick cannot double-send. One message per person
    per tick, so nobody is spammed. Outside working hours follow-ups wait for the next window.
    """
    today = hours.local_today(now)
    tasks, goals, people, unconfirmed, pending = db.load_followup_state()
    graph = TaskGraph(tasks, goals)
    candidates = due_followups(graph, graph.analyze(today, params), today, cfg)
    candidates += proposal_followups(unconfirmed, now, cfg)

    when = hours.next_open(now)
    new_rows = db.insert_followups([{"entity_id": f.entity_id, "kind": f.kind, "step": f.step,
                                     "dedupe_key": f.dedupe_key, "due_at": when, "recipient_role": f.recipient_role,
                                     "text": f.text, "severity": f.severity} for f in candidates])
    pending = pending + new_rows

    # Anything queued that the rules no longer produce (task done, date changed, owner updated) is cancelled.
    current = {f.dedupe_key for f in candidates}
    stale = [r["id"] for r in pending if r["dedupe_key"] not in current]
    pending = [r for r in pending if r["dedupe_key"] in current]

    sent = 0
    due_now: list[dict] = []
    if hours.is_open(now):
        owners = {t.id: t.owner for t in tasks}
        due_now = [r for r in pending if r["due_at"] <= now]
        by_person: dict[str | None, list[dict]] = {}
        for r in due_now:
            by_person.setdefault(resolve_recipient(r["recipient_role"], owners.get(r["task_id"]), people), []).append(r)
        titles = {t.id: t.title for t in tasks}
        for person, rows in by_person.items():
            header = f"Follow-ups for {person or 'the team'} ({len(rows)}):"
            sev = "off_track" if any(r["severity"] == "off_track" for r in rows) else \
                "at_risk" if any(r["severity"] == "at_risk" for r in rows) else "info"
            items = tuple(AlertItem(r["task_id"], r["text"], actions_for(r["kind"], r["recipient_role"]),
                                    titles.get(r["task_id"])) for r in rows)
            notifier.send(Alert(person, sev, "\n".join([header, *[r["text"] for r in rows]]),
                                tuple(r["task_id"] for r in rows), kind="followup", items=items))
            sent += len(rows)
    db.finish_followups(stale, [r["id"] for r in due_now])
    return {"candidates": len(candidates), "sent": sent, "waiting": len(pending) - sent}


def preview_date_change(tasks: list[Task], goals: list[Goal], task_id: str, new_due: date, today: date,
                        params: RiskParams = RiskParams()) -> list[str]:
    """What would moving this task's due date put at risk? Plain-language lines, nothing is changed."""
    from dataclasses import replace
    graph = TaskGraph(tasks, goals)
    if task_id not in graph.tasks:
        return []
    changed = [replace(t, due=new_due) if t.id == task_id else t for t in tasks]
    after_graph = TaskGraph(changed, goals)
    lines: list[str] = []
    for imp in slip_impacts(graph.analyze(today, params), after_graph.analyze(today, params), after_graph):
        if imp.task_id == task_id:
            continue
        t = after_graph.tasks[imp.task_id]
        st = after_graph.analyze(today, params)[imp.task_id]
        late = f", {imp.new_delay} day(s) late" if imp.new_delay else ""
        lines.append(f"{t.owner or 'no owner'}: “{t.title}” would be {imp.new_risk.value.replace('_', ' ')}{late}")
    t0 = after_graph.tasks[task_id]
    if t0.goal_id and t0.goal_id in after_graph.goals:
        before_h = graph.goal_health(t0.goal_id, graph.analyze(today, params))
        after_h = after_graph.goal_health(t0.goal_id, after_graph.analyze(today, params))
        if after_h.status != before_h.status:
            lines.append(f"Goal “{after_graph.goals[t0.goal_id].name}”: {before_h.status.replace('_', ' ')} → "
                         f"{after_h.status.replace('_', ' ')}")
    return lines
