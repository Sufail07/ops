import json
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from core.alerts.format import Alert, AlertItem
from core.models import Goal, Person, Status, Task
from core.runtime import Runtime
from core.settings import Settings
from core.slack import actions as A
from core.slack import app as SA
from core.slack import blocks as B
from core.slack.linking import link_slack_users
from core.slack.notifier import SlackNotifier

TODAY = date(2026, 12, 1)
CFG = Settings(database_url="postgresql://x", notion_tasks_ds="T", slack_alerts_channel="C-ALERTS")
GOAL = Goal("g1", "Q1 launch", 5, date(2027, 1, 10))
PEOPLE = [Person("p-rahul", "Rahul", slack_user_id="U-RAHUL"), Person("p-meera", "Meera", slack_user_id="U-MEERA"),
          Person("p-asha", "Asha", is_lead=True, slack_user_id="U-ASHA")]


class FakeNotion:
    def __init__(self, fail_create=False):
        self.updates, self.creates, self.fail_create = [], [], fail_create

    def update_page(self, pid, props):
        self.updates.append((pid, props))
        return {"last_edited_time": "2026-12-01T00:00:00Z"}

    def create_page(self, ds, props):
        if self.fail_create:
            raise RuntimeError("notion down")
        self.creates.append((ds, props))
        return {"id": f"new-{len(self.creates)}"}


def slack():
    c = MagicMock()
    c.chat_postMessage.return_value = {"ok": True, "channel": "C"}
    return c


def chain():
    return [Task("A", "API spec", "Rahul", date(2026, 12, 8), 3, Status.DOING, "g1", (), TODAY),
            Task("B", "Backend build", "Meera", date(2026, 12, 17), 5, Status.DOING, "g1", ("A",), TODAY),
            Task("U", "Unowned thing", None, date(2026, 12, 20), 1, Status.TODO, "g1", (), TODAY)]


@pytest.fixture()
def rt(db):
    db.upsert_people(PEOPLE)
    db.save_graph([GOAL], chain())
    return Runtime(db, MagicMock(), FakeNotion())


# ---------------------------------------------------------------- blocks (pure)
def slip_alert(n=1):
    items = tuple(AlertItem(f"t{i}", f"“Task {i}” is late", ("Slipping",), f"Task {i}") for i in range(n))
    return Alert("Rahul", "at_risk", "Heads up, Rahul: a delay\nbody", tuple(i.task_id for i in items), "slip", items)


def test_alert_blocks_have_unique_action_ids_and_respect_limits():
    bl = B.alert_blocks(slip_alert(30))
    assert len(bl) <= 50
    ids = [e["action_id"] for b in bl if b["type"] == "actions" for e in b["elements"]]
    assert len(ids) == len(set(ids)) == B.MAX_ITEMS and all(len(i) < 255 for i in ids)
    assert any("more" in str(b) for b in bl)


def test_followup_buttons_and_escaping():
    a = Alert("Rahul", "info", "Follow-ups\n x", ("t",), "followup",
              (AlertItem("t", "⏰ <b>&co</b> due", ("Done", "On track", "Slipping"), "x"),))
    bl = B.alert_blocks(a)
    assert "&lt;b&gt;&amp;co" in bl[1]["text"]["text"]
    labels = [e["text"]["text"] for e in bl[2]["elements"]]
    assert labels == ["Done", "On track", "Slipping"] and bl[2]["elements"][0]["style"] == "primary"


def test_replace_task_actions_swaps_only_that_task():
    bl = B.alert_blocks(slip_alert(2))
    out = B.replace_task_actions(bl, "t0", "done!")
    assert [b["block_id"] for b in out if b["type"] == "actions"] == ["act_t1"]
    assert any(b["type"] == "context" and "done!" in b["elements"][0]["text"] for b in out)


def test_proposals_card_shows_flags_and_hides_buttons_once_decided():
    rows = [{"local_id": "t1", "status": "Proposed", "decided_by": None,
             "payload": {"title": "Write spec", "owner": None, "due": None, "needs_attention": ["owner?"],
                         "evidence_quote": "x", "goal_id": "g1"}},
            {"local_id": "t2", "status": "Approved", "decided_by": "Asha", "payload": {"title": "Done one", "due": "2026-12-05"}}]
    text = json.dumps(B.proposals_card("m1", "Weekly", rows, {"g1": "Q1 launch"}))
    assert "owner?" in text and "Q1 launch" in text and "ops_prop_approve:m1|t1" in text
    assert "ops_prop_approve:m1|t2" not in text and "Created in Notion" in text


# ---------------------------------------------------------------- notifier
def notifier(db, client=None):
    db.upsert_people(PEOPLE)
    return SlackNotifier(db, "xoxb", "C-ALERTS", client or slack(), people_ttl=0)


def test_dm_to_linked_owner_else_channel(db):
    c = slack()
    n = notifier(db, c)
    n.send(slip_alert())                                   # Rahul is linked -> DM
    assert c.chat_postMessage.call_args.kwargs["channel"] == "U-RAHUL"
    c.reset_mock()
    n.send(Alert("Stranger", "info", "h\nb", (), "system"))   # unknown owner -> channel with a note
    assert c.chat_postMessage.call_args.kwargs["channel"] == "C-ALERTS"
    assert "no Slack account linked" in json.dumps(c.chat_postMessage.call_args.kwargs["blocks"])
    c.reset_mock()
    n.send(Alert(None, "off_track", "cycle\nb", (), "system"))   # no owner -> channel
    assert c.chat_postMessage.call_args.kwargs["channel"] == "C-ALERTS"


def test_dm_failure_falls_back_to_channel_and_total_failure_raises(db):
    c = slack()
    err = SlackApiError("x", {"ok": False, "error": "cannot_dm_bot"})
    c.chat_postMessage.side_effect = [err, {"ok": True}]
    notifier(db, c).send(slip_alert())
    assert [k.kwargs["channel"] for k in c.chat_postMessage.call_args_list] == ["U-RAHUL", "C-ALERTS"]
    n = SlackNotifier(db, "xoxb", None, slack(), people_ttl=0)
    with pytest.raises(RuntimeError):
        n.send(Alert("Nobody", "info", "h\nb", (), "system"))


# ---------------------------------------------------------------- linking
def test_link_by_email_then_name_then_create_and_is_idempotent(db):
    db.ensure_people([{"id": "n1", "name": "Priya Shah", "email": "priya@x.com"}, {"id": "n2", "name": "Dev Patel", "email": None}])
    c = MagicMock()
    c.users_list.return_value = {"members": [
        {"id": "S1", "profile": {"email": "PRIYA@x.com"}, "real_name": "P. Shah"},
        {"id": "S2", "profile": {}, "real_name": "dev patel"},
        {"id": "S3", "profile": {"email": "new@x.com"}, "real_name": "New Person"},
        {"id": "B1", "is_bot": True}, {"id": "USLACKBOT"}], "response_metadata": {"next_cursor": ""}}
    assert link_slack_users(db, c) == {"linked": 2, "created": 1}
    by = {p.name: p.slack_user_id for p in db.load_people()}
    assert by["Priya Shah"] == "S1" and by["Dev Patel"] == "S2" and by["New Person"] == "S3"
    assert link_slack_users(db, c) == {"linked": 0, "created": 0}


# ---------------------------------------------------------------- actions: permissions
def test_mark_done_owner_only(rt):
    res = A.mark_done(rt, "U-MEERA", "A")                       # Meera does not own A
    assert not res.ok and "belongs to Rahul" in res.text and rt.notion.updates == []
    assert not A.mark_done(rt, "U-NOBODY", "A").ok               # unlinked
    ok = A.mark_done(rt, "U-RAHUL", "A")
    assert ok.ok and rt.notion.updates[0] == ("A", {"Status": {"status": {"name": "Done"}}})
    assert A.mark_done(rt, "U-ASHA", "B").ok                     # a lead may
    assert A.mark_done(rt, "U-MEERA", "U").ok                    # unowned task: any linked person


def test_on_track_records_outcome_without_touching_notion(rt):
    assert A.confirm_on_track(rt, "U-RAHUL", "A").ok
    assert rt.notion.updates == []
    assert rt.db.event_exists("task.confirmed_on_track", "A")


# ---------------------------------------------------------------- actions: date change is a REQUEST first
def test_date_change_request_then_lead_approval(rt):
    res = A.request_date_change(rt, "U-RAHUL", "A", date(2026, 12, 20), "vendor delay", TODAY)
    assert res.ok and rt.notion.updates == []                    # Notion untouched by a request
    assert any("Backend build" in line for line in res.data["preview"])   # preview names who is put at risk
    mid, lid = res.data["meeting_id"], res.data["local_id"]
    denied = A.decide_date_change(rt, "U-MEERA", mid, lid, True)   # a non-lead cannot approve
    assert not denied.ok and rt.notion.updates == []
    ok = A.decide_date_change(rt, "U-ASHA", mid, lid, True)
    assert ok.ok and rt.notion.updates == [("A", {"Due date": {"date": {"start": "2026-12-20"}}})]
    again = A.decide_date_change(rt, "U-ASHA", mid, lid, True)     # double click: written exactly once
    assert not again.ok and len(rt.notion.updates) == 1


def test_date_change_validation_and_reject(rt):
    assert not A.request_date_change(rt, "U-RAHUL", "A", date(2026, 11, 1), None, TODAY).ok     # past
    assert not A.request_date_change(rt, "U-RAHUL", "A", date(2026, 12, 8), None, TODAY).ok     # same date
    assert not A.request_date_change(rt, "U-MEERA", "A", date(2026, 12, 20), None, TODAY).ok    # not the owner
    r = A.request_date_change(rt, "U-RAHUL", "A", date(2026, 12, 20), None, TODAY)
    A.decide_date_change(rt, "U-ASHA", r.data["meeting_id"], r.data["local_id"], False)
    assert rt.notion.updates == []


def test_bootstrap_any_linked_person_approves_when_no_lead(db):
    db.upsert_people([Person("p1", "Rahul", slack_user_id="U1")])
    db.save_graph([GOAL], chain())
    rt = Runtime(db, MagicMock(), FakeNotion())
    r = A.request_date_change(rt, "U1", "A", date(2026, 12, 20), None, TODAY)
    assert A.decide_date_change(rt, "U1", r.data["meeting_id"], r.data["local_id"], True).ok


def test_failed_notion_write_reopens_the_request(rt):
    r = A.request_date_change(rt, "U-RAHUL", "A", date(2026, 12, 20), None, TODAY)
    rt.notion.update_page = MagicMock(side_effect=RuntimeError("notion down"))
    with pytest.raises(RuntimeError):
        A.decide_date_change(rt, "U-ASHA", r.data["meeting_id"], r.data["local_id"], True)
    assert rt.db.get_proposal(r.data["meeting_id"], r.data["local_id"])["status"] == "Proposed"


# ---------------------------------------------------------------- actions: meeting proposals
def props():
    return [{"local_id": "t1", "title": "Write spec", "owner": "Rahul", "owner_spoken": "Rahul", "due": "2026-12-05",
             "goal_id": "g1", "depends_on": [], "evidence_quote": "I'll do the spec", "needs_attention": []},
            {"local_id": "t2", "title": "Build it", "owner": None, "owner_spoken": None, "due": None, "goal_id": None,
             "depends_on": ["t1"], "evidence_quote": "then build", "needs_attention": ["owner?"]}]


def test_approving_proposals_creates_tasks_links_dependencies_and_decides_once(rt):
    rt.db.save_proposals("m1", props())
    res = A.decide_meeting_proposals(rt, CFG, "U-ASHA", "m1", None, True)
    assert res.ok and len(rt.notion.creates) == 2
    first = rt.notion.creates[0][1]
    assert first["Task name"]["title"][0]["text"]["content"] == "Write spec"
    assert first["Due date"]["date"]["start"] == "2026-12-05" and first["Goal"]["relation"] == [{"id": "g1"}]
    assert "Assignee" not in first                               # 'p-rahul' is not a Notion user id: not assigned
    assert first["Owner (name)"]["rich_text"][0]["text"]["content"] == "Rahul"   # ...so the name is kept as the owner
    assert ("new-2", {"Depends on": {"relation": [{"id": "new-1"}]}}) in rt.notion.updates
    assert not A.decide_meeting_proposals(rt, CFG, "U-ASHA", "m1", None, True).ok      # nothing left: no duplicates
    assert len(rt.notion.creates) == 2


def test_reject_creates_nothing_and_failure_reopens(rt):
    rt.db.save_proposals("m1", props())
    assert A.decide_meeting_proposals(rt, CFG, "U-RAHUL", "m1", ["t2"], False).ok and rt.notion.creates == []
    rt.notion.fail_create = True
    with pytest.raises(RuntimeError):
        A.decide_meeting_proposals(rt, CFG, "U-RAHUL", "m1", ["t1"], True)
    assert rt.db.get_proposal("m1", "t1")["status"] == "Proposed"             # not half-approved


# ---------------------------------------------------------------- handlers
def body(uid, task_id, action="ops_done"):
    return {"user": {"id": uid}, "actions": [{"value": task_id, "action_id": f"{action}:{task_id}"}],
            "channel": {"id": "C1"}, "trigger_id": "trig",
            "message": {"ts": "1.0", "text": "t", "blocks": B.alert_blocks(
                Alert("Rahul", "info", "h\nb", ("A",), "followup", (AlertItem("A", "x", ("Done", "On track", "Slipping")),)))}}


def test_done_button_updates_message_and_unauthorised_gets_private_reply(rt, monkeypatch):
    monkeypatch.setattr(SA, "sync_in_background", lambda *a, **k: None)
    c = MagicMock()
    SA.handle_task_button(rt, CFG, "done", body("U-RAHUL", "A"), c)
    blocks = c.chat_update.call_args.kwargs["blocks"]
    assert not any(b.get("block_id") == "act_A" for b in blocks) and "marked done" in json.dumps(blocks)
    c2 = MagicMock()
    SA.handle_task_button(rt, CFG, "done", body("U-MEERA", "A"), c2)
    c2.chat_update.assert_not_called()
    assert "belongs to Rahul" in c2.chat_postEphemeral.call_args.kwargs["text"]


def test_slipping_opens_modal_and_submission_notifies_the_lead(rt):
    c = MagicMock()
    SA.handle_task_button(rt, CFG, "slipping", body("U-RAHUL", "A", "ops_slipping"), c)
    assert c.views_open.call_args.kwargs["view"]["callback_id"] == "ops_slip_submit"
    sub = {"user": {"id": "U-RAHUL"}, "view": {"private_metadata": "A", "state": {"values": {
        "due": {"new_due": {"selected_date": "2026-12-20"}}, "reason": {"text": {"value": "vendor"}}}}}}
    c3 = MagicMock()
    SA.handle_slip_submit(rt, CFG, sub, c3)
    targets = [k.kwargs["channel"] for k in c3.chat_postMessage.call_args_list]
    assert targets == ["U-RAHUL", "U-ASHA"] and rt.notion.updates == []     # lead asked; Notion still untouched


def test_commands(rt):
    plan = SA.handle_command(rt, CFG, {"user_id": "U-RAHUL", "text": "plan"})
    assert "Top priorities for Rahul" in plan and "API spec" in plan
    assert "Backend build" in SA.handle_command(rt, CFG, {"user_id": "U-RAHUL", "text": "why backend"})
    assert "No task matches" in SA.handle_command(rt, CFG, {"user_id": "U-RAHUL", "text": "why zzz"})
    assert "/ops plan" in SA.handle_command(rt, CFG, {"user_id": "U-RAHUL", "text": ""})


def test_bolt_routes_a_real_button_payload_to_the_handler(rt, monkeypatch):
    """Goes through Bolt itself: regex routing on action_id, ack(), then our handler. Only the network call
    (WebClient.api_call) is stubbed, so nothing reaches Slack."""
    from slack_bolt.request import BoltRequest
    from slack_sdk import WebClient
    monkeypatch.setattr(SA, "sync_in_background", lambda *a, **k: None)
    calls = []

    from slack_sdk.web import SlackResponse

    def fake_api_call(self, api_method, **kwargs):
        calls.append((api_method, kwargs))
        data = ({"ok": True, "user_id": "UBOT", "bot_id": "B1", "team_id": "T1", "url": "https://x.slack.com/"}
                if api_method == "auth.test" else {"ok": True, "channel": "C1"})
        return SlackResponse(client=self, http_verb="POST", api_url="", req_args={}, data=data, headers={}, status_code=200)
    monkeypatch.setattr(WebClient, "api_call", fake_api_call)

    app = SA.build_bolt_app(rt, Settings(database_url="postgresql://x", slack_oauth_token="xoxb-test"))
    payload = {"type": "block_actions", "user": {"id": "U-RAHUL", "team_id": "T1"}, "team": {"id": "T1"},
               "api_app_id": "A1", "trigger_id": "trig", "container": {"channel_id": "C1"}, "channel": {"id": "C1"},
               "message": body("U-RAHUL", "A")["message"],
               "actions": [{"type": "button", "action_id": "ops_done:A", "block_id": "act_A", "value": "A"}]}
    resp = app.dispatch(BoltRequest(body=payload, mode="socket_mode"))
    assert resp.status == 200                                       # acked within Slack's 3 seconds
    for _ in range(100):                                            # the listener runs in a worker thread
        if any(m == "chat.update" for m, _ in calls):
            break
        time.sleep(0.1)
    assert any(m == "chat.update" for m, _ in calls), [m for m, _ in calls]
    assert rt.notion.updates[0] == ("A", {"Status": {"status": {"name": "Done"}}})
