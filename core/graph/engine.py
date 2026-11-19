"""Deterministic dependency-graph engine: projected finish dates, slack, risk, goal health.

Forward pass  : projected_finish(t) = when t will really finish, given its own date
                (adjusted for overdue/blocked/stale) and when its dependencies finish.
Backward pass : needed_by(t) = latest date t may finish without breaking a dependent
                (or the goal target date for the last task in a chain).
slack(t)      : needed_by(t) - projected_finish(t), in calendar days.

No LLM and no I/O here. Everything is a pure function of the tasks, goals and today's date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Iterable

from core.models import Goal, Status, Task


class Risk(str, Enum):
    DONE = "done"
    OK = "ok"
    UNKNOWN = "unknown"  # not enough data (no date) to judge
    AT_RISK = "at_risk"
    OFF_TRACK = "off_track"


RISK_RANK = {Risk.DONE: -1, Risk.OK: 0, Risk.UNKNOWN: 1, Risk.AT_RISK: 2, Risk.OFF_TRACK: 3}


class CycleError(ValueError):
    def __init__(self, members: list[str]):
        super().__init__(f"Dependency cycle among tasks: {', '.join(sorted(members))}")
        self.members = members


@dataclass(frozen=True)
class RiskParams:
    buffer_days: int = 2          # slack at or below this = at risk
    stale_days: int = 5           # no update for this long (open task) = stale
    stale_penalty_days: int = 2   # projected finish is pushed out by this for stale tasks
    blocked_penalty_days: int = 3


@dataclass(frozen=True)
class TaskState:
    task_id: str
    projected_finish: date | None
    needed_by: date | None
    slack_days: int | None
    delay_days: int                      # projected_finish minus own due date, floored at 0
    risk: Risk
    critical_predecessor: str | None     # dependency that sets this task's projected finish
    critical_successor: str | None       # dependent that sets this task's needed_by
    reasons: tuple[str, ...] = ()


Analysis = dict[str, TaskState]


@dataclass(frozen=True)
class GoalHealth:
    goal_id: str
    status: str            # on_track | at_risk | off_track | no_tasks
    reason: str
    worst_task_id: str | None


class TaskGraph:
    def __init__(self, tasks: Iterable[Task], goals: Iterable[Goal] = ()):
        self.tasks: dict[str, Task] = {t.id: t for t in tasks}
        self.goals: dict[str, Goal] = {g.id: g for g in goals}
        self.warnings: list[str] = []
        self.deps: dict[str, list[str]] = {}
        self.dependents: dict[str, list[str]] = {tid: [] for tid in self.tasks}
        for t in self.tasks.values():
            kept = []
            for d in t.depends_on:
                if d == t.id:
                    raise CycleError([t.id])
                if d not in self.tasks:
                    self.warnings.append(f"Task '{t.title}' depends on unknown task id {d}; ignored")
                    continue
                kept.append(d)
                self.dependents[d].append(t.id)
            self.deps[t.id] = kept
        self.topo = self._topological_order()

    def _topological_order(self) -> list[str]:
        indeg = {tid: len(ds) for tid, ds in self.deps.items()}
        ready = sorted(tid for tid, n in indeg.items() if n == 0)
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            for d in sorted(self.dependents[tid]):
                indeg[d] -= 1
                if indeg[d] == 0:
                    ready.append(d)
        if len(order) != len(self.tasks):
            raise CycleError([tid for tid, n in indeg.items() if n > 0])
        return order

    # ----- queries -------------------------------------------------------------------
    def downstream(self, task_id: str) -> list[str]:
        """All transitive dependents, in dependency order."""
        seen: set[str] = set()
        stack = list(self.dependents.get(task_id, []))
        while stack:
            t = stack.pop()
            if t not in seen:
                seen.add(t)
                stack.extend(self.dependents[t])
        return [t for t in self.topo if t in seen]

    def upstream(self, task_id: str) -> list[str]:
        seen: set[str] = set()
        stack = list(self.deps.get(task_id, []))
        while stack:
            t = stack.pop()
            if t not in seen:
                seen.add(t)
                stack.extend(self.deps[t])
        return [t for t in self.topo if t in seen]

    # ----- analysis ------------------------------------------------------------------
    def analyze(self, today: date, params: RiskParams = RiskParams()) -> Analysis:
        base_reasons: dict[str, list[str]] = {}
        projected: dict[str, date | None] = {}
        crit_pred: dict[str, str | None] = {}

        # Forward pass
        for tid in self.topo:
            t = self.tasks[tid]
            reasons: list[str] = []
            if t.status == Status.DONE:
                projected[tid] = None
                crit_pred[tid] = None
                base_reasons[tid] = reasons
                continue
            base = self._own_finish(t, today, params, reasons)
            open_deps = [d for d in self.deps[tid] if self.tasks[d].status != Status.DONE]
            dated = [(projected[d], d) for d in open_deps if projected.get(d) is not None]
            for d in open_deps:
                if projected.get(d) is None:
                    reasons.append(f"dependency '{self.tasks[d].title}' has no date")
            pred = None
            final = base
            if dated:
                ready, pred_id = max(dated, key=lambda x: (x[0], x[1]))
                from_deps = ready + timedelta(days=t.estimate_days)
                if base is None or from_deps > base:
                    final = from_deps
                    pred = pred_id
            projected[tid] = final
            crit_pred[tid] = pred
            base_reasons[tid] = reasons

        # Backward pass
        needed: dict[str, date | None] = {}
        crit_succ: dict[str, str | None] = {}
        for tid in reversed(self.topo):
            t = self.tasks[tid]
            open_dependents = [d for d in self.dependents[tid] if self.tasks[d].status != Status.DONE]
            cands: list[tuple[date, str]] = []
            for d in open_dependents:
                dt = self.tasks[d]
                limit = [x for x in (dt.due, needed.get(d)) if x is not None]
                if limit:
                    cands.append((min(limit) - timedelta(days=dt.estimate_days), d))
            if cands:
                nb, succ = min(cands, key=lambda x: (x[0], x[1]))
                needed[tid], crit_succ[tid] = nb, succ
            else:
                goal = self.goals.get(t.goal_id) if t.goal_id else None
                nb = goal.target_date if goal and goal.target_date else t.due
                needed[tid], crit_succ[tid] = nb, None

        # Assemble
        out: Analysis = {}
        for tid in self.topo:
            t = self.tasks[tid]
            if t.status == Status.DONE:
                out[tid] = TaskState(tid, None, None, None, 0, Risk.DONE, None, None, ())
                continue
            pf, nb = projected[tid], needed[tid]
            reasons = list(base_reasons[tid])
            delay = max(0, (pf - t.due).days) if pf and t.due else 0
            slack = (nb - pf).days if pf and nb else None
            if pf is None:
                risk = Risk.UNKNOWN
                reasons.append("no due date")
            elif slack is not None and slack < 0:
                risk = Risk.OFF_TRACK
            elif (slack is not None and slack <= params.buffer_days) or delay > 0:
                risk = Risk.AT_RISK
            elif nb is None:
                risk = Risk.UNKNOWN
                reasons.append("no deadline to measure against")
            else:
                risk = Risk.OK
            out[tid] = TaskState(tid, pf, nb, slack, delay, risk, crit_pred[tid], crit_succ[tid], tuple(reasons))
        return out

    @staticmethod
    def _own_finish(t: Task, today: date, p: RiskParams, reasons: list[str]) -> date | None:
        if t.due is None:
            return None
        f = t.due
        if f < today:
            reasons.append(f"overdue by {(today - f).days} day(s)")
            f = today
        if t.status == Status.BLOCKED:
            reasons.append("marked blocked")
            f = max(f, today) + timedelta(days=p.blocked_penalty_days)
        if t.last_update and (today - t.last_update).days > p.stale_days:
            reasons.append(f"no update for {(today - t.last_update).days} days")
            f = f + timedelta(days=p.stale_penalty_days)
        return f

    # ----- chains --------------------------------------------------------------------
    def root_cause_chain(self, task_id: str, analysis: Analysis) -> list[str]:
        """[root slipped task, ..., task_id] following the critical predecessors."""
        chain = [task_id]
        while (p := analysis[chain[0]].critical_predecessor) is not None:
            chain.insert(0, p)
        return chain

    def path_to_goal(self, task_id: str, analysis: Analysis) -> list[str]:
        """[task_id, ..., last task before the goal] following the critical successors."""
        path = [task_id]
        while (s := analysis[path[-1]].critical_successor) is not None:
            path.append(s)
        return path

    # ----- goals ---------------------------------------------------------------------
    def goal_health(self, goal_id: str, analysis: Analysis) -> GoalHealth:
        direct = [t.id for t in self.tasks.values() if t.goal_id == goal_id]
        members: set[str] = set(direct)
        for tid in direct:
            members.update(self.upstream(tid))
        if not direct:
            return GoalHealth(goal_id, "no_tasks", "no tasks linked yet", None)
        open_members = [m for m in members if self.tasks[m].status != Status.DONE]
        if not open_members:
            return GoalHealth(goal_id, "on_track", "all tasks done", None)

        def worst(rank_pred) -> str | None:
            hits = [m for m in open_members if rank_pred(analysis[m].risk)]
            if not hits:
                return None
            return min(hits, key=lambda m: (analysis[m].slack_days if analysis[m].slack_days is not None else 10**6, m))

        off = worst(lambda r: r == Risk.OFF_TRACK)
        if off:
            s = analysis[off]
            return GoalHealth(goal_id, "off_track", f"'{self.tasks[off].title}' will finish {-s.slack_days} day(s) too late", off)
        risky = worst(lambda r: r in (Risk.AT_RISK, Risk.UNKNOWN))
        if risky:
            s = analysis[risky]
            why = "; ".join(s.reasons) or (f"only {s.slack_days} day(s) of slack" if s.slack_days is not None else "at risk")
            return GoalHealth(goal_id, "at_risk", f"'{self.tasks[risky].title}': {why}", risky)
        return GoalHealth(goal_id, "on_track", "all open tasks have enough slack", None)


# ----- slip impact ---------------------------------------------------------------------
@dataclass(frozen=True)
class Impact:
    task_id: str
    old_risk: Risk
    new_risk: Risk
    old_delay: int
    new_delay: int
    cause_chain: tuple[str, ...]      # root slipped task first, this task last
    path_to_goal: tuple[str, ...]     # this task first, last task before the goal last
    goal_id: str | None


def slip_impacts(before: Analysis, after: Analysis, graph: TaskGraph) -> list[Impact]:
    """Tasks whose risk or expected delay got worse between two analyses."""
    impacts: list[Impact] = []
    for tid in graph.topo:
        new = after[tid]
        if new.risk == Risk.DONE:
            continue
        old = before.get(tid)
        old_rank = RISK_RANK[old.risk] if old else RISK_RANK[Risk.OK]
        old_delay = old.delay_days if old else 0
        worse = RISK_RANK[new.risk] > old_rank or new.delay_days > old_delay
        if new.risk == Risk.UNKNOWN and (old is None or old.risk == Risk.UNKNOWN):
            worse = False  # missing data is a nudge, not a slip alert
        if not worse:
            continue
        path = graph.path_to_goal(tid, after)
        last = graph.tasks[path[-1]]
        goal_id = last.goal_id or graph.tasks[tid].goal_id
        impacts.append(Impact(tid, old.risk if old else Risk.OK, new.risk, old_delay, new.delay_days,
                              tuple(graph.root_cause_chain(tid, after)), tuple(path), goal_id))
    return impacts
