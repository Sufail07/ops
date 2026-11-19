from datetime import date

import pytest

from core.alerts.format import build_slip_alerts
from core.graph.engine import CycleError, Risk, RiskParams, TaskGraph, slip_impacts
from core.models import Goal, Status, Task

TODAY = date(2026, 12, 1)
GOAL = Goal("g1", "Q1 launch", weight=5, target_date=date(2027, 1, 10))


def chain(a_due=date(2026, 12, 8)):
    """A -> B -> C -> D -> goal. Each depends on the previous one."""
    return [
        Task("A", "API spec", "Rahul", a_due, 3, depends_on=(), goal_id="g1"),
        Task("B", "Backend build", "Meera", date(2026, 12, 17), 5, depends_on=("A",), goal_id="g1"),
        Task("C", "QA pass", "Priya", date(2026, 12, 24), 4, depends_on=("B",), goal_id="g1"),
        Task("D", "Launch page", "Dev", date(2026, 12, 29), 2, depends_on=("C",), goal_id="g1"),
    ]


def test_healthy_chain_is_ok():
    g = TaskGraph(chain(), [GOAL])
    a = g.analyze(TODAY)
    assert all(s.risk == Risk.OK for s in a.values()), a
    assert a["D"].needed_by == date(2027, 1, 10)


def test_upstream_slip_reaches_every_downstream_task():
    before = TaskGraph(chain(), [GOAL])
    after = TaskGraph(chain(a_due=date(2026, 12, 20)), [GOAL])
    ia, ib = before.analyze(TODAY), after.analyze(TODAY)
    impacts = slip_impacts(ia, ib, after)
    hit = {i.task_id for i in impacts}
    # A slipped 10 days; B/C/D all inherit the delay through the chain
    assert {"A", "B", "C", "D"} <= hit
    d = next(i for i in impacts if i.task_id == "D")
    assert d.cause_chain == ("A", "B", "C", "D")
    assert d.goal_id == "g1"
    assert ib["A"].risk == Risk.OFF_TRACK      # A itself can no longer meet what B needs
    assert ib["D"].delay_days == 2             # 20 Dec + 5 + 4 + 2 = 31 Dec vs due 29 Dec
    assert ib["D"].risk == Risk.AT_RISK        # late vs its own date, still inside the goal date


def test_alerts_group_per_owner_and_explain_cause():
    before = TaskGraph(chain(), [GOAL])
    after = TaskGraph(chain(a_due=date(2026, 12, 20)), [GOAL])
    ia, ib = before.analyze(TODAY), after.analyze(TODAY)
    alerts = build_slip_alerts(slip_impacts(ia, ib, after), after, ib)
    owners = {a.owner for a in alerts}
    assert owners == {"Rahul", "Meera", "Priya", "Dev"}
    dev = next(a for a in alerts if a.owner == "Dev")
    assert "API spec" in dev.text and "Q1 launch" in dev.text
    assert "Nothing was changed automatically" in dev.text


def test_no_alert_when_nothing_changed():
    g = TaskGraph(chain(), [GOAL])
    a = g.analyze(TODAY)
    assert slip_impacts(a, a, g) == []


def test_done_upstream_does_not_block():
    ts = chain()
    ts[0] = Task("A", "API spec", "Rahul", date(2026, 11, 20), 3, Status.DONE, "g1")
    g = TaskGraph(ts, [GOAL])
    a = g.analyze(TODAY)
    assert a["A"].risk == Risk.DONE
    assert a["B"].critical_predecessor is None


def test_overdue_task_projects_from_today():
    ts = chain(a_due=date(2026, 11, 25))
    g = TaskGraph(ts, [GOAL])
    a = g.analyze(TODAY)
    assert a["A"].projected_finish == TODAY
    assert any("overdue" in r for r in a["A"].reasons)


def test_blocked_and_stale_push_dates_out():
    ts = chain()
    ts[0] = Task("A", "API spec", "Rahul", date(2026, 12, 10), 3, Status.BLOCKED, "g1", (), date(2026, 11, 1))
    a = TaskGraph(ts, [GOAL]).analyze(TODAY, RiskParams(blocked_penalty_days=3, stale_penalty_days=2))
    assert a["A"].projected_finish == date(2026, 12, 15)  # 10 Dec + 2 stale (blocked adds from today=1 Dec: max(10,1)+3=13) +2
    assert a["A"].delay_days == 5


def test_diamond_uses_latest_predecessor():
    ts = [
        Task("A", "A", "x", date(2026, 12, 5), 1),
        Task("B", "B", "y", date(2026, 12, 9), 1),
        Task("C", "C", "z", date(2026, 12, 15), 2, depends_on=("A", "B"), goal_id="g1"),
    ]
    a = TaskGraph(ts, [GOAL]).analyze(TODAY)
    assert a["C"].critical_predecessor is None  # due 15 Dec beats 9 Dec + 2
    ts[1] = Task("B", "B", "y", date(2026, 12, 20), 1)
    a = TaskGraph(ts, [GOAL]).analyze(TODAY)
    assert a["C"].critical_predecessor == "B"
    assert a["C"].projected_finish == date(2026, 12, 22)


def test_cycle_detected():
    ts = [Task("A", "A", depends_on=("B",)), Task("B", "B", depends_on=("A",))]
    with pytest.raises(CycleError) as e:
        TaskGraph(ts)
    assert set(e.value.members) == {"A", "B"}


def test_unknown_dependency_is_warned_not_crashed():
    g = TaskGraph([Task("A", "A", depends_on=("ghost",))])
    assert g.warnings and "unknown task id" in g.warnings[0]


def test_missing_dates_are_unknown_not_ok():
    ts = [Task("A", "A", "x", None), Task("B", "B", "y", date(2026, 12, 10), depends_on=("A",), goal_id="g1")]
    a = TaskGraph(ts, [GOAL]).analyze(TODAY)
    assert a["A"].risk == Risk.UNKNOWN
    assert any("no date" in r for r in a["B"].reasons)


def test_goal_health():
    ok = TaskGraph(chain(), [GOAL])
    assert ok.goal_health("g1", ok.analyze(TODAY)).status == "on_track"
    bad = TaskGraph(chain(a_due=date(2026, 12, 20)), [GOAL])
    h = bad.goal_health("g1", bad.analyze(TODAY))
    assert h.status == "off_track" and h.worst_task_id is not None
    near = TaskGraph(chain(a_due=date(2026, 12, 12)), [GOAL])
    assert near.goal_health("g1", near.analyze(TODAY)).status == "at_risk"


def test_downstream_and_upstream_order():
    g = TaskGraph(chain(), [GOAL])
    assert g.downstream("A") == ["B", "C", "D"]
    assert g.upstream("D") == ["A", "B", "C"]


def test_goal_with_no_tasks_is_not_reported_healthy():
    g = TaskGraph(chain(), [GOAL, Goal("g2", "Empty", 3, None)])
    h = g.goal_health("g2", g.analyze(TODAY))
    assert h.status == "no_tasks"
