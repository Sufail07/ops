"""Plain domain objects shared by the deterministic engines."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Status(str, Enum):
    TODO = "todo"
    DOING = "doing"
    BLOCKED = "blocked"
    DONE = "done"


@dataclass(frozen=True)
class Goal:
    id: str
    name: str
    weight: int = 3  # 1..5, set by founders in Notion Config
    target_date: date | None = None


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    owner: str | None = None
    due: date | None = None
    estimate_days: int = 1
    status: Status = Status.TODO
    goal_id: str | None = None
    depends_on: tuple[str, ...] = ()
    last_update: date | None = None


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    is_lead: bool = False
    backup_id: str | None = None
    slack_user_id: str | None = None
    email: str | None = None
