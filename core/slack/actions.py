"""What the Slack buttons do. Plain functions (no Slack objects) so each rule is unit-testable.

Rules (the "AI proposes, a person confirms" principle, enforced in code):
  * A person must be linked to a Slack account to act at all.
  * Done / On track / Slipping: only the task's owner, a lead, or anyone if the task has no owner.
  * A date change is only a REQUEST until a lead approves it; only then is Notion written.
  * Meeting proposals create nothing until someone approves; approval is atomic (claimed exactly once).
  * Bootstrap: if no lead is configured yet, any linked person counts as an approver.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core import services
from core.models import Person, Task
from core.notion import mapping as m
from core.runtime import Runtime
from core.settings import Settings

UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)


@dataclass
class ActionResult:
    ok: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)


def _who(rt: Runtime, slack_uid: str) -> tuple[Person | None, list[Person]]:
    people = rt.db.load_people()
    return next((p for p in people if p.slack_user_id == slack_uid), None), people


def is_approver(person: Person | None, people: list[Person]) -> bool:
    return bool(person) and (person.is_lead or not any(p.is_lead for p in people))


def can_act_on(person: Person | None, task: Task) -> bool:
    return bool(person) and (person.is_lead or task.owner is None or task.owner == person.name)


UNLINKED = ActionResult(False, "I don't know which team member you are yet, so I can't change anything. "
                               "Ask an admin to link your Slack account.")


def _task(rt: Runtime, task_id: str) -> Task | None:
    tasks, _ = rt.db.load_graph_inputs()
    return next((t for t in tasks if t.id == task_id), None)


def mark_done(rt: Runtime, slack_uid: str, task_id: str) -> ActionResult:
    person, _ = _who(rt, slack_uid)
    task = _task(rt, task_id)
    if not person:
        return UNLINKED
    if task is None:
        return ActionResult(False, "That task no longer exists in Notion.")
    if not can_act_on(person, task):
        return ActionResult(False, f"“{task.title}” belongs to {task.owner}. Only they or a lead can mark it done.")
    rt.notion.update_page(task_id, {m.T_STATUS: {"status": {"name": "Done"}}})
    rt.db.append_event("task.marked_done", task_id, {"via": "slack"}, person.name)
    rt.db.set_followup_outcome(task_id, "done")
    return ActionResult(True, f"✅ “{task.title}” marked done by {person.name}.", {"title": task.title})


def confirm_on_track(rt: Runtime, slack_uid: str, task_id: str) -> ActionResult:
    person, _ = _who(rt, slack_uid)
    task = _task(rt, task_id)
    if not person:
        return UNLINKED
    if task is None:
        return ActionResult(False, "That task no longer exists in Notion.")
    if not can_act_on(person, task):
        return ActionResult(False, f"“{task.title}” belongs to {task.owner}.")
    rt.db.append_event("task.confirmed_on_track", task_id, {"via": "slack"}, person.name)
    rt.db.set_followup_outcome(task_id, "on_track")
    return ActionResult(True, f"👍 {person.name} confirmed “{task.title}” is on track.")


def slip_precheck(rt: Runtime, slack_uid: str, task_id: str) -> ActionResult:
    """Can this person propose a new date for this task? (checked before the modal opens)"""
    person, _ = _who(rt, slack_uid)
    task = _task(rt, task_id)
    if not person:
        return UNLINKED
    if task is None:
        return ActionResult(False, "That task no longer exists in Notion.")
    if not can_act_on(person, task):
        return ActionResult(False, f"“{task.title}” belongs to {task.owner}.")
    return ActionResult(True, "", {"title": task.title})


def request_date_change(rt: Runtime, slack_uid: str, task_id: str, new_due: date, reason: str | None,
                        today: date) -> ActionResult:
    """Creates a REQUEST only. Notion is untouched until a lead approves."""
    person, people = _who(rt, slack_uid)
    tasks, goals = rt.db.load_graph_inputs()
    task = next((t for t in tasks if t.id == task_id), None)
    if not person:
        return UNLINKED
    if task is None:
        return ActionResult(False, "That task no longer exists in Notion.")
    if not can_act_on(person, task):
        return ActionResult(False, f"“{task.title}” belongs to {task.owner}.")
    if new_due == task.due:
        return ActionResult(False, "That is already the due date.")
    if new_due < today:
        return ActionResult(False, "The new date is in the past.")
    local_id = str(int(time.time() * 1000))
    meeting_id = f"slip:{task_id}"
    payload = {"kind": "date_change", "local_id": local_id, "task_id": task_id, "title": task.title,
               "old_due": str(task.due) if task.due else None, "new_due": str(new_due), "reason": reason,
               "requested_by": person.name}
    rt.db.save_proposals(meeting_id, [payload])
    rt.db.append_event("date_change.requested", task_id, {"new_due": str(new_due)}, person.name)
    preview = services.preview_date_change(tasks, goals, task_id, new_due, today)
    return ActionResult(True, f"Request sent for approval: “{task.title}” → {new_due}.",
                        {"meeting_id": meeting_id, "local_id": local_id, "payload": payload, "preview": preview})


def decide_date_change(rt: Runtime, slack_uid: str, meeting_id: str, local_id: str, approve: bool) -> ActionResult:
    person, people = _who(rt, slack_uid)
    if not person:
        return UNLINKED
    if not is_approver(person, people):
        return ActionResult(False, "Only a lead can approve or reject date changes.")
    row = rt.db.get_proposal(meeting_id, local_id)
    if row is None or row["payload"].get("kind") != "date_change":
        return ActionResult(False, "I can't find that request.")
    payload = row["payload"]
    if not rt.db.decide_proposal(meeting_id, local_id, "Approved" if approve else "Rejected", person.name):
        return ActionResult(False, f"Already {row['status'].lower()}.", {"status": row["status"]})
    if approve:
        try:
            rt.notion.update_page(payload["task_id"], {m.T_DUE: {"date": {"start": payload["new_due"]}}})
        except Exception:
            rt.db._exec("UPDATE proposals SET status='Proposed', decided_at=NULL, decided_by=NULL "
                        "WHERE meeting_id=%s AND local_id=%s", (meeting_id, local_id))  # let them retry
            raise
        rt.db.append_event("task.due_changed", payload["task_id"],
                           {"old": payload.get("old_due"), "new": payload["new_due"], "requested_by": payload["requested_by"]},
                           person.name)
    return ActionResult(True, ("✅ Approved" if approve else "✖ Rejected") + f" by {person.name}.",
                        {"status": "Approved" if approve else "Rejected", "decided_by": person.name,
                         "task_id": payload["task_id"]})


def decide_meeting_proposals(rt: Runtime, cfg: Settings, slack_uid: str, meeting_id: str,
                             local_ids: list[str] | None, approve: bool) -> ActionResult:
    """Approve (create Notion tasks) or reject proposals from a meeting. local_ids=None means all still pending."""
    person, people = _who(rt, slack_uid)
    if not person:
        return UNLINKED
    rows = [r for r in rt.db.get_proposals(meeting_id) if r["status"] == "Proposed"
            and (local_ids is None or r["local_id"] in local_ids)]
    if not rows:
        return ActionResult(False, "Nothing left to decide there.")
    if not approve:
        n = sum(rt.db.decide_proposal(meeting_id, r["local_id"], "Rejected", person.name) for r in rows)
        return ActionResult(True, f"✖ Rejected {n} proposal(s).", {"decided": n})

    claimed = [r for r in rows if rt.db.decide_proposal(meeting_id, r["local_id"], "Approved", person.name)]
    created: dict[str, str] = {}
    try:
        for r in claimed:
            p = r["payload"]
            props: dict[str, Any] = {
                m.T_TITLE: {"title": [{"text": {"content": p["title"][:200]}}]},
                m.T_STATUS: {"status": {"name": "Not started"}},
                "Description": {"rich_text": [{"text": {"content": (
                    f"Proposed by Ops Brain from a meeting, approved by {person.name}.\n"
                    f"Owner discussed: {p.get('owner') or p.get('owner_spoken') or 'unassigned'}\n"
                    f"Evidence: “{p.get('evidence_quote', '')}”")[:1900]}}]},
            }
            if p.get("due"):
                props[m.T_DUE] = {"date": {"start": p["due"]}}
            if p.get("goal_id"):
                props[m.T_GOAL] = {"relation": [{"id": p["goal_id"]}]}
            owner_person = next((x for x in people if x.name == p.get("owner")), None)
            if owner_person and UUID.match(owner_person.id):  # only real Notion user ids can be assigned
                props[m.T_ASSIGNEE] = {"people": [{"id": owner_person.id}]}
            elif p.get("owner") or p.get("owner_spoken"):      # otherwise keep the name, so the task still has an owner
                props[m.T_OWNER_TEXT] = {"rich_text": [{"text": {"content": (p.get("owner") or p["owner_spoken"])[:200]}}]}
            created[p["local_id"]] = cfg_create(rt, cfg, props)["id"]
        for r in claimed:  # dependencies need the new page ids, so they are linked in a second pass
            deps = [created[d] for d in r["payload"].get("depends_on", []) if d in created]
            if deps:
                rt.notion.update_page(created[r["local_id"]], {m.T_DEPENDS: {"relation": [{"id": d} for d in deps]}})
    except Exception:
        for r in claimed:  # nothing is half-approved: reopen the ones we did not manage to create
            if r["local_id"] not in created:
                rt.db._exec("UPDATE proposals SET status='Proposed', decided_at=NULL, decided_by=NULL "
                            "WHERE meeting_id=%s AND local_id=%s", (meeting_id, r["local_id"]))
        raise
    rt.db.append_events([("task.created_from_proposal", pid, {"meeting": meeting_id}, person.name)
                         for pid in created.values()])
    return ActionResult(True, f"✅ Created {len(created)} task(s) in Notion.", {"created": created})


def cfg_create(rt: Runtime, cfg: Settings, props: dict) -> dict:
    return rt.notion.create_page(cfg.notion_tasks_ds, props)


# ----- read-only commands -------------------------------------------------------------------
def my_plan(rt: Runtime, slack_uid: str, today: date, limit: int = 7) -> str:
    from core.graph.engine import TaskGraph
    from core.priority.engine import prioritise
    person, _ = _who(rt, slack_uid)
    tasks, goals, previous = rt.db.load_daily_state(today)
    g = TaskGraph(tasks, goals)
    items = prioritise(g, g.analyze(today), today, previous=previous)
    mine = [i for i in items if person and i.owner == person.name] or items
    scope = f"for {person.name}" if person and any(i.owner == person.name for i in items) else "for the team (no tasks assigned to you)"
    if not mine:
        return "No open tasks. 🎉"
    lines = [f"*Top priorities {scope}*"]
    for i in mine[:limit]:
        t = g.tasks[i.task_id]
        lines.append(f"{i.rank}. *{i.band}* {t.title} — {i.why.split(': ', 1)[1]}")
    return "\n".join(lines)


def why_task(rt: Runtime, query: str, today: date) -> str:
    from core.graph.engine import TaskGraph
    from core.priority.engine import prioritise
    tasks, goals, previous = rt.db.load_daily_state(today)
    g = TaskGraph(tasks, goals)
    hits = [t for t in tasks if query.casefold() in t.title.casefold()]
    if not hits:
        return f"No task matches “{query}”."
    if len(hits) > 1:
        return "Several tasks match, be more specific:\n" + "\n".join(f"• {t.title}" for t in hits[:8])
    t, a = hits[0], g.analyze(today)[hits[0].id]
    item = next((i for i in prioritise(g, g.analyze(today), today, previous=previous) if i.task_id == t.id), None)
    out = [f"*{t.title}*", item.why if item else "Done."]
    out.append(f"Risk: {a.risk.value.replace('_', ' ')}" + (f" · slack {a.slack_days} day(s)" if a.slack_days is not None else "")
               + (f" · projected finish {a.projected_finish}" if a.projected_finish else ""))
    if a.reasons:
        out.append("Because: " + "; ".join(a.reasons))
    return "\n".join(out)
