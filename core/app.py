"""FastAPI service: health, Notion webhook, read-only admin. The job worker runs inside the same process
(one container). Webhooks only verify, de-duplicate and enqueue: heavy work happens in jobs."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import hmac
import json
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timezone
from typing import Any

from anyio import to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core import jobs, services
from core.followups.hours import WorkingHours
from core.graph.engine import TaskGraph
from core.priority.engine import prioritise
from core.queue import app as queue_app
from core.runtime import Runtime, get_runtime, set_runtime
from core.settings import Settings, get_settings


class SnapshotIn(BaseModel):
    tasks: list[dict[str, Any]]
    goals: list[dict[str, Any]] = Field(default_factory=list)
    today: date | None = None


def verify_notion_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Notion signs the raw body: X-Notion-Signature = 'sha256=' + HMAC-SHA256(body, verification_token)."""
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def create_app(rt: Runtime | None = None, settings: Settings | None = None, run_worker: bool | None = None) -> FastAPI:
    cfg = settings or get_settings()
    if rt is not None:
        set_runtime(rt)
    start_worker = cfg.run_worker if run_worker is None else run_worker

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        slack_handler = None
        rt_ = get_runtime()
        if cfg.run_slack and rt_.slack is not None and cfg.slack_app_token:
            from core.slack.app import start_socket_mode
            from core.slack.linking import link_slack_users
            try:
                await to_thread.run_sync(link_slack_users, rt_.db, rt_.slack.client)
                slack_handler = await to_thread.run_sync(start_socket_mode, rt_, cfg)
            except Exception:  # noqa: BLE001  (Slack down must not stop the API or the jobs)
                logging.getLogger("ops.slack").exception("Slack Socket Mode failed to start; alerts will not be delivered")
        async with queue_app.open_async():
            worker = asyncio.create_task(queue_app.run_worker_async(wait=True, install_signal_handlers=False)) \
                if start_worker else None
            try:
                yield
            finally:
                if worker:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker
                if slack_handler:
                    await to_thread.run_sync(slack_handler.close)

    api = FastAPI(title="Ops Brain", version="0.1.0", lifespan=lifespan)

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        if not cfg.admin_token:
            raise HTTPException(503, "admin API disabled: ADMIN_TOKEN is not set")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, cfg.admin_token):
            raise HTTPException(401, "invalid token")

    # ----- health --------------------------------------------------------------------
    @api.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @api.get("/readyz")
    async def readyz() -> dict:
        def probe() -> None:
            with get_runtime().db.pool.connection() as c:
                c.execute("SELECT 1")
                c.execute("SELECT 1 FROM procrastinate_jobs LIMIT 1")
        try:
            await to_thread.run_sync(probe)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"not ready: {type(e).__name__}") from e
        return {"status": "ready"}

    # ----- Notion webhook ------------------------------------------------------------
    @api.post("/hooks/notion")
    async def notion_hook(request: Request, x_notion_signature: str | None = Header(default=None)) -> dict:
        raw = await request.body()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "body is not JSON")
        if "verification_token" in payload:  # one-time handshake: paste the token into Notion's UI
            await to_thread.run_sync(get_runtime().db.append_event, "notion.verification_received", None,
                                     {"note": "token received; set NOTION_WEBHOOK_SECRET to it"}, "webhook")
            return {"status": "verification_received"}
        if not cfg.notion_webhook_secret:
            raise HTTPException(503, "webhook disabled: NOTION_WEBHOOK_SECRET is not set")
        if not verify_notion_signature(cfg.notion_webhook_secret, raw, x_notion_signature):
            raise HTTPException(401, "bad signature")
        event_id = str(payload.get("id") or "")
        if not event_id:
            raise HTTPException(400, "event has no id")
        db = get_runtime().db
        if not await to_thread.run_sync(db.record_inbox, event_id, "notion"):
            return {"status": "duplicate"}
        await jobs.process_inbox_event.defer_async(event_id=event_id, source="notion", payload=payload)
        return {"status": "queued"}

    # ----- admin (read-only, plus a manual snapshot input for testing without Notion) --
    @api.get("/admin/goals", dependencies=[Depends(require_admin)])
    async def goals() -> list[dict]:
        def work() -> list[dict]:
            tasks, gs = get_runtime().db.load_graph_inputs()
            g = TaskGraph(tasks, gs)
            a = g.analyze(WorkingHours.from_settings(cfg).local_today(datetime.now(timezone.utc)))
            return [{"goal_id": x.id, "name": x.name, **vars(g.goal_health(x.id, a))} for x in gs]
        return await to_thread.run_sync(work)

    @api.get("/admin/tasks/{task_id}/why", dependencies=[Depends(require_admin)])
    async def why(task_id: str) -> dict:
        def work() -> dict:
            rt = get_runtime()
            tasks, gs = rt.db.load_graph_inputs()
            today = WorkingHours.from_settings(cfg).local_today(datetime.now(timezone.utc))
            g = TaskGraph(tasks, gs)
            if task_id not in g.tasks:
                raise HTTPException(404, "unknown task")
            a = g.analyze(today)
            item = next((i for i in prioritise(g, a, today, previous=rt.db.load_previous_snapshot(today))
                         if i.task_id == task_id), None)
            st = a[task_id]
            return {"task_id": task_id, "why": item.why if item else "done", "band": item.band if item else None,
                    "rank": item.rank if item else None, "risk": st.risk.value, "slack_days": st.slack_days,
                    "projected_finish": st.projected_finish, "reasons": list(st.reasons)}
        return await to_thread.run_sync(work)

    @api.post("/admin/snapshot", dependencies=[Depends(require_admin)])
    async def snapshot(body: SnapshotIn) -> dict:
        today = body.today or WorkingHours.from_settings(cfg).local_today(datetime.now(timezone.utc))
        await jobs.apply_snapshot.defer_async(tasks=body.tasks, goals=body.goals, today=today.isoformat())
        return {"status": "queued"}

    @api.post("/admin/sync", dependencies=[Depends(require_admin)])
    async def sync_now(writeback: bool = True) -> dict:
        from core.notion import sync
        rt = get_runtime()
        if rt.notion is None:
            raise HTTPException(503, "Notion is not configured (NOTION_PAT)")
        today = WorkingHours.from_settings(cfg).local_today(datetime.now(timezone.utc))
        res = await to_thread.run_sync(lambda: sync.sync_notion(rt.db, rt.notifier, rt.notion, cfg, today,
                                                                 writeback=writeback))
        return vars(res)

    @api.post("/admin/slack/test", dependencies=[Depends(require_admin)])
    async def slack_test() -> dict:
        rt = get_runtime()
        if rt.slack is None or not cfg.slack_alerts_channel:
            raise HTTPException(503, "Slack is not configured")
        from core.slack import blocks as SB
        res = await to_thread.run_sync(rt.slack.post, cfg.slack_alerts_channel, "Ops Brain is connected",
                                       [SB.section("✅ *Ops Brain is connected.* Alerts and follow-ups will appear here.")])
        return {"ok": res["ok"], "channel": res["channel"]}

    @api.post("/admin/slack/link", dependencies=[Depends(require_admin)])
    async def slack_link() -> dict:
        from core.slack.linking import link_slack_users
        rt = get_runtime()
        if rt.slack is None:
            raise HTTPException(503, "Slack is not configured")
        return await to_thread.run_sync(link_slack_users, rt.db, rt.slack.client)

    @api.post("/admin/poll", dependencies=[Depends(require_admin)])
    async def poll_now() -> dict:
        from core.poll import run_poll
        rt = get_runtime()
        if rt.notion is None:
            raise HTTPException(503, "Notion is not configured (NOTION_PAT)")
        return await to_thread.run_sync(run_poll, rt, cfg)

    @api.get("/admin/outbox", dependencies=[Depends(require_admin)])
    async def outbox() -> dict:
        return await to_thread.run_sync(get_runtime().db.outbox_stats)

    @api.post("/admin/sweep", dependencies=[Depends(require_admin)])
    async def sweep_now() -> dict:
        rt = get_runtime()
        return await to_thread.run_sync(services.sweep, rt.db, rt.notifier, 0)

    @api.post("/admin/followup-tick", dependencies=[Depends(require_admin)])
    async def tick() -> dict:
        rt = get_runtime()
        return await to_thread.run_sync(services.followup_tick, rt.db, rt.notifier, datetime.now(timezone.utc),
                                        WorkingHours.from_settings(cfg))

    return api
