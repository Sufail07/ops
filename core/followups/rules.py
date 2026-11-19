"""Pure follow-up rules: given the current graph state, which nudges are due? No I/O, no clock reads.

Follow-ups never change a date, owner or priority. They ask a person for a status. Re-running the
rules is safe: every follow-up has a dedupe_key, and the same key is never sent twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from core.graph.engine import Analysis, Risk, TaskGraph
from core.models import Person, Status


@dataclass(frozen=True)
class FollowUpConfig:
    due_soon_days: int = 2
    stale_days: int = 5
    # (days overdue at which this step starts, who gets it). Stops the moment the task is updated or done.
    ladder: tuple[tuple[int, str], ...] = ((1, "owner"), (2, "owner"), (3, "lead"), (5, "backup"))
    proposal_ladder_hours: tuple[tuple[int, str], ...] = ((24, "lead"), (48, "backup"))


@dataclass(frozen=True)
class FollowUp:
    entity_id: str            # task id, or "meeting:<id>" for unconfirmed proposals
    kind: str                 # due_soon | due_today | overdue | stale | proposals_unconfirmed
    step: int
    recipient_role: str       # owner | lead | backup
    dedupe_key: str
    text: str
    severity: str = "info"    # info | at_risk | off_track
    actions: tuple[str, ...] = ()


OWNER_ACTIONS = ("Done", "On track", "Slipping")


def actions_for(kind: str, recipient_role: str) -> tuple[str, ...]:
    """Buttons offered with a follow-up. Only actions that are implemented: Done, On track, Slipping."""
    if kind in ("due_soon", "due_today", "stale", "overdue") and recipient_role == "owner":
        return OWNER_ACTIONS
    return ()


def _d(x: date) -> str:
    return f"{x.day} {x.strftime('%b')}"


def due_followups(graph: TaskGraph, analysis: Analysis, today: date, cfg: FollowUpConfig = FollowUpConfig()) -> list[FollowUp]:
    out: list[FollowUp] = []
    for tid in graph.topo:
        t = graph.tasks[tid]
        if t.status == Status.DONE or t.due is None:
            continue
        left = (t.due - today).days
        who = t.owner or "someone"
        risky = analysis[tid].risk in (Risk.AT_RISK, Risk.OFF_TRACK)
        if left < 0:
            late = -left
            reached = [i for i, (days, _) in enumerate(cfg.ladder) if late >= days]
            if not reached:
                continue
            step = reached[-1]
            role = cfg.ladder[step][1]
            if role == "owner":
                text = (f"🔴 “{t.title}” was due {_d(t.due)} ({late} day(s) ago). What is the status? "
                        "If it will slip, send a new date and a person will confirm it.")
                actions = OWNER_ACTIONS
            else:
                text = (f"🔴 “{t.title}” ({who}) is {late} day(s) overdue with no resolution. "
                        f"Please check in with {who}.") if t.owner else (
                        f"🔴 “{t.title}” has no owner and is {late} day(s) overdue. "
                        "Please assign someone, or close it in Notion if it is no longer needed.")
                actions = ()
            out.append(FollowUp(tid, "overdue", step, role, f"{tid}:overdue:{step}:{t.due}", text, "off_track", actions))
        elif left == 0:
            out.append(FollowUp(tid, "due_today", 0, "owner", f"{tid}:due_today:{t.due}",
                                f"📅 “{t.title}” is due today. Is it done?", "info", OWNER_ACTIONS))
        elif left <= cfg.due_soon_days:
            out.append(FollowUp(tid, "due_soon", 0, "owner", f"{tid}:due_soon:{t.due}",
                                f"⏰ “{t.title}” is due {_d(t.due)} ({left} day(s) away). Is it on track?",
                                "at_risk" if risky else "info", OWNER_ACTIONS))
        elif t.last_update and (today - t.last_update).days > cfg.stale_days and t.status != Status.BLOCKED:
            n = (today - t.last_update).days
            out.append(FollowUp(tid, "stale", 0, "owner", f"{tid}:stale:{t.last_update}",
                                f"🟡 No update on “{t.title}” for {n} days. Quick status?", "info",
                                OWNER_ACTIONS))
    return out


def proposal_followups(unconfirmed: list[dict], now: datetime, cfg: FollowUpConfig = FollowUpConfig()) -> list[FollowUp]:
    """unconfirmed: rows with meeting_id and created_at (tz-aware) for proposals still in 'Proposed'."""
    by_meeting: dict[str, list[dict]] = {}
    for r in unconfirmed:
        by_meeting.setdefault(r["meeting_id"], []).append(r)
    out: list[FollowUp] = []
    for mid, rows in sorted(by_meeting.items()):
        age_h = (now - min(r["created_at"] for r in rows)).total_seconds() / 3600
        reached = [i for i, (h, _) in enumerate(cfg.proposal_ladder_hours) if age_h >= h]
        if not reached:
            continue
        step = reached[-1]
        out.append(FollowUp(f"meeting:{mid}", "proposals_unconfirmed", step, cfg.proposal_ladder_hours[step][1],
                            f"meeting:{mid}:proposals:{step}",
                            f"📝 {len(rows)} task(s) from a meeting are waiting for approval (never auto-approved). "
                            "Please review them.", "info", ()))
    return out


def resolve_recipient(role: str, owner: str | None, people: list[Person]) -> str | None:
    """Role -> person name. None means 'post to the team channel'."""
    by_id = {p.id: p for p in people}
    by_name = {p.name: p for p in people}
    lead = next((p for p in people if p.is_lead), None)
    if role == "owner":
        return owner or (lead.name if lead else None)
    if role == "lead":
        return lead.name if lead else owner
    if role == "backup":
        o = by_name.get(owner or "")
        if o and o.backup_id and o.backup_id in by_id:
            return by_id[o.backup_id].name
        return lead.name if lead else owner
    raise ValueError(f"unknown role {role}")
