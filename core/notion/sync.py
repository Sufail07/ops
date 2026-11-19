"""Notion <-> Ops Brain sync. Pull Projects (goals) and Tasks, detect what changed, alert on slips,
write computed outputs (priority, risk, projected finish, goal health) back to Notion.

Never edits anything a person owns (dates, owner, status, their own Priority). Only the Ops output columns.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core import services
from core.db import Database
from core.extractor.extract import extract_proposals
from core.graph.engine import CycleError, TaskGraph
from core.models import Task
from core.notifier import Notifier
from core.notion import mapping as m
from core.notion.client import NotionClient
from core.priority.engine import prioritise
from core.settings import Settings


@dataclass
class SyncResult:
    tasks_seen: int = 0
    goals_seen: int = 0
    tasks_changed: int = 0
    tasks_removed: int = 0
    baseline: bool = False
    alerts: int = 0
    writebacks: int = 0
    warnings: list[str] = field(default_factory=list)


log = logging.getLogger("ops.sync")


def sync_notion(db: Database, notifier: Notifier, notion: NotionClient, cfg: Settings, today: date,
                *, writeback: bool = True) -> SyncResult:
    res = SyncResult()
    goal_pages = notion.query_all(cfg.notion_projects_ds)
    task_pages = notion.query_all(cfg.notion_tasks_ds)

    goals = [g for p in goal_pages if (g := m.map_goal(p, res.warnings))]
    goal_ids = {g.id for g in goals}
    live_task_ids = {p["id"] for p in task_pages if not (p.get("in_trash") or p.get("archived"))}

    # One round trip for everything we need to know about the cache.
    cached_tasks, cached_goals, writes, previous, people, recent = db.load_sync_state(today)
    prev = {t.id: t for t in cached_tasks}
    tasks: list[Task] = []
    for p in task_pages:
        prior, w = prev.get(p["id"]), writes.get(p["id"])
        t = m.map_task(p, goal_ids, live_task_ids, res.warnings,
                       prev_last_update=prior.last_update if prior else None,
                       our_write_at=m.utc(w[1]) if w else None)
        if t:
            tasks.append(t)
    res.tasks_seen, res.goals_seen = len(tasks), len(goals)

    seen = {t.id for t in tasks}
    removed_tasks = [i for i in prev if i not in seen]
    removed_goals = [g.id for g in cached_goals if g.id not in goal_ids]
    db.delete_tasks(removed_tasks)
    db.delete_goals(removed_goals)
    res.tasks_removed = len(removed_tasks)

    cached_goal_map = {g.id: g for g in cached_goals}
    changed_goals = [g for g in goals if cached_goal_map.get(g.id) != g]
    changed_tasks = [t for t in tasks if prev.get(t.id) != t]
    res.tasks_changed = len(changed_tasks)

    res.baseline = not cached_tasks and not cached_goals
    if changed_tasks or changed_goals:
        keep_t = [t for t in cached_tasks if t.id in seen]
        keep_g = [g for g in cached_goals if g.id in goal_ids]
        res.alerts = len(services.apply_snapshot(db, notifier, changed_tasks, changed_goals, today,
                                                 quiet=res.baseline, state=(keep_t, keep_g, recent)))
    _sync_people(db, notion, tasks, people, res)
    for w_ in res.warnings:
        log.warning(w_)  # warnings describe Notion data to fix; logged, not stored as events (they repeat every poll)

    if writeback:
        res.writebacks = _write_back(db, notion, tasks, goals, writes, previous, today, res)
    return res


def _sync_people(db: Database, notion: NotionClient, tasks: list[Task], known: list, res: SyncResult) -> None:
    try:
        users = [u for u in notion.list_users() if u.get("type") == "person"]
        found = [{"id": u["id"], "name": u.get("name") or u["id"], "email": (u.get("person") or {}).get("email")}
                 for u in users]
    except Exception as e:  # noqa: BLE001  (users API may be unavailable to the token)
        res.warnings.append(f"Could not list Notion users ({type(e).__name__}); people derived from assignees only")
        found = [{"id": n, "name": n, "email": None} for n in sorted({t.owner for t in tasks if t.owner})]
    have = {(p.id, p.name, p.email) for p in known}
    new = [f for f in found if (f["id"], f["name"], f["email"]) not in have
           and not any(k.name == f["name"] for k in known if k.id != f["id"])]
    if new:
        db.ensure_people(new)


def _write_back(db: Database, notion: NotionClient, tasks: list[Task], goals: list, writes: dict,
                previous: dict, today: date, res: SyncResult) -> int:
    try:
        graph = TaskGraph(tasks, goals)
    except CycleError as e:
        res.warnings.append(f"Dependency cycle: {e}; outputs not written")
        return 0
    analysis = graph.analyze(today)
    items = {i.task_id: i for i in prioritise(graph, analysis, today, previous=previous)}
    desired: dict[str, dict[str, Any]] = {t.id: m.task_outputs(items.get(t.id), analysis[t.id]) for t in tasks}
    desired |= {g.id: m.goal_outputs(graph.goal_health(g.id, analysis)) for g in goals}

    done: list[tuple[str, str, datetime]] = []
    for page_id, props in desired.items():
        h = m.payload_hash(props)
        if page_id in writes and writes[page_id][0] == h:
            continue  # unchanged since our last write
        try:
            out = notion.update_page(page_id, props)
        except Exception as e:  # noqa: BLE001
            res.warnings.append(f"Write-back failed for {page_id}: {e}")
            continue
        edited = out.get("last_edited_time")
        done.append((page_id, h, m.parse_ts(edited) if edited else datetime.now(timezone.utc)))
    db.record_notion_writes(done)  # one round trip for all pages
    return len(done)


def extract_new_meetings(db: Database, notion: NotionClient, gateway: Any, cfg: Settings, today: date,
                         lookback_days: int = 14, notifier: Notifier | None = None) -> int:
    """Run the extractor once per recent meeting note. Stores PROPOSALS only; a person approves later."""
    since = (today - timedelta(days=lookback_days)).isoformat()
    pages = notion.query_all(cfg.notion_meetings_ds, {"property": "Date", "date": {"on_or_after": since}})
    goals, people_rows, extracted = db.load_extraction_state()
    people = {p.name: list(p.aliases) for p in people_rows}
    created = 0
    for page in pages:
        pid = page["id"]
        if page.get("in_trash") or page.get("archived") or pid in extracted:
            continue
        text = notion.page_text(pid)
        if len(text.strip()) < 40:
            continue  # empty or still being written; try again on the next poll
        d = m._date(page, "Date") or m.parse_ts(page["created_time"]).date()
        props = extract_proposals(gateway, text, d, goals, people)
        title = m._plain(m._prop(page, "Meeting name").get("title")) or "meeting"
        db.record_extraction(pid, [{**asdict(p), "due": str(p.due) if p.due else None, "meeting_title": title}
                                   for p in props])
        created += len(props)
        if notifier is not None and props:  # ask a person to approve; nothing is created until they do
            try:
                notifier.send_proposals(pid, title, db.get_proposals(pid), {g.id: g.name for g in goals})
                db.mark_card_sent(pid)
            except Exception as e:  # noqa: BLE001  the proposals are saved; the sweeper posts the card later
                log.warning("approval card for %s not posted yet (%s); the sweeper will retry", pid, e)
    return created
