from datetime import date, datetime, timedelta, timezone

import pytest

from core.models import Status
from core.notifier import MemoryNotifier
from core.notion import mapping as m
from core.notion.sync import extract_new_meetings, sync_notion
from core.settings import Settings

TODAY = date(2026, 12, 1)
NOW = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)
CFG = Settings(database_url="postgresql://x", notion_tasks_ds="T", notion_projects_ds="P", notion_meetings_ds="M")


def goal_page(id="g1", name="Q1 launch", weight=5, target="2027-01-10"):
    return {"id": id, "last_edited_time": "2026-11-20T00:00:00.000Z", "properties": {
        "Name": {"title": [{"plain_text": name}]}, "Weight": {"number": weight},
        "Target date": {"date": {"start": target} if target else None}}}


def task_page(id, title, owner="Rahul", due="2026-12-08", status="In progress", est=3, goal="g1", deps=(),
              edited="2026-11-30T10:00:00.000Z"):
    return {"id": id, "last_edited_time": edited, "properties": {
        "Task name": {"title": [{"plain_text": title}]}, "Due date": {"date": {"start": due} if due else None},
        "Status": {"status": {"name": status}}, "Assignee": {"people": [{"id": "u-" + owner, "name": owner}] if owner else []},
        "Estimate (days)": {"number": est}, "Goal": {"relation": [{"id": goal}] if goal else []},
        "Depends on": {"relation": [{"id": d} for d in deps]}}}


class FakeNotion:
    def __init__(self):
        self.ds = {"P": [goal_page()], "T": [], "M": []}
        self.updates: list[tuple[str, dict]] = []
        self.texts: dict[str, str] = {}

    def query_all(self, ds, filter=None):
        return self.ds[ds]

    def update_page(self, page_id, props):
        self.updates.append((page_id, props))
        return {"last_edited_time": NOW.isoformat()}

    def list_users(self):
        return [{"id": "u-Rahul", "type": "person", "name": "Rahul", "person": {"email": "r@x.com"}}]

    def page_text(self, pid):
        return self.texts[pid]


def chain():
    return [task_page("A", "API spec", "Rahul", "2026-12-08", est=3),
            task_page("B", "Backend build", "Meera", "2026-12-17", est=5, deps=["A"]),
            task_page("C", "QA pass", "Priya", "2026-12-24", est=4, deps=["B"]),
            task_page("D", "Launch page", "Dev", "2026-12-29", est=2, deps=["C"])]


# ---------- mapping (pure) ----------
def test_mapping_basic_and_defaults():
    w = []
    t = m.map_task(task_page("A", "Spec", est=None, status="Blocked"), {"g1"}, {"A"}, w)
    assert (t.status, t.estimate_days, t.owner, t.goal_id, t.due) == (Status.BLOCKED, 1, "Rahul", "g1", date(2026, 12, 8))
    assert w == []


def test_owner_falls_back_to_the_owner_name_column():
    page = task_page("A", "Spec", owner=None)
    page["properties"]["Owner (name)"] = {"rich_text": [{"plain_text": "Rahul"}]}
    assert m.map_task(page, {"g1"}, {"A"}, []).owner == "Rahul"
    page["properties"]["Assignee"] = {"people": [{"id": "u1", "name": "Meera"}]}   # a real assignee wins
    assert m.map_task(page, {"g1"}, {"A"}, []).owner == "Meera"


def test_mapping_warns_instead_of_crashing():
    w = []
    t = m.map_task(task_page("A", "Spec", status="Weird", goal="gone", deps=["ghost", "A"]), {"g1"}, {"A"}, w)
    assert t.status == Status.TODO and t.goal_id is None and t.depends_on == ()
    assert len(w) == 2
    w2 = []
    assert m.map_goal(goal_page(weight=9), w2).weight == 3 and "Weight" in w2[0]


def test_archived_pages_are_skipped():
    p = task_page("A", "x")
    p["in_trash"] = True
    assert m.map_task(p, set(), set(), []) is None


def test_own_writeback_is_not_a_human_update():
    p = task_page("A", "x", edited="2026-12-01T06:00:30.000Z")
    wrote = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)
    kept = m.map_task(p, {"g1"}, {"A"}, [], prev_last_update=date(2026, 11, 1), our_write_at=wrote)
    assert kept.last_update == date(2026, 11, 1)
    later = task_page("A", "x", edited="2026-12-01T09:00:00.000Z")
    assert m.map_task(later, {"g1"}, {"A"}, [], prev_last_update=date(2026, 11, 1), our_write_at=wrote).last_update == date(2026, 12, 1)


# ---------- sync (real Postgres, fake Notion) ----------
def test_first_sync_is_a_quiet_baseline_and_writes_outputs(db):
    n, notion = MemoryNotifier(), FakeNotion()
    notion.ds["T"] = chain()
    r = sync_notion(db, n, notion, CFG, TODAY)
    assert (r.tasks_seen, r.goals_seen, r.baseline, r.alerts) == (4, 1, True, 0) and n.sent == []
    assert r.writebacks == 5                                   # 4 tasks + 1 goal
    page, props = next(u for u in notion.updates if u[0] == "A")
    assert props["Ops priority"]["select"]["name"] in ("P0", "P1", "P2", "P3")
    assert props["Risk"]["select"]["name"] == "On track" and props["Projected finish"]["date"]["start"] == "2026-12-08"
    goal_props = next(p for pid, p in notion.updates if pid == "g1")
    assert goal_props["Health"]["select"]["name"] == "On track"


def test_second_sync_with_no_changes_does_nothing(db):
    n, notion = MemoryNotifier(), FakeNotion()
    notion.ds["T"] = chain()
    sync_notion(db, n, notion, CFG, TODAY)
    notion.updates.clear()
    r = sync_notion(db, n, notion, CFG, TODAY)
    assert (r.tasks_changed, r.alerts, r.writebacks, notion.updates) == (0, 0, 0, [])


def test_person_moves_due_date_then_everyone_downstream_is_alerted_and_outputs_update(db):
    n, notion = MemoryNotifier(), FakeNotion()
    notion.ds["T"] = chain()
    sync_notion(db, n, notion, CFG, TODAY)
    notion.updates.clear()
    notion.ds["T"][0] = task_page("A", "API spec", "Rahul", "2026-12-20", est=3, edited="2026-12-01T09:00:00.000Z")
    r = sync_notion(db, n, notion, CFG, TODAY)
    assert r.tasks_changed == 1 and r.alerts == 4
    assert sorted(a.owner for a in n.sent) == ["Dev", "Meera", "Priya", "Rahul"]
    risks = {pid: p["Risk"]["select"]["name"] for pid, p in notion.updates if "Risk" in p}
    assert risks["A"] == "Off track" and risks["D"] == "At risk"
    goal_props = next(p for pid, p in notion.updates if pid == "g1")
    assert goal_props["Health"]["select"]["name"] == "Off track"


def test_deleted_page_and_status_done_handled(db):
    n, notion = MemoryNotifier(), FakeNotion()
    notion.ds["T"] = chain()
    sync_notion(db, n, notion, CFG, TODAY)
    notion.ds["T"] = [notion.ds["T"][0], notion.ds["T"][1], task_page("C", "QA pass", "Priya", "2026-12-24", status="Done", deps=["B"], edited="2026-12-01T09:00:00.000Z")]
    notion.updates.clear()
    r = sync_notion(db, n, notion, CFG, TODAY)
    assert r.tasks_removed == 1
    done = next(p for pid, p in notion.updates if pid == "C")
    assert done["Risk"] == {"select": None} and done["Ops priority"] == {"select": None}


def test_people_synced_without_clobbering_lead_settings(db):
    from core.models import Person
    db.upsert_people([Person("u-Rahul", "Rahul", is_lead=True)])
    notion = FakeNotion()
    notion.ds["T"] = chain()
    sync_notion(db, MemoryNotifier(), notion, CFG, TODAY)
    p = next(x for x in db.load_people() if x.id == "u-Rahul")
    assert p.is_lead is True


def test_meeting_extraction_runs_once_and_stores_proposals_only(db):
    from types import SimpleNamespace
    import json
    from core.gateway import Gateway, Routing
    reply = json.dumps({"tasks": [{"id": "t1", "title": "Write spec", "owner_name": "Rahul", "due_date": "2026-12-05",
                                   "goal_id": "g1", "confidence": 0.9}]})
    calls = []

    def completion(model, **kw):
        calls.append(model)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    gw = Gateway(Routing("t", {"T2": ["p/a"]}, {"extract_tasks": {"tiers": ["T2"]}}), completion)
    notion = FakeNotion()
    notion.ds["T"] = chain()
    notion.ds["M"] = [{"id": "mtg1", "created_time": "2026-11-30T00:00:00Z", "properties": {"Date": {"date": {"start": "2026-11-30"}}}}]
    notion.texts["mtg1"] = "Rahul will write the API spec by 5 December for the Q1 launch goal."
    sync_notion(db, MemoryNotifier(), notion, CFG, TODAY)
    assert extract_new_meetings(db, notion, gw, CFG, TODAY) == 1
    assert extract_new_meetings(db, notion, gw, CFG, TODAY) == 0 and len(calls) == 1   # once per meeting
    with db.pool.connection() as c:
        row = c.execute("SELECT status, payload FROM proposals").fetchone()
        assert row["status"] == "Proposed" and row["payload"]["owner"] == "Rahul"
        assert c.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 4   # no task was created
