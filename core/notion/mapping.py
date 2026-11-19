"""Notion page JSON <-> domain objects. Pure functions; every oddity becomes a warning, never a crash."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.graph.engine import GoalHealth, Risk, TaskState
from core.models import Goal, Status, Task
from core.priority.engine import PriorityItem

# Property names in the user's Notion databases
T_TITLE, T_DUE, T_STATUS, T_ASSIGNEE = "Task name", "Due date", "Status", "Assignee"
T_GOAL, T_DEPENDS, T_ESTIMATE = "Goal", "Depends on", "Estimate (days)"
T_OWNER_TEXT = "Owner (name)"  # fallback for owners who are not Notion users (or whose Notion user we cannot assign)
T_OUT_PRIORITY, T_OUT_REASON, T_OUT_RISK, T_OUT_FINISH = "Ops priority", "Rank reason", "Risk", "Projected finish"
P_TITLE, P_WEIGHT, P_TARGET, P_OUT_HEALTH, P_OUT_REASON = "Name", "Weight", "Target date", "Health", "Health reason"

STATUS_MAP = {"not started": Status.TODO, "in progress": Status.DOING, "blocked": Status.BLOCKED, "done": Status.DONE}
RISK_LABEL = {Risk.OK: "On track", Risk.AT_RISK: "At risk", Risk.OFF_TRACK: "Off track", Risk.UNKNOWN: "Unknown"}
HEALTH_LABEL = {"on_track": "On track", "at_risk": "At risk", "off_track": "Off track"}
# Notion rounds last_edited_time to the minute, so allow a margin when recognising our own write-backs.
OWN_WRITE_MARGIN = timedelta(seconds=90)


def _plain(rich: list[dict] | None) -> str:
    return "".join(x.get("plain_text", "") for x in rich or [])


def _prop(page: dict, name: str) -> dict:
    return (page.get("properties") or {}).get(name) or {}


def _date(page: dict, name: str) -> date | None:
    d = _prop(page, name).get("date")
    return date.fromisoformat(d["start"][:10]) if d and d.get("start") else None


def _relation_ids(page: dict, name: str) -> list[str]:
    return [r["id"] for r in _prop(page, name).get("relation", [])]


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def map_goal(page: dict, warnings: list[str]) -> Goal | None:
    if page.get("in_trash") or page.get("archived"):
        return None
    name = _plain(_prop(page, P_TITLE).get("title")) or "(untitled project)"
    w = _prop(page, P_WEIGHT).get("number")
    if w is None:
        weight = 3
    elif float(w).is_integer() and 1 <= w <= 5:
        weight = int(w)
    else:
        warnings.append(f"Project '{name}': Weight must be a whole number 1 to 5 (got {w}); using 3")
        weight = 3
    return Goal(page["id"], name, weight, _date(page, P_TARGET))


def map_task(page: dict, goal_ids: set[str], task_ids: set[str], warnings: list[str], *,
             prev_last_update: date | None = None, our_write_at: datetime | None = None) -> Task | None:
    if page.get("in_trash") or page.get("archived"):
        return None
    title = _plain(_prop(page, T_TITLE).get("title")) or "(untitled task)"
    status_name = ((_prop(page, T_STATUS).get("status") or {}).get("name") or "").strip()
    status = STATUS_MAP.get(status_name.lower())
    if status is None:
        warnings.append(f"Task '{title}': unknown status '{status_name}'; treated as Not started")
        status = Status.TODO
    people = _prop(page, T_ASSIGNEE).get("people", [])
    if len(people) > 1:
        warnings.append(f"Task '{title}': {len(people)} assignees; using the first")
    if people:
        owner = people[0].get("name") or people[0]["id"]
    else:
        owner = _plain(_prop(page, T_OWNER_TEXT).get("rich_text")).strip() or None

    est = _prop(page, T_ESTIMATE).get("number")
    if est is None:
        estimate = 1
    elif est < 0:
        warnings.append(f"Task '{title}': negative estimate ignored")
        estimate = 1
    else:
        estimate = math.ceil(est)

    goals = _relation_ids(page, T_GOAL)
    if len(goals) > 1:
        warnings.append(f"Task '{title}': linked to {len(goals)} goals; using the first")
    goal_id = goals[0] if goals and goals[0] in goal_ids else None
    if goals and goal_id is None:
        warnings.append(f"Task '{title}': its goal is not an active project; goal link ignored")
    deps = tuple(sorted(d for d in _relation_ids(page, T_DEPENDS) if d in task_ids and d != page["id"]))

    edited = parse_ts(page["last_edited_time"])
    if our_write_at is not None and edited <= our_write_at + OWN_WRITE_MARGIN and prev_last_update is not None:
        last_update = prev_last_update      # that edit was our own write-back, not a person's update
    else:
        last_update = edited.date()
    return Task(page["id"], title, owner, _date(page, T_DUE), estimate, status, goal_id, deps, last_update)


# ----- write-back payloads ------------------------------------------------------------------
def _rich(s: str) -> dict:
    return {"rich_text": [{"text": {"content": s[:1900]}}]} if s else {"rich_text": []}


def task_outputs(item: PriorityItem | None, state: TaskState) -> dict[str, Any]:
    if state.risk == Risk.DONE or item is None:
        return {T_OUT_PRIORITY: {"select": None}, T_OUT_REASON: _rich(""), T_OUT_RISK: {"select": None},
                T_OUT_FINISH: {"date": None}}
    reason = item.why + (f" | {'; '.join(state.reasons)}" if state.reasons else "")
    return {T_OUT_PRIORITY: {"select": {"name": item.band}}, T_OUT_REASON: _rich(reason),
            T_OUT_RISK: {"select": {"name": RISK_LABEL[state.risk]}},
            T_OUT_FINISH: {"date": {"start": state.projected_finish.isoformat()} if state.projected_finish else None}}


def goal_outputs(h: GoalHealth) -> dict[str, Any]:
    label = HEALTH_LABEL.get(h.status)  # no_tasks: leave Health empty rather than claim it is healthy
    return {P_OUT_HEALTH: {"select": {"name": label}} if label else {"select": None}, P_OUT_REASON: _rich(h.reason)}


def payload_hash(props: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(props, sort_keys=True, default=str).encode()).hexdigest()[:16]


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
