"""Deterministic, explainable, stable priority ranking.

score = (goal_weight / 3) x urgency x criticality x blocking
Band (P0..P3) only changes when the score clears a boundary by a margin (hysteresis),
or when a real input changed (fingerprint differs from the previous snapshot).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from core.graph.engine import Analysis, Risk, TaskGraph
from core.models import Status

BANDS = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True)
class PriorityParams:
    thresholds: tuple[float, float, float] = (3.0, 1.8, 1.0)  # minimum score for P0, P1, P2
    margin: float = 0.15           # fraction past a boundary needed to change band without a real event
    horizon_days: int = 7
    default_weight: int = 1        # tasks with no goal link rank low


@dataclass(frozen=True)
class PrevSnapshot:
    band: str
    fingerprint: str


@dataclass(frozen=True)
class PriorityItem:
    task_id: str
    owner: str | None
    band: str
    score: float
    rank: int                  # 1 = most important, within the whole team
    fingerprint: str
    why: str
    reasons: tuple[str, ...]


def raw_band(score: float, t: tuple[float, float, float]) -> int:
    for i, limit in enumerate(t):
        if score >= limit:
            return i
    return 3


def stable_band(score: float, prev_band: str | None, real_change: bool, p: PriorityParams) -> int:
    raw = raw_band(score, p.thresholds)
    if prev_band is None or real_change:
        return raw
    cur = BANDS.index(prev_band)
    while True:
        if cur > 0 and score >= p.thresholds[cur - 1] * (1 + p.margin):
            cur -= 1  # promote (more urgent)
        elif cur < 3 and score < p.thresholds[cur] * (1 - p.margin):
            cur += 1  # demote
        else:
            return cur


def _urgency(days_left: int | None, horizon: int) -> float:
    if days_left is None:
        return 1.0
    if days_left < 0:
        return 3.0 + min(-days_left, 10) * 0.1
    return 1.0 + 2.0 / (1.0 + days_left / horizon)


def _fingerprint(graph: TaskGraph, tid: str, analysis: Analysis) -> str:
    t = graph.tasks[tid]
    goal = graph.goals.get(t.goal_id) if t.goal_id else None
    st = analysis[tid]
    parts = (t.due, t.owner, t.status.value, t.goal_id, goal.weight if goal else None,
             goal.target_date if goal else None, tuple(sorted(graph.deps[tid])),
             t.estimate_days, st.risk.value, st.delay_days)
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:12]


def _fmt(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def prioritise(graph: TaskGraph, analysis: Analysis, today: date,
               params: PriorityParams = PriorityParams(),
               previous: dict[str, PrevSnapshot] | None = None) -> list[PriorityItem]:
    previous = previous or {}
    scored: list[tuple[int, float, str]] = []
    meta: dict[str, tuple[str, tuple[str, ...], str]] = {}
    for tid in graph.topo:
        t = graph.tasks[tid]
        if t.status == Status.DONE:
            continue
        st = analysis[tid]
        goal = graph.goals.get(t.goal_id) if t.goal_id else None
        weight = goal.weight if goal else params.default_weight
        deadlines = [d for d in (t.due, st.needed_by) if d is not None]
        days_left = (min(deadlines) - today).days if deadlines else None
        n_down = len([d for d in graph.downstream(tid) if graph.tasks[d].status != Status.DONE])
        crit = 1.0 + 0.25 * min(n_down, 8) + {Risk.OFF_TRACK: 0.5, Risk.AT_RISK: 0.25}.get(st.risk, 0.0)
        blocking = 1.15 if any(graph.tasks[d].status != Status.DONE for d in graph.dependents[tid]) else 1.0
        score = (weight / 3) * _urgency(days_left, params.horizon_days) * crit * blocking

        fp = _fingerprint(graph, tid, analysis)
        prev = previous.get(tid)
        band_i = stable_band(score, prev.band if prev else None, bool(prev) and prev.fingerprint != fp, params)

        reasons = []
        reasons.append(f"goal '{goal.name}' (weight {weight}/5)" if goal else "no goal linked")
        if days_left is not None:
            reasons.append(f"deadline pressure: {days_left} day(s) left" if days_left >= 0
                           else f"overdue by {-days_left} day(s)")
        if n_down:
            reasons.append(f"{n_down} task(s) depend on it")
        if st.risk in (Risk.AT_RISK, Risk.OFF_TRACK):
            reasons.append(f"risk: {st.risk.value.replace('_', ' ')}")
        meta[tid] = (BANDS[band_i], tuple(reasons), fp)
        scored.append((band_i, score, tid))

    scored.sort(key=lambda x: (x[0], -x[1], graph.tasks[x[2]].due or date.max, x[2]))
    items = []
    for rank, (band_i, score, tid) in enumerate(scored, start=1):
        band, reasons, fp = meta[tid]
        items.append(PriorityItem(tid, graph.tasks[tid].owner, band, round(score, 2), rank, fp,
                                  f"{band}: " + "; ".join(reasons), reasons))
    return items
