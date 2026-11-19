"""End-to-end smoke on Neon: Procrastinate schema -> enqueue -> worker -> graph engine -> alerts -> Postgres.

  python scripts/smoke_queue.py            # runs on the TEST branch (default, safe)
  python scripts/smoke_queue.py --target prod --schema-only   # only ensure the queue schema exists on prod
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("--target", choices=["test", "prod"], default="test")
ap.add_argument("--schema-only", action="store_true")
args = ap.parse_args()

from core.settings import get_settings  # noqa: E402

S = get_settings()
if args.target == "test":
    assert S.test_database_url, "TEST_DATABASE_URL missing"
    os.environ["DATABASE_URL"] = S.test_database_url
    os.environ["DATABASE_URL_UNPOOLED"] = S.test_database_url_unpooled or S.test_database_url
    get_settings.cache_clear()

from core.db import Database  # noqa: E402
from core.notifier import MemoryNotifier  # noqa: E402
from core.queue import app  # noqa: E402
from core.runtime import Runtime, set_runtime  # noqa: E402
import core.jobs as jobs  # noqa: E402,F401  (registers tasks)

GOAL = {"id": "g1", "name": "Q1 launch", "weight": 5, "target_date": "2027-01-10"}


def t(id, title, owner, due, est, deps=()):
    return {"id": id, "title": title, "owner": owner, "due": due, "estimate_days": est, "goal_id": "g1",
            "depends_on": list(deps)}


CHAIN = [t("A", "API spec", "Rahul", "2026-12-08", 3), t("B", "Backend build", "Meera", "2026-12-17", 5, ["A"]),
         t("C", "QA pass", "Priya", "2026-12-24", 4, ["B"]), t("D", "Launch page", "Dev", "2026-12-29", 2, ["C"])]


async def main() -> None:
    async with app.open_async():
        await app.schema_manager.apply_schema_async()
        print("procrastinate schema: ok")
        if args.schema_only:
            return
        db = Database(os.environ["DATABASE_URL"])
        with db.pool.connection() as c:
            for tbl in ("risk_events", "edges", "tasks", "goals"):
                c.execute(f"DELETE FROM {tbl}")
        n = MemoryNotifier()
        set_runtime(Runtime(db, n))
        today = "2026-12-01"
        await jobs.apply_snapshot.defer_async(tasks=CHAIN, goals=[GOAL], today=today)
        await app.run_worker_async(wait=False)
        print(f"healthy chain loaded, alerts: {len(n.sent)} (expect 0)")
        slipped = [t("A", "API spec", "Rahul", "2026-12-20", 3)]
        t0 = time.monotonic()
        await jobs.apply_snapshot.defer_async(tasks=slipped, goals=[], today=today)
        await app.run_worker_async(wait=False)
        dt = time.monotonic() - t0
        owners = sorted(a.owner for a in n.sent)
        print(f"slip -> alerts: {owners} in {dt:.2f}s via queue+worker on Neon")
        print("--- sample alert (Dev) ---\n" + next(a.text for a in n.sent if a.owner == "Dev"))
        with db.pool.connection() as c:
            rows = c.execute("SELECT task_id,new_risk FROM risk_events ORDER BY task_id").fetchall()
            jobs_done = c.execute("SELECT status, count(*) AS n FROM procrastinate_jobs GROUP BY status").fetchall()
        print("risk_events:", [(r['task_id'], r['new_risk']) for r in rows])
        print("procrastinate_jobs:", [(r['status'], r['n']) for r in jobs_done])
        assert owners == ["Dev", "Meera", "Priya", "Rahul"]
        db.close()


asyncio.run(main())
