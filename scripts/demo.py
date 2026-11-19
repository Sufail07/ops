"""Demo helper: seed, drive, reset and clean up the data for the Loom recording. Runs on the DATABASE_URL and the
Notion workspace in .env (your real ones), and only touches pages it created (see demo/state.json).

  python scripts/demo.py seed        create (or repair) the demo goal + 4-task chain + people, then sync. Safe to re-run
  python scripts/demo.py status      show what the system currently believes (risk, priority, slack)
  python scripts/demo.py meeting     create a fresh mixed-language meeting note in Notion (a new "take")
  python scripts/demo.py poll        run one poll now (sync Notion, extract meetings, send follow-ups)
  python scripts/demo.py slip [N]    move the first task's due date N days later in Notion (default 28), then poll
  python scripts/demo.py reset       back to the clean starting state, ready for another take
  python scripts/demo.py cleanup     remove everything the demo created (Notion pages and demo people)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.followups.hours import WorkingHours  # noqa: E402
from core.graph.engine import TaskGraph  # noqa: E402
from core.models import Person  # noqa: E402
from core.notion import mapping as m  # noqa: E402
from core.poll import run_poll  # noqa: E402
from core.priority.engine import prioritise  # noqa: E402
from core.runtime import get_runtime  # noqa: E402
from core.settings import get_settings  # noqa: E402

STATE = ROOT / "demo" / "state.json"
MARKER = "Demo data created by scripts/demo.py"
PROJECT_NAME = "[DEMO] Launch partner program"

# (key, title, owner, due offset in days, estimate days, depends on, status)
CHAIN = [
    ("A", "Finalise partner API spec", "Rahul", 8, 3, None, "In progress"),
    ("B", "Build partner backend", "Meera", 26, 5, "A", "Not started"),
    ("C", "QA pass on the backend", "Priya", 33, 4, "B", "Not started"),
    ("D", "Launch partner page", "Sufail", 40, 2, "C", "Not started"),
]
GOAL_OFFSET = 52
PEOPLE = {"Rahul": ["राहुल", "રાહુલ"], "Meera": ["मीरा", "મીરા"], "Priya": ["प्रिया", "પ્રિયા"], "Dev": ["देव", "દેવ"]}
HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
GU = ["જાન્યુઆરી", "ફેબ્રુઆરી", "માર્ચ", "એપ્રિલ", "મે", "જૂન", "જુલાઈ", "ઑગસ્ટ", "સપ્ટેમ્બર", "ઑક્ટોબર", "નવેમ્બર", "ડિસેમ્બર"]


# ----- helpers ----------------------------------------------------------------------------------
def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def today_local(cfg) -> date:
    return WorkingHours.from_settings(cfg).local_today(datetime.now(timezone.utc))


def title(text: str) -> dict:
    return {"title": [{"text": {"content": text}}]}


def rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:1900]}}]}


def dateprop(d: date) -> dict:
    return {"date": {"start": d.isoformat()}}


def task_props(t, base: date, ids: dict, project: str) -> dict:
    key, name, owner, off, est, dep, status = t
    props = {m.T_TITLE: title(name), m.T_DUE: dateprop(base + timedelta(days=off)), m.T_STATUS: {"status": {"name": status}},
             m.T_OWNER_TEXT: rich(owner), m.T_ESTIMATE: {"number": est}, m.T_GOAL: {"relation": [{"id": project}]},
             "Description": rich(MARKER)}
    if dep:
        props[m.T_DEPENDS] = {"relation": [{"id": ids[dep]}]}
    return props


def poll(rt, cfg) -> dict:
    """Prefer the running service (one process does everything); otherwise run the poll in this process."""
    token = cfg.admin_token
    try:
        req = urllib.request.Request("http://127.0.0.1:8077/admin/poll", method="POST",
                                     headers={"Authorization": f"Bearer {token}"})
        out = json.load(urllib.request.urlopen(req, timeout=180))
        print("poll (via the running service):", out)
        return out
    except Exception:
        out = run_poll(rt, cfg)
        print("poll (service not running, ran in this process):", out)
        return out


# ----- commands -----------------------------------------------------------------------------------
def ensure_chain(notion, cfg, st: dict, today: date) -> dict:
    """Make sure the demo goal and the 4-task chain exist in Notion, creating ONLY what is missing.

    Safe to run any time: after you deleted the tasks, after a half-finished take, or on a fresh workspace.
    If anything had to be created, the dates restart from today.
    """
    st = dict(st)
    if m.T_OWNER_TEXT not in notion.get_data_source(cfg.notion_tasks_ds)["properties"]:
        notion.update_data_source(cfg.notion_tasks_ds, {m.T_OWNER_TEXT: {"rich_text": {}}})
        print(f"added the '{m.T_OWNER_TEXT}' column to Tasks Tracker")
    live_projects = {p["id"] for p in notion.query_all(cfg.notion_projects_ds)}
    live_tasks = {p["id"] for p in notion.query_all(cfg.notion_tasks_ds)}
    ids: dict[str, str] = dict(st.get("tasks", {}))
    missing_project = st.get("project") not in live_projects
    missing = [t[0] for t in CHAIN if ids.get(t[0]) not in live_tasks]
    if missing_project or missing:
        st["base"] = today.isoformat()             # something is new, so start the clock again from today
    base = date.fromisoformat(st["base"])
    if missing_project:
        st["project"] = notion.create_page(cfg.notion_projects_ds, {
            m.P_TITLE: title(PROJECT_NAME), m.P_WEIGHT: {"number": 5},
            m.P_TARGET: dateprop(base + timedelta(days=GOAL_OFFSET))})["id"]
        print("created the demo goal")
    for t in CHAIN:
        if t[0] in missing:
            ids[t[0]] = notion.create_page(cfg.notion_tasks_ds, task_props(t, base, ids, st["project"]))["id"]
            print(f"created task: {t[1]}")
    st["tasks"] = ids
    st.setdefault("meetings", [])
    st.setdefault("take", 0)
    return st


def restore_chain(notion, st: dict) -> None:
    """Put every chain task back to its baseline (dates, status, owner, goal and dependency links)."""
    base = date.fromisoformat(st["base"])
    for t in CHAIN:
        notion.update_page(st["tasks"][t[0]], task_props(t, base, st["tasks"], st["project"]))
    notion.update_page(st["project"], {m.P_WEIGHT: {"number": 5}, m.P_TARGET: dateprop(base + timedelta(days=GOAL_OFFSET))})


def seed() -> None:
    """Create (or repair) the demo goal and chain. Idempotent: run it as often as you like."""
    rt, cfg = get_runtime(), get_settings()
    st = ensure_chain(rt.notion, cfg, load_state(), today_local(cfg))
    restore_chain(rt.notion, st)
    rt.db.upsert_people([Person(f"demo:{n.lower()}", n, tuple(a)) for n, a in PEOPLE.items()])
    save_state(st)
    print(f"demo ready: 1 goal, {len(st['tasks'])} tasks, {len(PEOPLE)} people. Syncing...")
    poll(rt, cfg)
    status()


def status() -> None:
    rt, cfg = get_runtime(), get_settings()
    today = today_local(cfg)
    tasks, goals, prev = rt.db.load_daily_state(today)
    g = TaskGraph(tasks, goals)
    a = g.analyze(today)
    items = {i.task_id: i for i in prioritise(g, a, today, previous=prev)}
    st = load_state()
    demo_ids = set(st.get("tasks", {}).values())
    print(f"{'task':<30}{'owner':<9}{'due':<12}{'projected':<12}{'slack':<7}{'risk':<10}band")
    for t in tasks:
        s, it = a[t.id], items.get(t.id)
        mark = "" if t.id in demo_ids else "  (not demo)"
        print(f"{t.title[:28]:<30}{(t.owner or '-'):<9}{str(t.due or '-'):<12}{str(s.projected_finish or '-'):<12}"
              f"{str(s.slack_days if s.slack_days is not None else '-'):<7}{s.risk.value:<10}{it.band if it else '-'}{mark}")
    for goal in goals:
        if goal.id == st.get("project"):
            h = g.goal_health(goal.id, a)
            print(f"\ngoal '{goal.name}': {h.status} ({h.reason})")


def meeting() -> None:
    rt, cfg = get_runtime(), get_settings()
    st = load_state()
    if not st.get("project"):
        sys.exit("Run `python scripts/demo.py seed` first.")
    today = today_local(cfg)
    st["take"] += 1
    d = lambda n: today + timedelta(days=n)
    en = lambda x: f"{x.day} {x.strftime('%B')}"
    hi = lambda x: f"{x.day} {HI[x.month - 1]}"
    gu = lambda x: f"{x.day} {GU[x.month - 1]}"
    lines = [
        f"Partner onboarding kickoff, {en(today)}. Present: Sufail, Rahul, Meera, Priya, Dev.",
        "This is all for the Launch partner program.",
        f"Sufail: partner onboarding guide main {en(d(15))} tak likh dunga.",
        "Dev: onboarding email template guide ke baad banega, next Friday tak bhej dunga.",
        f"Priya: હું partner contract draft નું review {gu(d(14))} સુધીમાં કરીશ.",
        f"Meera: मैं {hi(d(19))} तक partner sandbox accounts set up कर दूँगी.",
        "Nobody has taken the partner FAQ page yet.",
        "Decision: partner welcome call ki date baad mein decide karenge, Rahul will run it.",
    ]
    blocks = [{"object": "block", "type": "paragraph",
               "paragraph": {"rich_text": [{"type": "text", "text": {"content": ln}}]}} for ln in lines]
    page = rt.notion.create_page(cfg.notion_meetings_ds, {
        "Meeting name": title(f"[DEMO] Partner onboarding kickoff (take {st['take']})"),
        "Date": dateprop(today)}, children=blocks)
    st["meetings"].append(page["id"])
    save_state(st)
    print(f"created meeting note, take {st['take']}. Next: `python scripts/demo.py poll` (or wait up to 5 minutes).")


def slip(days: int = 28) -> None:
    rt, cfg = get_runtime(), get_settings()
    st = load_state()
    if not st.get("project"):
        sys.exit("Run `python scripts/demo.py seed` first.")
    base = date.fromisoformat(st["base"])
    new = base + timedelta(days=CHAIN[0][3] + days)
    rt.notion.update_page(st["tasks"]["A"], {m.T_DUE: dateprop(new)})
    print(f"'{CHAIN[0][1]}' ({CHAIN[0][2]}) due date moved to {new} in Notion. Polling...")
    poll(rt, cfg)


def _archive_matching(rt, cfg, everything: bool) -> list[str]:
    st = load_state()
    notion, gone = rt.notion, []
    for page in notion.query_all(cfg.notion_tasks_ds):
        desc = m._plain(m._prop(page, "Description").get("rich_text"))
        is_chain = page["id"] in st.get("tasks", {}).values()
        if "Proposed by Ops Brain" in desc or (everything and is_chain):
            notion.archive_page(page["id"])
            gone.append(page["id"])
    for page in notion.query_all(cfg.notion_meetings_ds):
        if m._plain(m._prop(page, "Meeting name").get("title")).startswith("[DEMO]"):
            notion.archive_page(page["id"])
            gone.append(page["id"])
    return gone


def _forget(rt, task_ids: list[str], meeting_ids: list[str]) -> None:
    """Clear alert/follow-up/proposal state for demo items so a retake alerts again (alerts dedupe per hour)."""
    rt.db._exec("DELETE FROM risk_events WHERE task_id = ANY(%s)", (task_ids,))
    rt.db._exec("DELETE FROM followups WHERE task_id = ANY(%s)", (task_ids,))
    rt.db._exec("DELETE FROM proposals WHERE meeting_id = ANY(%s) OR meeting_id LIKE 'slip:%%'", (meeting_ids,))


def reset() -> None:
    rt, cfg = get_runtime(), get_settings()
    st = load_state()
    if not st.get("project"):
        sys.exit("Nothing to reset: run `python scripts/demo.py seed`.")
    st = ensure_chain(rt.notion, cfg, st, today_local(cfg))   # heals if tasks were deleted by hand
    removed = _archive_matching(rt, cfg, everything=False)
    restore_chain(rt.notion, st)
    _forget(rt, list(st["tasks"].values()) + removed, st["meetings"])
    st["meetings"] = []
    save_state(st)
    print(f"reset: chain dates/statuses restored, {len(removed)} generated page(s) archived. Syncing...")
    poll(rt, cfg)
    status()


def cleanup() -> None:
    rt, cfg = get_runtime(), get_settings()
    st = load_state()
    removed = _archive_matching(rt, cfg, everything=True)
    if st.get("project"):
        try:
            rt.notion.archive_page(st["project"])
            removed.append(st["project"])
        except Exception:  # already deleted by hand: nothing to do
            pass
    _forget(rt, removed, st.get("meetings", []))
    rt.db._exec("DELETE FROM people WHERE id LIKE 'demo:%%'")
    STATE.unlink(missing_ok=True)
    print(f"cleanup: archived {len(removed)} Notion page(s), removed demo people. Syncing...")
    poll(rt, cfg)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "slip":
        slip(int(sys.argv[2]) if len(sys.argv) > 2 else 28)
    elif cmd in ("seed", "status", "meeting", "poll", "reset", "cleanup"):
        {"seed": seed, "status": status, "meeting": meeting, "reset": reset, "cleanup": cleanup,
         "poll": lambda: poll(get_runtime(), get_settings())}[cmd]()
    else:
        sys.exit(__doc__)
