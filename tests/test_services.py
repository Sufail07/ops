"""Done-test logic on a real Postgres: a 4-task chain, an upstream slip, every affected owner is alerted."""
from datetime import date

from core.models import Goal, Status, Task
from core.notifier import MemoryNotifier
from core.services import apply_snapshot, run_daily_plan

TODAY = date(2026, 12, 1)
GOAL = Goal("g1", "Q1 launch", 5, date(2027, 1, 10))


def chain(a_due=date(2026, 12, 8)):
    return [
        Task("A", "API spec", "Rahul", a_due, 3, goal_id="g1"),
        Task("B", "Backend build", "Meera", date(2026, 12, 17), 5, goal_id="g1", depends_on=("A",)),
        Task("C", "QA pass", "Priya", date(2026, 12, 24), 4, goal_id="g1", depends_on=("B",)),
        Task("D", "Launch page", "Dev", date(2026, 12, 29), 2, goal_id="g1", depends_on=("C",)),
    ]


def test_initial_load_is_quiet_then_slip_alerts_everyone(db):
    n = MemoryNotifier()
    assert apply_snapshot(db, n, chain(), [GOAL], TODAY) == []          # healthy chain: no noise
    with db.pool.connection() as c:  # events is append-only and shared across tests: count only our own rows
        ev_before = c.execute("SELECT count(*) AS n FROM events WHERE kind='risk.changed'").fetchone()["n"]
    alerts = apply_snapshot(db, n, [chain(a_due=date(2026, 12, 20))[0]], [], TODAY)  # only A changed
    assert {a.owner for a in alerts} == {"Rahul", "Meera", "Priya", "Dev"}
    assert n.sent == alerts
    with db.pool.connection() as c:
        rows = c.execute("SELECT task_id, new_risk, notified_at FROM risk_events").fetchall()
        ev = c.execute("SELECT count(*) AS n FROM events WHERE kind='risk.changed'").fetchone()["n"]
    assert {r["task_id"] for r in rows} == {"A", "B", "C", "D"} and all(r["notified_at"] for r in rows)
    assert ev - ev_before == 4


def test_same_slip_not_realerted_within_the_hour(db):
    n = MemoryNotifier()
    apply_snapshot(db, n, chain(), [GOAL], TODAY)
    a = chain(a_due=date(2026, 12, 20))[0]
    first = apply_snapshot(db, n, [a], [], TODAY)
    again = apply_snapshot(db, n, [chain()[0]], [], TODAY)  # back to normal
    third = apply_snapshot(db, n, [a], [], TODAY)           # same slip again within the hour
    assert first and again == [] and third == []


def test_cycle_is_rejected_and_nothing_saved(db):
    n = MemoryNotifier()
    apply_snapshot(db, n, chain(), [GOAL], TODAY)
    with db.pool.connection() as c:  # events is append-only and shared across tests: count only our own rows
        before = c.execute("SELECT count(*) AS n FROM events WHERE kind='graph.cycle_rejected'").fetchone()["n"]
    bad = Task("A", "API spec", "Rahul", date(2026, 12, 8), 3, goal_id="g1", depends_on=("D",))
    alerts = apply_snapshot(db, n, [bad], [], TODAY)
    assert len(alerts) == 1 and "circle" in alerts[0].text
    tasks, _ = db.load_graph_inputs()
    assert next(t for t in tasks if t.id == "A").depends_on == ()
    with db.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM events WHERE kind='graph.cycle_rejected'").fetchone()["n"] - before == 1


def test_daily_plan_uses_previous_snapshot(db):
    apply_snapshot(db, MemoryNotifier(), chain(), [GOAL], TODAY)
    items = run_daily_plan(db, TODAY)
    assert len(items) == 4 and items[0].rank == 1
    items2 = run_daily_plan(db, date(2026, 12, 2))
    assert [i.band for i in items2]  # second day reads yesterday's snapshot without error
