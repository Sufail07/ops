from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.followups.hours import WorkingHours
from core.followups.rules import FollowUpConfig, due_followups, proposal_followups, resolve_recipient
from core.graph.engine import TaskGraph
from core.models import Goal, Person, Status, Task

IST = ZoneInfo("Asia/Kolkata")
H = WorkingHours()
GOAL = Goal("g1", "Launch", 5, date(2027, 1, 10))


def ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# ---- working hours (2026-11-23 is a Monday, 2026-11-29 a Sunday) ----
def test_open_inside_window_closed_outside():
    assert H.is_open(ist(2026, 11, 23, 9, 30)) and H.is_open(ist(2026, 11, 23, 18, 29))
    assert not H.is_open(ist(2026, 11, 23, 9, 29)) and not H.is_open(ist(2026, 11, 23, 18, 30))


def test_saturday_open_sunday_closed():
    assert H.is_open(ist(2026, 11, 28, 12, 0))      # Saturday
    assert not H.is_open(ist(2026, 11, 29, 12, 0))  # Sunday


def test_next_open_rolls_forward():
    assert H.next_open(ist(2026, 11, 23, 7, 0)) == ist(2026, 11, 23, 9, 30)      # before opening: today
    assert H.next_open(ist(2026, 11, 23, 12, 0)) == ist(2026, 11, 23, 12, 0)     # open: now
    assert H.next_open(ist(2026, 11, 23, 19, 0)) == ist(2026, 11, 24, 9, 30)     # after close: tomorrow
    assert H.next_open(ist(2026, 11, 28, 19, 0)) == ist(2026, 11, 30, 9, 30)     # Sat evening: Monday


def test_holiday_is_skipped():
    h = WorkingHours(holidays=frozenset({date(2026, 11, 24)}))
    assert h.next_open(ist(2026, 11, 23, 19, 0)) == ist(2026, 11, 25, 9, 30)


def test_utc_input_is_converted():
    assert H.is_open(datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc))  # 10:00 IST
    assert not H.is_open(datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc))  # 07:30 IST


# ---- rules ----
TODAY = date(2026, 12, 10)


def graph(**kw):
    t = Task("A", "API spec", "Rahul", kw.get("due"), 1, kw.get("status", Status.DOING), "g1",
             last_update=kw.get("last_update"))
    g = TaskGraph([t], [GOAL])
    return g, g.analyze(TODAY)


def kinds(**kw):
    g, a = graph(**kw)
    return [(f.kind, f.step, f.recipient_role) for f in due_followups(g, a, TODAY)]


def test_due_soon_today_and_not_yet():
    assert kinds(due=date(2026, 12, 12)) == [("due_soon", 0, "owner")]
    assert kinds(due=date(2026, 12, 10)) == [("due_today", 0, "owner")]
    assert kinds(due=date(2026, 12, 20)) == []


def test_overdue_escalation_ladder():
    assert kinds(due=date(2026, 12, 9)) == [("overdue", 0, "owner")]
    assert kinds(due=date(2026, 12, 8)) == [("overdue", 1, "owner")]
    assert kinds(due=date(2026, 12, 7)) == [("overdue", 2, "lead")]
    assert kinds(due=date(2026, 12, 4)) == [("overdue", 3, "backup")]


def test_done_tasks_and_missing_dates_get_no_followup():
    assert kinds(due=date(2026, 12, 1), status=Status.DONE) == []
    assert kinds(due=None) == []


def test_stale_only_when_not_otherwise_due():
    assert kinds(due=date(2026, 12, 30), last_update=date(2026, 12, 1)) == [("stale", 0, "owner")]
    assert kinds(due=date(2026, 12, 30), last_update=date(2026, 12, 8)) == []


def test_dedupe_key_changes_when_due_date_changes():
    g1, a1 = graph(due=date(2026, 12, 12))
    g2, a2 = graph(due=date(2026, 12, 11))
    assert due_followups(g1, a1, TODAY)[0].dedupe_key != due_followups(g2, a2, TODAY)[0].dedupe_key


def test_rules_never_touch_dates_owners_or_priority():
    g, a = graph(due=date(2026, 12, 4))
    before = dict(g.tasks)
    due_followups(g, a, TODAY)
    assert g.tasks == before


def test_proposal_followups_wait_then_escalate():
    now = datetime(2026, 12, 10, 12, 0, tzinfo=timezone.utc)
    mk = lambda h: [{"meeting_id": "m1", "created_at": now.replace(hour=12) - __import__("datetime").timedelta(hours=h)}]
    assert proposal_followups(mk(10), now) == []
    assert [(f.step, f.recipient_role) for f in proposal_followups(mk(25), now)] == [(0, "lead")]
    assert [(f.step, f.recipient_role) for f in proposal_followups(mk(49), now)] == [(1, "backup")]


def test_resolve_recipient():
    people = [Person("p1", "Asha", is_lead=True), Person("p2", "Rahul", backup_id="p3"), Person("p3", "Meera")]
    assert resolve_recipient("owner", "Rahul", people) == "Rahul"
    assert resolve_recipient("lead", "Rahul", people) == "Asha"
    assert resolve_recipient("backup", "Rahul", people) == "Meera"
    assert resolve_recipient("backup", "Meera", people) == "Asha"      # no backup set: falls to lead
    assert resolve_recipient("owner", None, people) == "Asha"          # unowned: lead
    assert resolve_recipient("lead", "Rahul", []) == "Rahul"
    with pytest.raises(ValueError):
        resolve_recipient("boss", "Rahul", people)


def test_ownerless_overdue_escalation_reads_naturally():
    t = Task("A", "API spec", None, date(2026, 12, 4), 1, Status.TODO, "g1")
    g = TaskGraph([t], [GOAL])
    (f,) = due_followups(g, g.analyze(TODAY), TODAY)
    assert "has no owner" in f.text and "someone" in f.text and "(someone)" not in f.text
