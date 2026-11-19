from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from core.followups.hours import WorkingHours
from core.models import Goal, Person, Status, Task
from core.notifier import MemoryNotifier
from core.services import followup_tick

IST = ZoneInfo("Asia/Kolkata")
H = WorkingHours()
GOAL = Goal("g1", "Launch", 5, date(2027, 1, 10))
PEOPLE = [Person("p1", "Asha", is_lead=True), Person("p2", "Rahul", backup_id="p1")]


def task(status=Status.DOING, due=date(2026, 12, 8)):
    return Task("A", "API spec", "Rahul", due, 1, status, "g1")


def seed(db, **kw):
    db.upsert_people(PEOPLE)
    db.upsert_goals([GOAL])
    db.upsert_tasks([task(**kw)])


def test_sends_once_then_dedupes(db):
    seed(db)
    n, now = MemoryNotifier(), datetime(2026, 12, 7, 10, 0, tzinfo=IST)  # Monday 10:00
    r1 = followup_tick(db, n, now, H)
    assert (r1["sent"], len(n.sent)) == (1, 1) and n.sent[0].owner == "Rahul" and "due 8 Dec" in n.sent[0].text
    assert n.sent[0].items[0].actions == ("Done", "On track", "Slipping")
    r2 = followup_tick(db, n, now.replace(minute=15), H)
    assert r2["sent"] == 0 and len(n.sent) == 1          # same key: never twice


def test_after_hours_waits_for_next_window(db):
    seed(db)
    n = MemoryNotifier()
    r = followup_tick(db, n, datetime(2026, 12, 7, 20, 0, tzinfo=IST), H)   # Monday 20:00, closed
    assert (r["sent"], r["waiting"], len(n.sent)) == (0, 1, 0)
    r = followup_tick(db, n, datetime(2026, 12, 8, 9, 30, tzinfo=IST), H)   # Tuesday opening
    assert r["sent"] == 1 and n.sent[0].owner == "Rahul"


def test_cancelled_when_task_done_before_send(db):
    seed(db)
    n = MemoryNotifier()
    followup_tick(db, n, datetime(2026, 12, 7, 20, 0, tzinfo=IST), H)        # queued, not sent
    db.upsert_tasks([task(status=Status.DONE)])
    r = followup_tick(db, n, datetime(2026, 12, 8, 9, 30, tzinfo=IST), H)
    assert r["sent"] == 0 and n.sent == []
    with db.pool.connection() as c:
        assert c.execute("SELECT outcome FROM followups").fetchone()["outcome"] == "no_longer_applies"


def test_one_message_per_person_and_escalation_to_lead(db):
    seed(db, due=date(2026, 12, 4))   # 3 days overdue on Mon 7 Dec -> step 2 -> lead (Asha)
    n = MemoryNotifier()
    followup_tick(db, n, datetime(2026, 12, 7, 10, 0, tzinfo=IST), H)
    assert [a.owner for a in n.sent] == ["Asha"] and n.sent[0].severity == "off_track"
    assert "check in with Rahul" in n.sent[0].text
