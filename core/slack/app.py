"""Slack Bolt app over Socket Mode (outbound only: no public URL needed).

Every handler acks first (Slack's 3-second rule), then does the work. The decision logic lives in
core/slack/actions.py; this file only translates Slack payloads to those calls and back.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timezone
from typing import Any

from core.followups.hours import WorkingHours
from core.runtime import Runtime
from core.settings import Settings
from core.slack import actions as A
from core.slack import blocks as B

log = logging.getLogger("ops.slack")
HELP = ("*Ops Brain*\n• `/ops plan` — your top priorities\n• `/ops why <task>` — why a task ranks where it does\n"
        "Tap the buttons on alerts to mark work done, confirm it is on track, or propose a new date.")


def _today(cfg: Settings) -> date:
    return WorkingHours.from_settings(cfg).local_today(datetime.now(timezone.utc))


def _ephemeral(client: Any, body: dict, text: str) -> None:
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
    if channel:
        client.chat_postEphemeral(channel=channel, user=body["user"]["id"], text=text)
    else:
        client.chat_postMessage(channel=body["user"]["id"], text=text)


def sync_in_background(rt: Runtime, cfg: Settings) -> threading.Thread:
    """After a change we wrote to Notion, pull it back now so downstream alerts go out in seconds, not at the
    next 5-minute poll. Guarded by the same advisory lock as the poll, so the two can never double-alert."""
    def run() -> None:
        from core.notion import sync
        try:
            with rt.db.try_lock("notion_sync") as got:
                if got and rt.notion:
                    sync.sync_notion(rt.db, rt.notifier, rt.notion, cfg, _today(cfg))
        except Exception:  # noqa: BLE001
            log.exception("background sync failed; the 5-minute poll will retry")
    t = threading.Thread(target=run, daemon=True, name="ops-sync-now")
    t.start()
    return t


# ----- button handlers (top-level so tests can call them with fakes) -------------------------
def handle_task_button(rt: Runtime, cfg: Settings, kind: str, body: dict, client: Any) -> None:
    uid, task_id = body["user"]["id"], body["actions"][0]["value"]
    if kind == "slipping":
        pre = A.slip_precheck(rt, uid, task_id)
        if not pre.ok:
            return _ephemeral(client, body, pre.text)
        client.views_open(trigger_id=body["trigger_id"], view=B.slip_modal(task_id, pre.data["title"]))
        return
    res = A.mark_done(rt, uid, task_id) if kind == "done" else A.confirm_on_track(rt, uid, task_id)
    if not res.ok:
        return _ephemeral(client, body, res.text)
    msg = body["message"]
    client.chat_update(channel=body["channel"]["id"], ts=msg["ts"], text=msg.get("text", ""),
                       blocks=B.replace_task_actions(msg["blocks"], task_id, res.text))
    if kind == "done":
        sync_in_background(rt, cfg)


def handle_slip_submit(rt: Runtime, cfg: Settings, body: dict, client: Any) -> None:
    uid = body["user"]["id"]
    values = body["view"]["state"]["values"]
    new_due = date.fromisoformat(values["due"]["new_due"]["selected_date"])
    reason = (values["reason"]["text"].get("value") or "").strip() or None
    res = A.request_date_change(rt, uid, body["view"]["private_metadata"], new_due, reason, _today(cfg))
    if not res.ok:
        client.chat_postMessage(channel=uid, text=res.text)
        return
    client.chat_postMessage(channel=uid, text=f"✅ {res.text} A lead will review it.")
    d = res.data
    card = B.date_change_card(d["meeting_id"], d["local_id"], d["payload"], d["preview"])
    people = rt.db.load_people()
    leads = [p.slack_user_id for p in people if p.is_lead and p.slack_user_id]
    for target in (leads or [cfg.slack_alerts_channel]):
        if target:
            client.chat_postMessage(channel=target, text=f"Date change requested for “{d['payload']['title']}”", blocks=card)


def handle_date_change_button(rt: Runtime, cfg: Settings, approve: bool, body: dict, client: Any) -> None:
    uid = body["user"]["id"]
    meeting_id, local_id = body["actions"][0]["value"].split("|", 1)
    res = A.decide_date_change(rt, uid, meeting_id, local_id, approve)
    if not res.ok:
        return _ephemeral(client, body, res.text)
    row = rt.db.get_proposal(meeting_id, local_id)
    msg = body["message"]
    client.chat_update(channel=body["channel"]["id"], ts=msg["ts"], text=msg.get("text", ""),
                       blocks=B.date_change_card(meeting_id, local_id, row["payload"], [], res.data["status"],
                                                 res.data["decided_by"]))
    if approve:
        sync_in_background(rt, cfg)  # downstream owners hear about it now, not in 5 minutes


def handle_proposal_button(rt: Runtime, cfg: Settings, approve: bool, scope: str, body: dict, client: Any) -> None:
    uid, value = body["user"]["id"], body["actions"][0]["value"]
    if scope == "all":
        meeting_id, local_ids = value, None
    else:
        meeting_id, local = value.split("|", 1)
        local_ids = [local]
    res = A.decide_meeting_proposals(rt, cfg, uid, meeting_id, local_ids, approve)
    if not res.ok:
        return _ephemeral(client, body, res.text)
    msg = body["message"]
    m = re.search(r"“(.+?)”", msg["blocks"][0]["text"]["text"])
    goals = {g.id: g.name for g in rt.db.load_graph_inputs()[1]}
    client.chat_update(channel=body["channel"]["id"], ts=msg["ts"], text=msg.get("text", ""),
                       blocks=B.proposals_card(meeting_id, m.group(1) if m else "meeting",
                                               rt.db.get_proposals(meeting_id), goals))
    _ephemeral(client, body, res.text)
    if approve:
        sync_in_background(rt, cfg)


def handle_command(rt: Runtime, cfg: Settings, command: dict) -> str:
    text = (command.get("text") or "").strip()
    uid = command["user_id"]
    if text in ("", "help"):
        return HELP
    if text == "plan":
        return A.my_plan(rt, uid, _today(cfg))
    if text.startswith("why "):
        return A.why_task(rt, text[4:].strip(), _today(cfg))
    return "I don't know that command.\n" + HELP


# ----- Bolt wiring -------------------------------------------------------------------------------
def build_bolt_app(rt: Runtime, cfg: Settings, client: Any | None = None):
    from slack_bolt import App
    app = App(token=cfg.slack_oauth_token, client=client, token_verification_enabled=client is None)

    def safe(fn):
        def wrapper(ack, body, client, respond=None, **_):
            ack()
            try:
                fn(body, client)
            except Exception:  # noqa: BLE001
                log.exception("slack handler failed")
                try:
                    _ephemeral(client, body, "Something went wrong on my side. Nothing was changed; please try again.")
                except Exception:  # noqa: BLE001
                    pass
        return wrapper

    for kind in ("done", "on_track", "slipping"):
        app.action(re.compile(rf"^ops_{kind}:"))(safe(lambda b, c, k=kind: handle_task_button(rt, cfg, k, b, c)))
    app.action(re.compile(r"^ops_dc_approve:"))(safe(lambda b, c: handle_date_change_button(rt, cfg, True, b, c)))
    app.action(re.compile(r"^ops_dc_reject:"))(safe(lambda b, c: handle_date_change_button(rt, cfg, False, b, c)))
    app.action(re.compile(r"^ops_prop_approve:"))(safe(lambda b, c: handle_proposal_button(rt, cfg, True, "one", b, c)))
    app.action(re.compile(r"^ops_prop_reject:"))(safe(lambda b, c: handle_proposal_button(rt, cfg, False, "one", b, c)))
    app.action(re.compile(r"^ops_prop_all_approve:"))(safe(lambda b, c: handle_proposal_button(rt, cfg, True, "all", b, c)))
    app.action(re.compile(r"^ops_prop_all_reject:"))(safe(lambda b, c: handle_proposal_button(rt, cfg, False, "all", b, c)))

    @app.view("ops_slip_submit")
    def _slip(ack, body, client):
        due = body["view"]["state"]["values"]["due"]["new_due"]["selected_date"]
        if date.fromisoformat(due) < _today(cfg):
            return ack(response_action="errors", errors={"due": "Pick a date that is not in the past."})
        ack()
        try:
            handle_slip_submit(rt, cfg, body, client)
        except Exception:  # noqa: BLE001
            log.exception("slip submit failed")
            client.chat_postMessage(channel=body["user"]["id"], text="Something went wrong sending that request. Please try again.")

    @app.command("/ops")
    def _ops(ack, command, respond):
        ack()
        try:
            respond(handle_command(rt, cfg, command))
        except Exception:  # noqa: BLE001
            log.exception("/ops failed")
            respond("Something went wrong on my side. Please try again.")

    return app


def start_socket_mode(rt: Runtime, cfg: Settings):
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    handler = SocketModeHandler(build_bolt_app(rt, cfg), cfg.slack_app_token)
    handler.connect()   # background thread; the FastAPI process keeps running
    return handler
