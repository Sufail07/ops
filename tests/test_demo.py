"""scripts/demo.py: seeding must repair whatever is missing (e.g. after tasks were deleted by hand), never duplicate."""
import importlib.util
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location("demo", Path(__file__).resolve().parents[1] / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

CFG = type("C", (), {"notion_tasks_ds": "T", "notion_projects_ds": "P", "notion_meetings_ds": "M"})()
TODAY = date(2026, 12, 1)


class Notion:
    def __init__(self):
        self.pages = {"P": {}, "T": {}}
        self.n = 0
        self.updates = []

    def get_data_source(self, ds):
        return {"properties": {"Owner (name)": {}}}

    def update_data_source(self, ds, props):
        pass

    def query_all(self, ds):
        return [{"id": i} for i in self.pages[ds]]

    def create_page(self, ds, props, children=None):
        self.n += 1
        pid = f"{ds}{self.n}"
        self.pages[ds][pid] = props
        return {"id": pid}

    def update_page(self, pid, props):
        self.updates.append((pid, props))


def titles(n):
    return sorted(p["Task name"]["title"][0]["text"]["content"] for p in n.pages["T"].values())


def test_fresh_workspace_creates_goal_and_chain_with_links():
    n = Notion()
    st = demo.ensure_chain(n, CFG, {}, TODAY)
    assert len(n.pages["P"]) == 1 and len(n.pages["T"]) == 4 and st["base"] == "2026-12-01"
    b = n.pages["T"][st["tasks"]["B"]]
    assert b["Depends on"]["relation"] == [{"id": st["tasks"]["A"]}] and b["Goal"]["relation"] == [{"id": st["project"]}]
    assert b["Owner (name)"]["rich_text"][0]["text"]["content"] == "Meera"


def test_nothing_is_duplicated_when_everything_exists():
    n = Notion()
    st = demo.ensure_chain(n, CFG, {}, TODAY)
    again = demo.ensure_chain(n, CFG, st, date(2026, 12, 9))
    assert len(n.pages["P"]) == 1 and len(n.pages["T"]) == 4
    assert again["tasks"] == st["tasks"] and again["base"] == st["base"]       # nothing new: dates not restarted


def test_after_deleting_all_tasks_by_hand_they_are_recreated_and_the_goal_is_reused():
    n = Notion()
    st = demo.ensure_chain(n, CFG, {}, TODAY)
    n.pages["T"].clear()                                                       # "I deleted all the tasks in Notion"
    healed = demo.ensure_chain(n, CFG, st, date(2026, 12, 9))
    assert len(n.pages["P"]) == 1 and healed["project"] == st["project"]
    assert len(n.pages["T"]) == 4 and set(healed["tasks"].values()).isdisjoint(st["tasks"].values())
    assert healed["base"] == "2026-12-09"                                      # dates restart from the new day


def test_one_missing_middle_task_is_recreated_and_relinked():
    n = Notion()
    st = demo.ensure_chain(n, CFG, {}, TODAY)
    del n.pages["T"][st["tasks"]["B"]]
    healed = demo.ensure_chain(n, CFG, st, TODAY)
    assert len(n.pages["T"]) == 4 and healed["tasks"]["A"] == st["tasks"]["A"] and healed["tasks"]["B"] != st["tasks"]["B"]
    demo.restore_chain(n, healed)                                              # C must now depend on the NEW B
    c_update = next(p for pid, p in n.updates if pid == healed["tasks"]["C"])
    assert c_update["Depends on"]["relation"] == [{"id": healed["tasks"]["B"]}]
