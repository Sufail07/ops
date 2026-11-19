"""Benchmark the DB-heavy operations on the Neon TEST branch. Run before/after changing the data layer.
   python scripts/bench_db.py
"""
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.settings import get_settings  # noqa: E402

S = get_settings()
assert S.test_database_url, "TEST_DATABASE_URL missing"
os.environ["DATABASE_URL"] = S.test_database_url
get_settings.cache_clear()

import psycopg  # noqa: E402
from core.db import Database  # noqa: E402
from core.followups.hours import WorkingHours  # noqa: E402
from core.models import Goal, Person, Task  # noqa: E402
from core.notifier import MemoryNotifier  # noqa: E402
from core.services import apply_snapshot, followup_tick, run_daily_plan  # noqa: E402

TODAY = date(2026, 12, 1)
GOAL = Goal("g1", "Q1 launch", 5, date(2027, 1, 10))


def chain(a_due=date(2026, 12, 8)):
    return [Task("A", "API spec", "Rahul", a_due, 3, goal_id="g1", last_update=TODAY),
            Task("B", "Backend build", "Meera", date(2026, 12, 17), 5, goal_id="g1", depends_on=("A",), last_update=TODAY),
            Task("C", "QA pass", "Priya", date(2026, 12, 24), 4, goal_id="g1", depends_on=("B",), last_update=TODAY),
            Task("D", "Launch page", "Dev", date(2026, 12, 29), 2, goal_id="g1", depends_on=("C",), last_update=TODAY)]


def timed(label, fn):
    t = time.monotonic()
    out = fn()
    print(f"  {label:<38}{(time.monotonic() - t):6.2f} s")
    return out


with psycopg.connect(S.test_database_url_unpooled or S.test_database_url, autocommit=True) as c:
    c.execute("TRUNCATE notion_writes, risk_events, followups, proposals, priority_snapshots, edges, tasks, goals, people, inbox CASCADE")

t0 = time.monotonic()
db = Database(S.test_database_url)
db.load_graph_inputs()  # first query also opens the pool
print(f"pool open + first query: {time.monotonic() - t0:.2f} s  (connection setup, paid once per process)")
n = MemoryNotifier()
print("operations (warm connection):")
timed("apply_snapshot: baseline 4 tasks", lambda: apply_snapshot(db, n, chain(), [GOAL], TODAY, quiet=True))
timed("apply_snapshot: slip -> 4 alerts", lambda: apply_snapshot(db, n, [chain(date(2026, 12, 20))[0]], [], TODAY))
assert len(n.sent) == 4
db.upsert_people([Person("p1", "Asha", is_lead=True), Person("p2", "Rahul", backup_id="p1")])
ist = ZoneInfo("Asia/Kolkata")
timed("followup_tick", lambda: followup_tick(db, n, datetime(2026, 12, 7, 10, 0, tzinfo=ist), WorkingHours()))
timed("run_daily_plan", lambda: run_daily_plan(db, TODAY))
timed("load_graph_inputs", db.load_graph_inputs)
timed("append_event", lambda: db.append_event("bench", "x", {}, "bench"))
db.close()
