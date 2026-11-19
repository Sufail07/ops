"""Take-home demo: one mixed Hindi/Gujarati/English meeting note -> structured PROPOSED tasks.

  python demo/run_demo.py --mock   # replays demo/mock_reply.json (no API key, no network)
  python demo/run_demo.py          # live: needs a hosted-provider key, e.g. OPENROUTER_API_KEY or GROQ_API_KEY
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.extractor.extract import extract_proposals  # noqa: E402
from core.gateway import Gateway, JsonlRunLog, Routing  # noqa: E402
from core.models import Goal  # noqa: E402

GOALS = [Goal("g_partner", "Launch the partner program", 5, date(2027, 1, 31)),
         Goal("g_hiring", "Hire two engineers", 2, date(2027, 3, 31))]
PEOPLE = {"Rahul": ["राहुल", "રાહુલ"], "Meera": ["मीरा", "મીરા"], "Priya": ["प्रिया", "પ્રિયા"], "Dev": ["देव", "દેવ"]}
MEETING_DATE = date(2026, 11, 19)


def mock_completion(**_kw):
    text = (ROOT / "demo" / "mock_reply.json").read_text()
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0))


def load_env(path: Path) -> None:
    """Minimal .env loader; never prints values."""
    import os
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def main() -> None:
    load_env(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--note", default=str(ROOT / "demo" / "sample_meeting.md"))
    args = ap.parse_args()

    gw = Gateway(Routing.load(ROOT / "config" / "routing.yaml"),
                 completion=mock_completion if args.mock else None,
                 run_log=JsonlRunLog(ROOT / "runs.jsonl"))
    props = extract_proposals(gw, Path(args.note).read_text(), MEETING_DATE, GOALS, PEOPLE)

    out = ROOT / "demo" / "out"
    out.mkdir(exist_ok=True)
    (out / "proposals.json").write_text(json.dumps([asdict(p) for p in props], indent=2, default=str, ensure_ascii=False))

    print(f"{'id':<4}{'owner':<8}{'due':<12}{'goal':<12}{'depends':<8}title  [check]")
    for p in props:
        print(f"{p.local_id:<4}{(p.owner or '?'):<8}{str(p.due or '?'):<12}{(p.goal_id or '?'):<12}"
              f"{','.join(p.depends_on) or '-':<8}{p.title}  [{'; '.join(p.needs_attention) or 'ok'}]")
    print(f"\n{len(props)} proposals written to {out / 'proposals.json'}. Nothing is created until a person approves.")


if __name__ == "__main__":
    main()
