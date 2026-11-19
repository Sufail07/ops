"""The alert outbox: a slip and its alert are saved together, so a crash can no longer lose the alert."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from core import services
from core.alerts.format import Alert, AlertItem
from core.gateway import Gateway, Routing
from core.models import Goal, Task
from core.notifier import MemoryNotifier
from core.notion.sync import extract_new_meetings, sync_notion
from core.services import apply_snapshot, deliver_outbox, sweep

from test_notion_sync import CFG, TODAY as SYNC_TODAY, FakeNotion, chain as notion_chain

TODAY = date(2026, 12, 1)
GOAL = Goal("g1", "Q1 launch", 5, date(2027, 1, 10))


def chain(a_due=date(2026, 12, 8)):
    return [Task("A", "API spec", "Rahul", a_due, 3, goal_id="g1", last_update=TODAY),
            Task("B", "Backend build", "Meera", date(2026, 12, 17), 5, goal_id="g1", depends_on=("A",), last_update=TODAY),
            Task("C", "QA pass", "Priya", date(2026, 12, 24), 4, goal_id="g1", depends_on=("B",), last_update=TODAY),
            Task("D", "Launch page", "Dev", date(2026, 12, 29), 2, goal_id="g1", depends_on=("C",), last_update=TODAY)]


SLIPPED_A = Task("A", "API spec", "Rahul", date(2026, 12, 30), 3, goal_id="g1", last_update=TODAY)


class DownNotifier:
    """Slack is down: every send fails."""
    def send(self, alert):
        raise ConnectionError("slack is down")

    def send_proposals(self, *a, **k):
        raise ConnectionError("slack is down")


def counts(db):
    return db._exec("SELECT count(*) FILTER (WHERE sent_at IS NULL) AS unsent, count(*) AS total FROM alert_outbox")[0]


def test_a_crash_right_after_the_save_cannot_lose_the_alert(db, monkeypatch):
    n = MemoryNotifier()
    apply_snapshot(db, n, chain(), [GOAL], TODAY)                      # healthy: nothing to say

    def crash(*a, **k):
        raise RuntimeError("process died after saving, before sending")
    monkeypatch.setattr(services, "deliver_outbox", crash)
    with pytest.raises(RuntimeError):
        apply_snapshot(db, n, [SLIPPED_A], [], TODAY)
    monkeypatch.undo()

    # The slip is saved (the cache has the new date) and its alerts are saved with it, still unsent.
    assert next(t for t in db.load_graph_inputs()[0] if t.id == "A").due == date(2026, 12, 30)
    assert counts(db)["unsent"] == 4 and n.sent == []
    assert db._exec("SELECT count(*) n FROM risk_events WHERE notified_at IS NULL")[0]["n"] == 4

    # This is why the old design lost them: retrying the same change now sees "nothing changed".
    assert apply_snapshot(db, MemoryNotifier(), [SLIPPED_A], [], TODAY) == []

    # The sweeper finds them and sends all four.
    out = sweep(db, n, grace_seconds=0)
    assert out["sent"] == 4
    assert sorted(a.owner for a in n.sent) == ["Dev", "Meera", "Priya", "Rahul"]
    assert counts(db)["unsent"] == 0
    assert db._exec("SELECT count(*) n FROM risk_events WHERE notified_at IS NULL")[0]["n"] == 0


def test_a_send_failure_is_kept_and_retried_not_raised(db):
    apply_snapshot(db, MemoryNotifier(), chain(), [GOAL], TODAY)
    alerts = apply_snapshot(db, DownNotifier(), [SLIPPED_A], [], TODAY)      # does not raise
    assert len(alerts) == 4 and counts(db)["unsent"] == 4
    row = db._exec("SELECT attempts, last_error FROM alert_outbox ORDER BY id LIMIT 1")[0]
    assert row["attempts"] == 1 and "slack is down" in row["last_error"]
    # Slack is back. The claim from the failed attempt must expire first, then the sweeper delivers.
    db._exec("UPDATE alert_outbox SET claimed_at = now() - interval '5 minutes'")
    n = MemoryNotifier()
    assert sweep(db, n, grace_seconds=0)["sent"] == 4 and len(n.sent) == 4


def test_each_alert_is_sent_exactly_once(db):
    n = MemoryNotifier()
    apply_snapshot(db, n, chain(), [GOAL], TODAY)
    apply_snapshot(db, n, [SLIPPED_A], [], TODAY)
    assert len(n.sent) == 4
    assert deliver_outbox(db, n)["claimed"] == 0 and sweep(db, n, 0)["sent"] == 0
    assert len(n.sent) == 4


def test_two_senders_cannot_claim_the_same_alert(db):
    ids = db.enqueue_alerts([Alert("Rahul", "info", "h\nb", ("A",), "system")])
    first = db.claim_outbox(ids)
    assert len(first) == 1
    assert db.claim_outbox(ids) == []                                        # already claimed
    assert len(db.claim_outbox(ids, claim_ttl_seconds=0)) == 1               # a dead sender's claim expires


def test_sweeper_leaves_fresh_alerts_to_the_live_delivery(db):
    db.enqueue_alerts([Alert("Rahul", "info", "h\nb", ("A",), "system")])
    n = MemoryNotifier()
    assert sweep(db, n, grace_seconds=60)["sent"] == 0 and n.sent == []      # just saved: still being delivered live
    assert sweep(db, n, grace_seconds=0)["sent"] == 1


def test_a_failed_alert_backs_off_and_keeps_retrying_instead_of_giving_up(db):
    db.enqueue_alerts([Alert("Rahul", "info", "h\nb", ("A",), "system")])
    for expected_attempts in range(1, 13):                                   # far more than the old limit of 10
        db._exec("UPDATE alert_outbox SET claimed_at = now() - interval '1 hour'")   # the back-off has passed
        rows = db.claim_outbox()
        assert len(rows) == 1 and rows[0]["attempts"] == expected_attempts
    assert db.outbox_stats()["pending"] == 1                                 # still waiting, not abandoned


def test_back_off_grows_and_is_capped(db):
    db.enqueue_alerts([Alert("Rahul", "info", "h\nb", ("A",), "system")])
    db._exec("UPDATE alert_outbox SET attempts = 3, claimed_at = now() - interval '200 seconds'")
    assert db.claim_outbox() == []                                           # 3 failures: wait 240 s, only 200 passed
    db._exec("UPDATE alert_outbox SET claimed_at = now() - interval '250 seconds'")
    assert len(db.claim_outbox()) == 1
    db._exec("UPDATE alert_outbox SET attempts = 30, claimed_at = now() - interval '14 minutes'")
    assert db.claim_outbox() == []                                           # capped at 15 min, not 2^30 seconds
    db._exec("UPDATE alert_outbox SET claimed_at = now() - interval '16 minutes'")
    assert len(db.claim_outbox()) == 1


def test_an_alert_that_waited_arrives_marked_delayed(db):
    ids = db.enqueue_alerts([Alert("Dev", "at_risk", "Heads up, Dev: a delay\nline two", ("D",), "slip")])
    db._exec("UPDATE alert_outbox SET created_at = now() - interval '2 hours 5 minutes'")
    n = MemoryNotifier()
    deliver_outbox(db, n, ids)
    assert n.sent[0].text.startswith("Heads up, Dev: a delay (delayed: saved 2 h ago)\nline two")
    fresh = db.enqueue_alerts([Alert("Dev", "info", "On time\nbody", ("D",), "slip")])
    deliver_outbox(db, n, fresh)
    assert n.sent[1].text == "On time\nbody"                                 # a fresh alert is left exactly as written


def test_alerts_older_than_a_day_are_stale_and_not_retried(db):
    db.enqueue_alerts([Alert("Rahul", "info", "h\nb", ("A",), "system")])
    db._exec("UPDATE alert_outbox SET created_at = now() - interval '25 hours'")
    assert db.claim_outbox() == []
    stats = db.outbox_stats()
    assert stats["expired"] == 1 and stats["pending"] == 0


def test_the_exact_message_is_what_gets_sent(db):
    original = Alert("Dev", "at_risk", "Heads up, Dev\nline", ("D",), "slip",
                     (AlertItem("D", "“Launch page” is late", ("Slipping",), "Launch page"),))
    ids = db.enqueue_alerts([original])
    n = MemoryNotifier()
    deliver_outbox(db, n, ids)
    assert n.sent == [original]                                              # text, buttons and items all preserved


def test_a_dependency_cycle_alert_also_goes_through_the_outbox(db):
    n = MemoryNotifier()
    apply_snapshot(db, n, chain(), [GOAL], TODAY)
    bad = Task("A", "API spec", "Rahul", date(2026, 12, 8), 3, goal_id="g1", depends_on=("D",))
    out = apply_snapshot(db, n, [bad], [], TODAY)
    assert len(out) == 1 and "circle" in n.sent[0].text and counts(db)["unsent"] == 0


# ---------------------------------------------------------------- meeting approval cards
def _gateway():
    reply = json.dumps({"tasks": [{"id": "t1", "title": "Write spec", "owner_name": "Rahul", "due_date": "2026-12-05",
                                   "goal_id": "g1", "confidence": 0.9}]})

    def completion(model, **kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    return Gateway(Routing("t", {"T2": ["p/a"]}, {"extract_tasks": {"tiers": ["T2"]}}), completion)


def test_a_meeting_card_that_failed_to_post_is_posted_later(db):
    notion = FakeNotion()
    notion.ds["T"] = notion_chain()
    notion.ds["M"] = [{"id": "mtg-outbox-1", "created_time": "2026-11-30T00:00:00Z", "properties": {
        "Meeting name": {"title": [{"plain_text": "Weekly sync"}]}, "Date": {"date": {"start": "2026-11-30"}}}}]
    notion.texts["mtg-outbox-1"] = "Rahul will write the API spec by 5 December for the Q1 launch goal."
    sync_notion(db, MemoryNotifier(), notion, CFG, SYNC_TODAY)

    # Slack is down when the note is processed: proposals are saved, the card is not posted.
    assert extract_new_meetings(db, notion, _gateway(), CFG, SYNC_TODAY, notifier=DownNotifier()) == 1
    assert db._exec("SELECT count(*) n FROM proposals WHERE card_sent_at IS NULL")[0]["n"] == 1

    n = MemoryNotifier()
    assert sweep(db, n, grace_seconds=0)["cards"] == 1
    (mid, title, rows), = n.proposal_cards
    assert (mid, title) == ("mtg-outbox-1", "Weekly sync") and rows[0]["status"] == "Proposed"
    assert sweep(db, n, grace_seconds=0)["cards"] == 0 and len(n.proposal_cards) == 1   # posted once only


def test_cards_that_were_already_posted_are_never_reposted(db):
    notion = FakeNotion()
    notion.ds["T"] = notion_chain()
    notion.ds["M"] = [{"id": "mtg-outbox-2", "created_time": "2026-11-30T00:00:00Z", "properties": {
        "Meeting name": {"title": [{"plain_text": "Weekly sync"}]}, "Date": {"date": {"start": "2026-11-30"}}}}]
    notion.texts["mtg-outbox-2"] = "Rahul will write the API spec by 5 December for the Q1 launch goal."
    sync_notion(db, MemoryNotifier(), notion, CFG, SYNC_TODAY)
    n = MemoryNotifier()
    extract_new_meetings(db, notion, _gateway(), CFG, SYNC_TODAY, notifier=n)
    assert len(n.proposal_cards) == 1
    assert sweep(db, n, grace_seconds=0)["cards"] == 0 and len(n.proposal_cards) == 1
