"""Meeting notes -> PROPOSED tasks. The model only reads language; everything it returns is
validated and resolved by deterministic code. Nothing here creates or changes a Notion task:
the output is a list of proposals that a person must confirm."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from core.gateway import Gateway
from core.models import Goal

PROMPT_PATH = Path(__file__).resolve().parents[2] / "config" / "prompts" / "extract_tasks.md"


class RawTask(BaseModel):
    id: str
    title: str
    owner_name: str | None = None
    due_date: str | None = None
    due_text: str | None = None
    goal_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    evidence_quote: str = ""
    confidence: float = 0.5
    language: Literal["en", "hi", "gu", "mixed"] = "mixed"


class RawExtraction(BaseModel):
    tasks: list[RawTask] = Field(default_factory=list)


@dataclass
class Proposal:
    local_id: str
    title: str
    owner: str | None            # canonical person name, resolved from the alias map
    owner_spoken: str | None
    due: date | None
    due_text: str | None
    goal_id: str | None
    depends_on: list[str]        # local ids of other proposals
    evidence_quote: str
    confidence: float
    language: str
    needs_attention: list[str] = field(default_factory=list)  # what the approver should check
    status: str = "Proposed"     # a person moves it to Approved / Edited / Rejected


def build_messages(note: str, meeting_date: date, goals: list[Goal], people: dict[str, list[str]]) -> list[dict[str, str]]:
    system = PROMPT_PATH.read_text()
    goals_txt = "\n".join(f"- {g.id}: {g.name}" for g in goals) or "(none)"
    people_txt = ", ".join(people) or "(unknown)"
    user = (f"MEETING DATE: {meeting_date.isoformat()} ({meeting_date.strftime('%A')})\n"
            f"KNOWN GOALS:\n{goals_txt}\nTEAM MEMBERS: {people_txt}\n\n"
            f"<meeting_notes>\n{note}\n</meeting_notes>")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def resolve_owner(spoken: str | None, people: dict[str, list[str]]) -> str | None:
    """people: canonical name -> aliases (incl. Hindi/Gujarati spellings). Exact/alias match first, then close match."""
    if not spoken:
        return None
    key = spoken.strip().casefold()
    flat: dict[str, str] = {}
    for canon, aliases in people.items():
        flat[canon.casefold()] = canon
        for a in aliases:
            flat[a.casefold()] = canon
    if key in flat:
        return flat[key]
    close = difflib.get_close_matches(key, list(flat), n=1, cutoff=0.85)
    return flat[close[0]] if close else None


def _parse_date(s: str | None, meeting_date: date) -> tuple[date | None, str | None]:
    if not s:
        return None, None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None, f"unreadable date '{s}'"
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None, f"invalid date '{s}'"
    if d < meeting_date:
        return None, f"date {s} is before the meeting"
    return d, None


def to_proposals(raw: RawExtraction, meeting_date: date, goals: list[Goal], people: dict[str, list[str]]) -> list[Proposal]:
    goal_ids = {g.id for g in goals}
    local_ids = {t.id for t in raw.tasks}
    out: list[Proposal] = []
    for t in raw.tasks:
        flags: list[str] = []
        owner = resolve_owner(t.owner_name, people)
        if owner is None:
            flags.append("owner?" if not t.owner_name else f"owner '{t.owner_name}' not recognised")
        due, err = _parse_date(t.due_date, meeting_date)
        if err:
            flags.append(err)
        if due is None:
            flags.append("no due date")
        goal_id = t.goal_id if t.goal_id in goal_ids else None
        if t.goal_id and goal_id is None:
            flags.append(f"unknown goal id '{t.goal_id}' dropped")
        if goal_id is None:
            flags.append("no goal link")
        deps = []
        for d in t.depends_on:
            if d in local_ids and d != t.id:
                deps.append(d)
            else:
                flags.append(f"dependency '{d}' not found; dropped")
        if t.confidence < 0.6:
            flags.append("low confidence")
        elif due is not None and t.confidence < 0.8:
            flags.append(f"please confirm the date ('{t.due_text or t.due_date}')")
        out.append(Proposal(t.id, t.title.strip(), owner, t.owner_name, due, t.due_text, goal_id, deps,
                            t.evidence_quote, t.confidence, t.language, flags))
    return _drop_cycles(out)


def _drop_cycles(props: list[Proposal]) -> list[Proposal]:
    """Model-proposed dependencies must not form a cycle; break any that do and flag it."""
    by_id = {p.local_id: p for p in props}

    def reaches(src: str, target: str, seen: set[str]) -> bool:
        if src == target:
            return True
        if src in seen:
            return False
        seen.add(src)
        return any(reaches(d, target, seen) for d in by_id[src].depends_on)

    for p in props:
        for d in list(p.depends_on):
            if reaches(d, p.local_id, set()):
                p.depends_on.remove(d)
                p.needs_attention.append(f"dependency on '{d}' would create a cycle; dropped")
    return props


def extract_proposals(gateway: Gateway, note: str, meeting_date: date, goals: list[Goal],
                      people: dict[str, list[str]]) -> list[Proposal]:
    raw = gateway.call_json("extract_tasks", build_messages(note, meeting_date, goals, people),
                            RawExtraction, trigger="meeting")
    return to_proposals(raw, meeting_date, goals, people)
