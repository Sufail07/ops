import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from procrastinate.testing import InMemoryConnector

from core.app import create_app
from core.notifier import MemoryNotifier
from core.queue import app as queue_app
from core.runtime import Runtime, set_runtime
from core.settings import Settings

GOAL = {"id": "g1", "name": "Q1 launch", "weight": 5, "target_date": "2027-01-10"}


def t(id, title, owner, due, est, deps=()):
    return {"id": id, "title": title, "owner": owner, "due": due, "estimate_days": est, "goal_id": "g1",
            "depends_on": list(deps)}


CHAIN = [t("A", "API spec", "Rahul", "2026-12-08", 3), t("B", "Backend build", "Meera", "2026-12-17", 5, ["A"]),
         t("C", "QA pass", "Priya", "2026-12-24", 4, ["B"]), t("D", "Launch page", "Dev", "2026-12-29", 2, ["C"])]
AUTH = {"Authorization": "Bearer adm"}


def settings(test_db_url, **kw):
    base = dict(database_url=test_db_url, admin_token="adm", notion_webhook_secret="whsec", run_worker=False)
    return Settings(**(base | kw))


@pytest.fixture()
def client(db, test_db_url):
    n = MemoryNotifier()
    with queue_app.replace_connector(InMemoryConnector()):
        api = create_app(Runtime(db, n), settings(test_db_url), run_worker=False)
        with TestClient(api) as c:
            c.notifier = n
            yield c
    set_runtime(None)


def sign(body: bytes, secret="whsec") -> dict:
    return {"X-Notion-Signature": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def run_worker():
    asyncio.run(queue_app.run_worker_async(wait=False, install_signal_handlers=False))


def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_webhook_rejects_bad_or_missing_signature(client):
    body = json.dumps({"id": "e1", "type": "page.properties_updated"}).encode()
    assert client.post("/hooks/notion", content=body, headers={"Content-Type": "application/json"}).status_code == 401
    assert client.post("/hooks/notion", content=body, headers=sign(body, "wrong")).status_code == 401


def test_webhook_queues_then_dedupes_and_processes(client, db):
    body = json.dumps({"id": "e2", "type": "page.properties_updated", "entity": {"id": "page-1"}}).encode()
    assert client.post("/hooks/notion", content=body, headers=sign(body)).json() == {"status": "queued"}
    assert client.post("/hooks/notion", content=body, headers=sign(body)).json() == {"status": "duplicate"}
    run_worker()
    with db.pool.connection() as c:
        assert c.execute("SELECT processed_at FROM inbox WHERE event_id='e2'").fetchone()["processed_at"]
        assert c.execute("SELECT count(*) AS n FROM events WHERE kind='notion.event_received'").fetchone()["n"] == 1


def test_webhook_fails_closed_without_secret(db, test_db_url):
    with queue_app.replace_connector(InMemoryConnector()):
        api = create_app(Runtime(db, MemoryNotifier()), settings(test_db_url, notion_webhook_secret=None), run_worker=False)
        with TestClient(api) as c:
            assert c.post("/hooks/notion", json={"id": "x"}).status_code == 503
            assert c.post("/hooks/notion", json={"verification_token": "tok"}).json() == {"status": "verification_received"}
    set_runtime(None)


def test_admin_requires_token(client, db, test_db_url):
    assert client.get("/admin/goals").status_code == 401
    assert client.get("/admin/goals", headers={"Authorization": "Bearer nope"}).status_code == 401
    with queue_app.replace_connector(InMemoryConnector()):
        api = create_app(Runtime(db, MemoryNotifier()), settings(test_db_url, admin_token=None), run_worker=False)
        with TestClient(api) as c:
            assert c.get("/admin/goals").status_code == 503
    set_runtime(None)


def test_slip_flows_through_http_queue_worker_to_alerts(client):
    r = client.post("/admin/snapshot", headers=AUTH, json={"tasks": CHAIN, "goals": [GOAL], "today": "2026-12-01"})
    assert r.json() == {"status": "queued"}
    run_worker()
    assert client.notifier.sent == []                                   # healthy chain: quiet
    slipped = [t("A", "API spec", "Rahul", "2026-12-20", 3)]
    client.post("/admin/snapshot", headers=AUTH, json={"tasks": slipped, "today": "2026-12-01"})
    run_worker()
    assert sorted(a.owner for a in client.notifier.sent) == ["Dev", "Meera", "Priya", "Rahul"]
    goals = client.get("/admin/goals", headers=AUTH).json()
    assert goals[0]["status"] == "off_track"
    why = client.get("/admin/tasks/D/why", headers=AUTH).json()
    assert why["risk"] == "at_risk" and why["band"] and "Q1 launch" in why["why"]
    assert client.get("/admin/tasks/nope/why", headers=AUTH).status_code == 404


def test_followup_tick_endpoint(client, db):
    client.post("/admin/snapshot", headers=AUTH, json={"tasks": CHAIN, "goals": [GOAL], "today": "2026-12-01"})
    run_worker()
    r = client.post("/admin/followup-tick", headers=AUTH)
    assert r.status_code == 200 and set(r.json()) == {"candidates", "sent", "waiting"}


def test_outbox_endpoints(client):
    assert set(client.get("/admin/outbox", headers=AUTH).json()) == {"pending", "expired", "sent", "oldest_unsent"}
    r = client.post("/admin/sweep", headers=AUTH)
    assert r.status_code == 200 and {"sent", "failed", "cards"} <= set(r.json())
    assert client.get("/admin/outbox").status_code == 401
