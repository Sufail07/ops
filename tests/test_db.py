import os

from datetime import date

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from core.db import Database, PgRunLog
from core.gateway import Gateway, Routing
from core.graph.engine import TaskGraph
from core.models import Goal, Status, Task
from core.priority.engine import prioritise

GOAL = Goal("g1", "Launch", 5, date(2027, 1, 10))
TASKS = [
    Task("A", "Spec", "Rahul", date(2026, 12, 8), 3, goal_id="g1"),
    Task("B", "Build", "Meera", date(2026, 12, 17), 5, Status.DOING, "g1", ("A",)),
]


def test_graph_round_trip(db):
    db.upsert_goals([GOAL])
    db.upsert_tasks(TASKS)
    tasks, goals = db.load_graph_inputs()
    assert goals == [GOAL]
    assert {t.id: t for t in tasks} == {t.id: t for t in TASKS}
    TaskGraph(tasks, goals).analyze(date(2026, 12, 1))  # usable by the engine


def test_upsert_rewrites_edges(db):
    db.upsert_goals([GOAL])
    db.upsert_tasks(TASKS)
    db.upsert_tasks([Task("B", "Build", "Meera", date(2026, 12, 17), 5, Status.DOING, "g1", ())])
    tasks, _ = db.load_graph_inputs()
    assert next(t for t in tasks if t.id == "B").depends_on == ()


def test_events_are_append_only(db):
    eid = db.append_event("task.slipped", "A", {"days": 3}, "system")
    assert eid > 0
    with db.pool.connection() as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("UPDATE events SET kind='x'")
    with db.pool.connection() as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("DELETE FROM events")
    with db.pool.connection() as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("TRUNCATE events")


def test_duplicate_webhook_is_noop(db):
    assert db.record_inbox("evt-1", "notion") is True
    assert db.record_inbox("evt-1", "notion") is False
    assert db.record_inbox("evt-2", "notion") is True


def test_priority_snapshot_round_trip(db):
    g = TaskGraph(TASKS, [GOAL])
    today = date(2026, 12, 1)
    items = prioritise(g, g.analyze(today), today)
    db.save_priority_snapshot(today, items)
    db.save_priority_snapshot(today, items)  # idempotent
    prev = db.load_previous_snapshot(date(2026, 12, 2))
    assert set(prev) == {i.task_id for i in items}
    assert db.load_previous_snapshot(today) == {}


def test_run_log_writes_gateway_attempts(db):
    from types import SimpleNamespace
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int

    def completion(model, **kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"x": 1}'))],
                               usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))

    routing = Routing("v9", {"T2": ["prov/a"]}, {"job": {"tiers": ["T2"]}})
    Gateway(routing, completion, PgRunLog(db)).call_json("job", [], Out, trigger="test")
    with db.pool.connection() as c:
        r = c.execute("SELECT * FROM runs").fetchone()
    assert (r["job_type"], r["model"], r["tokens_in"], r["tokens_out"], r["ok"], r["config_version"]) == \
        ("job", "prov/a", 7, 3, True, "v9")


def test_constraints_reject_bad_data(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.upsert_goals([Goal("bad", "x", 9)])
    with db.pool.connection() as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO tasks(id,title,status) VALUES ('Z','z','bogus')")


def test_migration_downgrade_and_upgrade(test_db_url):
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    with psycopg.connect(os.environ["DATABASE_URL_UNPOOLED"]) as c:
        assert c.execute("SELECT to_regclass('public.tasks')").fetchone()[0] is None
    command.upgrade(cfg, "head")
    with psycopg.connect(os.environ["DATABASE_URL_UNPOOLED"]) as c:
        assert c.execute("SELECT to_regclass('public.tasks')").fetchone()[0] is not None
