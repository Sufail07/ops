from datetime import date, timedelta

from core.graph.engine import TaskGraph
from core.models import Goal, Status, Task
from core.priority.engine import PrevSnapshot, PriorityParams, prioritise, stable_band

TODAY = date(2026, 12, 1)
P = PriorityParams()


def graph(goal_weight=5):
    goals = [Goal("g1", "Q1 launch", goal_weight, date(2026, 12, 31)), Goal("g2", "Hiring", 1, date(2027, 3, 31))]
    tasks = [
        Task("A", "API spec", "Rahul", date(2026, 12, 6), 3, goal_id="g1"),
        Task("B", "Backend", "Meera", date(2026, 12, 20), 5, depends_on=("A",), goal_id="g1"),
        Task("H", "Hiring plan", "Priya", date(2027, 1, 15), 2, goal_id="g2"),
        Task("N", "Misc", "Dev", date(2026, 12, 3), 1),
    ]
    return TaskGraph(tasks, goals)


def run(g, today=TODAY, previous=None):
    return prioritise(g, g.analyze(today), today, P, previous)


def test_goal_weight_and_dependencies_drive_rank():
    items = run(graph())
    order = [i.task_id for i in items]
    assert order.index("A") < order.index("H")      # high-weight, blocking, near deadline first
    assert order[-1] in ("H", "N")                  # low-weight goal / no goal last
    assert items[0].rank == 1


def test_every_item_explains_itself():
    for i in run(graph()):
        assert i.why.startswith(i.band) and i.reasons


def test_done_tasks_not_ranked():
    g = graph()
    t = dict(g.tasks)
    t["N"] = Task("N", "Misc", "Dev", date(2026, 12, 3), 1, Status.DONE)
    g2 = TaskGraph(t.values(), g.goals.values())
    assert "N" not in {i.task_id for i in run(g2)}


def test_hysteresis_blocks_noise_but_allows_real_change():
    # score just over the P2->P1 boundary (1.8) but inside the margin: stays P2
    assert stable_band(1.9, "P2", real_change=False, p=P) == 2
    # clearly past the margin: promotes
    assert stable_band(2.2, "P2", real_change=False, p=P) == 1
    # a real event moves it immediately
    assert stable_band(1.9, "P2", real_change=True, p=P) == 1
    # demotion also needs margin
    assert stable_band(1.7, "P1", real_change=False, p=P) == 1
    assert stable_band(1.4, "P1", real_change=False, p=P) == 2


def test_bands_stay_stable_over_quiet_days():
    g = graph()
    prev: dict[str, PrevSnapshot] = {}
    bands_by_day = []
    for n in range(0, 6):
        day = TODAY + timedelta(days=n)
        items = run(g, day, prev)
        prev = {i.task_id: PrevSnapshot(i.band, i.fingerprint) for i in items}
        bands_by_day.append({i.task_id: i.band for i in items})
    for tid in ("H", "N"):
        changes = sum(1 for a, b in zip(bands_by_day, bands_by_day[1:]) if a[tid] != b[tid])
        assert changes <= 1, (tid, bands_by_day)


def test_upstream_slip_is_a_real_event_that_reprioritises():
    g0 = graph()
    items0 = run(g0)
    prev = {i.task_id: PrevSnapshot(i.band, i.fingerprint) for i in items0}
    slipped = TaskGraph([Task("A", "API spec", "Rahul", date(2026, 12, 18), 3, goal_id="g1"),
                         *[t for t in g0.tasks.values() if t.id != "A"]], g0.goals.values())
    items1 = run(slipped, previous=prev)
    a0 = next(i for i in items0 if i.task_id == "A")
    a1 = next(i for i in items1 if i.task_id == "A")
    assert a1.fingerprint != a0.fingerprint
    assert any("risk" in r for r in a1.reasons)
