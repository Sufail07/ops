"""Plain-language slip alerts, one grouped message per affected person.

Pure formatting: no Slack calls here. Suggestions are proposals only; a person confirms any change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.graph.engine import Analysis, Impact, Risk, TaskGraph


@dataclass(frozen=True)
class AlertItem:
    """One task inside an alert, with the buttons that make sense for it (Done / On track / Slipping)."""
    task_id: str
    text: str
    actions: tuple[str, ...] = ()
    title: str | None = None


@dataclass(frozen=True)
class Alert:
    owner: str | None      # None = no owner: goes to the channel/lead
    severity: str          # "off_track" | "at_risk" | "info"
    text: str              # plain-text version (used for notifications and as the fallback)
    task_ids: tuple[str, ...]
    kind: str = "slip"     # slip | followup | system
    items: tuple[AlertItem, ...] = ()


def alert_to_row(a: Alert) -> tuple:
    """Alert -> values for alert_outbox (owner, severity, kind, text, task_ids, items)."""
    items = [{"task_id": i.task_id, "text": i.text, "actions": list(i.actions), "title": i.title} for i in a.items]
    return (a.owner, a.severity, a.kind, a.text, list(a.task_ids), items)


def alert_from_row(r: dict) -> Alert:
    items = tuple(AlertItem(i["task_id"], i["text"], tuple(i.get("actions", ())), i.get("title")) for i in r["items"])
    return Alert(r["owner"], r["severity"], r["text"], tuple(r["task_ids"]), r["kind"], items)


def _d(x: date | None) -> str:
    return f"{x.day} {x.strftime('%b')}" if x else "no date"


def build_slip_alerts(impacts: list[Impact], graph: TaskGraph, after: Analysis) -> list[Alert]:
    by_owner: dict[str | None, list[Impact]] = {}
    for imp in impacts:
        if imp.new_risk in (Risk.AT_RISK, Risk.OFF_TRACK) or imp.new_delay > imp.old_delay:
            by_owner.setdefault(graph.tasks[imp.task_id].owner, []).append(imp)

    alerts: list[Alert] = []
    for owner, imps in by_owner.items():
        lines: list[str] = []
        severity = "at_risk"
        for imp in imps:
            t = graph.tasks[imp.task_id]
            st = after[imp.task_id]
            if imp.new_risk == Risk.OFF_TRACK:
                severity = "off_track"
            icon = "🔴" if imp.new_risk == Risk.OFF_TRACK else "🟠"
            line = f"{icon} “{t.title}” (due {_d(t.due)})"
            if imp.new_delay > 0:
                line += f" is now expected {_d(st.projected_finish)}, {imp.new_delay} day(s) late."
            else:
                line += f" has only {st.slack_days} day(s) of slack left."
            root = imp.cause_chain[0]
            if root != imp.task_id:
                rt = graph.tasks[root]
                line += f"\n   Because: “{rt.title}” ({rt.owner or 'no owner'}) is now expected {_d(after[root].projected_finish)}."
            else:
                why = "; ".join(st.reasons)
                if why:
                    line += f"\n   Because: {why}."
            chain = [graph.tasks[i].title for i in imp.path_to_goal]
            goal = graph.goals.get(imp.goal_id) if imp.goal_id else None
            tail = f" → Goal “{goal.name}” (target {_d(goal.target_date)})" if goal else ""
            line += f"\n   Puts at risk: {' → '.join(f'“{c}”' for c in chain)}{tail}"
            lines.append(line)
        who = owner or "Team lead"
        header = f"Heads up, {who}: a delay upstream affects {'your task' if len(imps) == 1 else f'{len(imps)} of your tasks'}."
        footer = "Suggested next step: talk to the owner of the slipped task, then confirm a new date. Nothing was changed automatically."
        items = tuple(AlertItem(i.task_id, ln, ("Slipping",), graph.tasks[i.task_id].title) for i, ln in zip(imps, lines))
        alerts.append(Alert(owner, severity, "\n".join([header, *lines, footer]), tuple(i.task_id for i in imps),
                            kind="slip", items=items))
    alerts.sort(key=lambda a: (a.severity != "off_track", str(a.owner)))
    return alerts
