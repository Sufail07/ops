"""Postgres access: a thin layer of SQL, no ORM. Postgres is a cache + log; Notion stays the truth.

Latency rules (Neon is ~200 ms away, so round trips are the cost, not SQL):
  * pool connections are autocommit: one statement = one round trip, not BEGIN + statement + COMMIT.
  * batches use executemany inside a pipeline + transaction: N rows = ~1 round trip.
  * related reads are pipelined (read_all): K queries = ~1 round trip.
"""
from __future__ import annotations

import contextlib
import zlib
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from core.alerts.format import Alert, alert_to_row
from core.models import Goal, Person, Status, Task
from core.priority.engine import PrevSnapshot, PriorityItem

Q = tuple[str, Sequence[Any] | None]

SQL_GOALS = "SELECT * FROM goals ORDER BY id"
SQL_EDGES = "SELECT task_id, depends_on_id FROM edges ORDER BY depends_on_id"
SQL_TASKS = "SELECT * FROM tasks ORDER BY id"
SQL_PEOPLE = "SELECT * FROM people ORDER BY id"
SQL_WRITES = "SELECT * FROM notion_writes"
SQL_PREV_SNAPSHOT = """SELECT DISTINCT ON (task_id) task_id, band, fingerprint FROM priority_snapshots
                       WHERE day < %s ORDER BY task_id, day DESC"""
SQL_RECENT = """SELECT DISTINCT task_id, new_risk, delay_days FROM risk_events
              WHERE notified_at > now() - interval '1 hour'"""
SQL_PENDING = "SELECT * FROM followups WHERE sent_at IS NULL AND outcome IS NULL ORDER BY due_at, id"
SQL_UNCONFIRMED = "SELECT meeting_id, created_at FROM proposals WHERE status='Proposed' AND meeting_id NOT LIKE 'slip:%'"


def _graph(goal_rows, edge_rows, task_rows) -> tuple[list[Task], list[Goal]]:
    goals = [Goal(r["id"], r["name"], r["weight"], r["target_date"]) for r in goal_rows]
    deps: dict[str, list[str]] = {}
    for r in edge_rows:
        deps.setdefault(r["task_id"], []).append(r["depends_on_id"])
    tasks = [Task(r["id"], r["title"], r["owner"], r["due"], r["estimate_days"], Status(r["status"]), r["goal_id"],
                  tuple(deps.get(r["id"], [])), r["last_update"]) for r in task_rows]
    return tasks, goals


def _people(rows) -> list[Person]:
    return [Person(r["id"], r["name"], tuple(r["aliases"]), r["is_lead"], r["backup_id"], r["slack_user_id"],
                   r.get("email")) for r in rows]


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 5):
        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, open=True,
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": True})  # pgbouncer-safe

    def close(self) -> None:
        self.pool.close()

    @contextlib.contextmanager
    def try_lock(self, name: str):
        """Session advisory lock: yields True if we got it, False if another worker holds it. Prevents two
        Notion syncs (poll + a Slack click) from running at once and double-alerting."""
        key = zlib.crc32(name.encode())
        with self.pool.connection() as c:
            got = c.execute("SELECT pg_try_advisory_lock(%s) AS got", (key,)).fetchone()["got"]
            try:
                yield got
            finally:
                if got:
                    c.execute("SELECT pg_advisory_unlock(%s)", (key,))

    # ----- generic helpers -----------------------------------------------------------
    def read_all(self, queries: list[Q]) -> list[list[dict[str, Any]]]:
        """Run several SELECTs in one round trip (pipelined)."""
        with self.pool.connection() as c:
            with c.pipeline():
                curs = [c.execute(sql, params) for sql, params in queries]
            return [cur.fetchall() for cur in curs]

    def _write_batch(self, statements: list[tuple[str, list[Sequence[Any]] | None]], *, fetch: bool = False):
        """Run many writes atomically in ONE round trip (~220 ms on Neon whether it is 1 row or 100).

        Statements are queued in a psycopg pipeline; Postgres runs everything between two syncs as a single
        implicit transaction, so the whole batch commits together or not at all (verified: a duplicate-key
        failure on statement 2 rolls back statement 1). Do NOT wrap this in conn.transaction(): explicit
        BEGIN/COMMIT are not pipelined and cost ~3 extra round trips. executemany() also syncs per call.

        Returns rowcounts per statement, or (fetch=True) the RETURNING rows of every statement.
        """
        per_statement: list[list[Any]] = []
        with self.pool.connection() as c, c.pipeline():
            for sql, rows in statements:
                curs = []
                for params in ([None] if rows is None else rows):
                    cur = c.cursor()
                    cur.execute(sql, params)
                    curs.append(cur)
                per_statement.append(curs)
        # results are only readable after the pipeline has synced (after the with-block)
        if fetch:
            return [r for curs in per_statement for cur in curs if cur.description for r in cur.fetchall()]
        return [sum(max(cur.rowcount, 0) for cur in curs) for curs in per_statement]

    def _exec(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with self.pool.connection() as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # ----- graph cache ---------------------------------------------------------------
    def save_graph(self, goals: Iterable[Goal], tasks: Iterable[Task]) -> None:
        """Upsert goals, tasks and rewrite the tasks' dependency edges: one atomic batch, one round trip."""
        self._write_batch(self._graph_statements(list(goals), list(tasks)))

    @staticmethod
    def _graph_statements(goals: list[Goal], tasks: list[Task]) -> list:
        return ([
            ("""INSERT INTO goals(id, name, weight, target_date) VALUES (%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, weight=EXCLUDED.weight,
                target_date=EXCLUDED.target_date, synced_at=now()""",
             [(g.id, g.name, g.weight, g.target_date) for g in goals]),
            ("""INSERT INTO tasks(id, title, owner, due, estimate_days, status, goal_id, last_update)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, owner=EXCLUDED.owner, due=EXCLUDED.due,
                estimate_days=EXCLUDED.estimate_days, status=EXCLUDED.status, goal_id=EXCLUDED.goal_id,
                last_update=EXCLUDED.last_update, synced_at=now()""",
             [(t.id, t.title, t.owner, t.due, t.estimate_days, t.status.value, t.goal_id, t.last_update) for t in tasks]),
            ("DELETE FROM edges WHERE task_id = ANY(%s)", [([t.id for t in tasks],)] if tasks else []),
            ("INSERT INTO edges(task_id, depends_on_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
             [(t.id, d) for t in tasks for d in t.depends_on]),
        ])

    def commit_snapshot(self, goals: list[Goal], tasks: list[Task], risk_rows: list[tuple], event_rows: list[tuple],
                        alerts: Iterable[Alert] = ()) -> list[int]:
        """Graph cache + risk events + audit events + the alerts to send, in ONE atomic round trip.

        The alerts go into alert_outbox in the same transaction as the change that caused them (a transactional
        outbox): either both are saved or neither, so a crash can never save a slip and lose its alert.
        Returns the new outbox ids, in order.
        """
        rows = [alert_to_row(a) for a in alerts]
        return [r["id"] for r in self._write_batch([
            *self._graph_statements(goals, tasks),
            ("""INSERT INTO risk_events(task_id, cause_task_id, old_risk, new_risk, delay_days, slack_days)
                VALUES (%s,%s,%s,%s,%s,%s)""", risk_rows),
            ("INSERT INTO events(kind, entity, payload, actor) VALUES (%s,%s,%s,%s)",
             [(k, e, Jsonb(p), a) for k, e, p, a in event_rows]),
            ("""INSERT INTO alert_outbox(owner, severity, kind, text, task_ids, items) VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id""", [(o, sv, k, t, ids, Jsonb(it)) for o, sv, k, t, ids, it in rows]),
        ], fetch=True)]

    # ----- alert outbox ----------------------------------------------------------------
    OUTBOX_EXPIRY_SECONDS = 24 * 3600   # an alert older than a day is stale: a slip that old has long since been dealt with

    def enqueue_alerts(self, alerts: Iterable[Alert]) -> list[int]:
        rows = [alert_to_row(a) for a in alerts]
        return [r["id"] for r in self._write_batch([
            ("""INSERT INTO alert_outbox(owner, severity, kind, text, task_ids, items) VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id""", [(o, sv, k, t, ids, Jsonb(it)) for o, sv, k, t, ids, it in rows])], fetch=True)]

    def claim_outbox(self, ids: list[int] | None = None, grace_seconds: float = 0, claim_ttl_seconds: float | None = None,
                     limit: int = 50) -> list[dict[str, Any]]:
        """Take unsent alerts for delivery.

        A claim stops two senders (the live delivery and the sweeper) sending the same alert. After a failed send the
        alert is retried with growing back-off (60 s, 2 min, 4 min ... capped at 15 min), so a long Slack outage
        means late alerts, not lost ones. claim_ttl_seconds overrides the back-off (used by tests).
        grace_seconds keeps the sweeper away from alerts that were only just saved (still being delivered live).
        Alerts older than OUTBOX_EXPIRY_SECONDS are not retried any more."""
        return self._exec("""
            UPDATE alert_outbox SET claimed_at = now(), attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM alert_outbox
                WHERE sent_at IS NULL
                  AND created_at > now() - make_interval(secs => %s)
                  AND (%s::bigint[] IS NULL OR id = ANY(%s))
                  AND created_at <= now() - make_interval(secs => %s)
                  AND (claimed_at IS NULL OR claimed_at < now() - make_interval(
                         secs => COALESCE(%s::float8, LEAST(900.0, 30.0 * power(2, attempts)))))
                ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
            RETURNING *""", (self.OUTBOX_EXPIRY_SECONDS, ids, ids, grace_seconds, claim_ttl_seconds, limit))

    def finish_outbox(self, sent_ids: list[int], failed: list[tuple[int, str]], notified_task_ids: list[str]) -> None:
        """Record delivery results and mark the matching risk events notified, in one round trip."""
        stmts: list[tuple] = []
        if sent_ids:
            stmts.append(("UPDATE alert_outbox SET sent_at = now(), last_error = NULL WHERE id = ANY(%s)", [(sent_ids,)]))
        if notified_task_ids:
            stmts.append(("UPDATE risk_events SET notified_at = now() WHERE task_id = ANY(%s) AND notified_at IS NULL",
                          [(notified_task_ids,)]))
        if failed:
            stmts.append(("UPDATE alert_outbox SET last_error = %s WHERE id = %s", [(err[:500], i) for i, err in failed]))
        if stmts:
            self._write_batch(stmts)

    def outbox_stats(self) -> dict[str, Any]:
        r = self._exec("""SELECT count(*) FILTER (WHERE sent_at IS NULL AND created_at > now() - make_interval(secs => %s)) AS pending,
                                 count(*) FILTER (WHERE sent_at IS NULL AND created_at <= now() - make_interval(secs => %s)) AS expired,
                                 count(*) FILTER (WHERE sent_at IS NOT NULL) AS sent,
                                 max(now() - created_at) FILTER (WHERE sent_at IS NULL) AS oldest_unsent
                          FROM alert_outbox""", (self.OUTBOX_EXPIRY_SECONDS, self.OUTBOX_EXPIRY_SECONDS))[0]
        r["oldest_unsent"] = r["oldest_unsent"].total_seconds() if r["oldest_unsent"] else None
        return r

    # ----- meeting cards -----------------------------------------------------------------
    def unposted_cards(self, grace_seconds: float = 60) -> list[dict[str, Any]]:
        """Proposals whose approval card was never posted (a crash or a Slack outage between save and post)."""
        return self._exec("""SELECT * FROM proposals WHERE card_sent_at IS NULL AND status = 'Proposed'
                             AND meeting_id NOT LIKE 'slip:%%' AND created_at <= now() - make_interval(secs => %s)
                             ORDER BY meeting_id, local_id""", (grace_seconds,))

    def mark_card_sent(self, meeting_id: str) -> None:
        self._exec("UPDATE proposals SET card_sent_at = now() WHERE meeting_id = %s", (meeting_id,))

    def upsert_goals(self, goals: Iterable[Goal]) -> None:
        self.save_graph(goals, [])

    def upsert_tasks(self, tasks: Iterable[Task]) -> None:
        self.save_graph([], tasks)

    def load_graph_inputs(self) -> tuple[list[Task], list[Goal]]:
        g, e, t = self.read_all([(SQL_GOALS, None), (SQL_EDGES, None), (SQL_TASKS, None)])
        return _graph(g, e, t)

    def load_snapshot_state(self):
        """Cache + alerts already sent in the last hour, in one round trip."""
        g, e, t, rec = self.read_all([(SQL_GOALS, None), (SQL_EDGES, None), (SQL_TASKS, None), (SQL_RECENT, None)])
        tasks, goals = _graph(g, e, t)
        return tasks, goals, {(r["task_id"], r["new_risk"], r["delay_days"]) for r in rec}

    def load_sync_state(self, today: date):
        """Everything the Notion sync needs, in one round trip."""
        g, e, t, w, prev, ppl, rec = self.read_all([(SQL_GOALS, None), (SQL_EDGES, None), (SQL_TASKS, None),
                                                    (SQL_WRITES, None), (SQL_PREV_SNAPSHOT, (today,)),
                                                    (SQL_PEOPLE, None), (SQL_RECENT, None)])
        tasks, goals = _graph(g, e, t)
        return (tasks, goals, {r["page_id"]: (r["payload_hash"], r["written_at"]) for r in w},
                {r["task_id"]: PrevSnapshot(r["band"], r["fingerprint"]) for r in prev}, _people(ppl),
                {(r["task_id"], r["new_risk"], r["delay_days"]) for r in rec})

    def load_followup_state(self):
        g, e, t, ppl, unconf, pending = self.read_all([(SQL_GOALS, None), (SQL_EDGES, None), (SQL_TASKS, None),
                                                       (SQL_PEOPLE, None), (SQL_UNCONFIRMED, None), (SQL_PENDING, None)])
        tasks, goals = _graph(g, e, t)
        return tasks, goals, _people(ppl), unconf, pending

    def load_daily_state(self, today: date):
        g, e, t, prev = self.read_all([(SQL_GOALS, None), (SQL_EDGES, None), (SQL_TASKS, None),
                                       (SQL_PREV_SNAPSHOT, (today,))])
        tasks, goals = _graph(g, e, t)
        return tasks, goals, {r["task_id"]: PrevSnapshot(r["band"], r["fingerprint"]) for r in prev}

    def delete_tasks(self, ids: list[str]) -> None:
        if ids:
            self._exec("DELETE FROM tasks WHERE id = ANY(%s)", (ids,))  # edges cascade

    def delete_goals(self, ids: list[str]) -> None:
        if ids:
            self._exec("DELETE FROM goals WHERE id = ANY(%s)", (ids,))  # tasks.goal_id set null

    # ----- append-only event log -----------------------------------------------------
    def append_event(self, kind: str, entity: str | None = None, payload: dict[str, Any] | None = None,
                     actor: str | None = None) -> int:
        return self._exec("INSERT INTO events(kind, entity, payload, actor) VALUES (%s,%s,%s,%s) RETURNING id",
                          (kind, entity, Jsonb(payload or {}), actor))[0]["id"]

    def append_events(self, rows: list[tuple[str, str | None, dict[str, Any], str | None]]) -> None:
        self._write_batch([("INSERT INTO events(kind, entity, payload, actor) VALUES (%s,%s,%s,%s)",
                            [(k, e, Jsonb(p), a) for k, e, p, a in rows])])

    def event_exists(self, kind: str, entity: str) -> bool:
        return bool(self._exec("SELECT 1 FROM events WHERE kind=%s AND entity=%s LIMIT 1", (kind, entity)))

    # ----- webhook idempotency -------------------------------------------------------
    def record_inbox(self, event_id: str, source: str) -> bool:
        """True if this event id is new (process it); False if it was already received."""
        return bool(self._exec("INSERT INTO inbox(event_id, source) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
                               (event_id, source)))

    def mark_inbox_processed(self, event_id: str) -> None:
        self._exec("UPDATE inbox SET processed_at=now() WHERE event_id=%s", (event_id,))

    # ----- priority snapshots --------------------------------------------------------
    def save_priority_snapshot(self, day: date, items: Iterable[PriorityItem]) -> None:
        self._write_batch([("""INSERT INTO priority_snapshots(day, task_id, score, band, fingerprint, why)
                               VALUES (%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (day, task_id) DO UPDATE SET score=EXCLUDED.score, band=EXCLUDED.band,
                               fingerprint=EXCLUDED.fingerprint, why=EXCLUDED.why""",
                            [(day, i.task_id, i.score, i.band, i.fingerprint, i.why) for i in items])])

    def load_previous_snapshot(self, before: date) -> dict[str, PrevSnapshot]:
        """Latest snapshot per task strictly before `before`."""
        return {r["task_id"]: PrevSnapshot(r["band"], r["fingerprint"]) for r in self._exec(SQL_PREV_SNAPSHOT, (before,))}

    # ----- risk events ---------------------------------------------------------------
    def recently_notified(self, task_ids: list[str]) -> set[tuple[str, str, int]]:
        """(task_id, new_risk, delay_days) already notified in the last hour: one alert per root cause per hour."""
        if not task_ids:
            return set()
        rows = self._exec("""SELECT DISTINCT task_id, new_risk, delay_days FROM risk_events
                             WHERE task_id = ANY(%s) AND notified_at > now() - interval '1 hour'""", (task_ids,))
        return {(r["task_id"], r["new_risk"], r["delay_days"]) for r in rows}

    def record_risk_events(self, rows: list[tuple[str, str | None, str | None, str, int, int | None]]) -> None:
        self._write_batch([("""INSERT INTO risk_events(task_id, cause_task_id, old_risk, new_risk, delay_days, slack_days)
                               VALUES (%s,%s,%s,%s,%s,%s)""", rows)])

    def mark_risk_notified(self, task_ids: list[str]) -> None:
        if task_ids:
            self._exec("UPDATE risk_events SET notified_at = now() WHERE task_id = ANY(%s) AND notified_at IS NULL",
                       (task_ids,))

    # ----- people --------------------------------------------------------------------
    def upsert_people(self, people: Iterable[Person]) -> None:
        people = list(people)
        self._write_batch([  # backup_id references people(id): insert first, link after
            ("""INSERT INTO people(id, name, aliases, is_lead, slack_user_id) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, aliases=EXCLUDED.aliases,
                is_lead=EXCLUDED.is_lead, slack_user_id=EXCLUDED.slack_user_id""",
             [(p.id, p.name, list(p.aliases), p.is_lead, p.slack_user_id) for p in people]),
            ("UPDATE people SET backup_id=%s WHERE id=%s", [(p.backup_id, p.id) for p in people]),
        ])

    def load_people(self) -> list[Person]:
        return _people(self._exec(SQL_PEOPLE))

    def ensure_people(self, people: list[dict[str, Any]]) -> None:
        """Insert or rename people from Notion without touching lead/backup/Slack settings."""
        self._write_batch([("""INSERT INTO people(id, name, email) VALUES (%(id)s,%(name)s,%(email)s)
                               ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,
                               email=COALESCE(EXCLUDED.email, people.email)""", people)])

    def set_slack_user(self, person_id: str, slack_user_id: str) -> None:
        self._exec("UPDATE people SET slack_user_id=%s WHERE id=%s", (slack_user_id, person_id))

    def add_slack_person(self, slack_user_id: str, name: str, email: str | None) -> None:
        self._exec("""INSERT INTO people(id, name, email, slack_user_id) VALUES (%s,%s,%s,%s)
                      ON CONFLICT (id) DO UPDATE SET slack_user_id=EXCLUDED.slack_user_id""",
                   (f"slack:{slack_user_id}", name, email, slack_user_id))

    # ----- follow-ups ----------------------------------------------------------------
    def insert_followups(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert new follow-ups; a dedupe_key that already exists is skipped. Returns the rows that were new."""
        if not rows:
            return []
        sql = """INSERT INTO followups(task_id, kind, step, dedupe_key, due_at, recipient_role, text, severity)
                 VALUES (%(entity_id)s,%(kind)s,%(step)s,%(dedupe_key)s,%(due_at)s,%(recipient_role)s,%(text)s,%(severity)s)
                 ON CONFLICT (dedupe_key) DO NOTHING RETURNING *"""
        return self._write_batch([(sql, rows)], fetch=True)

    def finish_followups(self, cancel_ids: list[int], sent_ids: list[int], why: str = "no_longer_applies") -> None:
        """Cancel obsolete follow-ups and mark delivered ones sent, atomically in one round trip."""
        stmts = []
        if cancel_ids:
            stmts.append(("UPDATE followups SET outcome=%s WHERE id = ANY(%s) AND sent_at IS NULL", [(why, cancel_ids)]))
        if sent_ids:
            stmts.append(("UPDATE followups SET sent_at=now() WHERE id = ANY(%s) AND sent_at IS NULL", [(sent_ids,)]))
        if stmts:
            self._write_batch(stmts)

    def pending_followups(self) -> list[dict[str, Any]]:
        return self._exec(SQL_PENDING)

    def mark_followups_sent(self, ids: list[int]) -> None:
        if ids:
            self._exec("UPDATE followups SET sent_at=now() WHERE id = ANY(%s) AND sent_at IS NULL", (ids,))

    def cancel_followups(self, ids: list[int], why: str = "cancelled") -> None:
        if ids:
            self._exec("UPDATE followups SET outcome=%s WHERE id = ANY(%s) AND sent_at IS NULL", (why, ids))

    def set_followup_outcome(self, task_id: str, outcome: str) -> None:
        self._exec("UPDATE followups SET outcome=%s WHERE task_id=%s AND sent_at IS NOT NULL AND outcome IS NULL",
                   (outcome, task_id))

    def unconfirmed_proposals(self) -> list[dict[str, Any]]:
        return self._exec(SQL_UNCONFIRMED)

    # ----- proposals (meeting tasks and date-change requests) ------------------------
    def save_proposals(self, meeting_id: str, proposals: list[dict[str, Any]]) -> None:
        self._write_batch([("""INSERT INTO proposals(meeting_id, local_id, payload) VALUES (%s,%s,%s)
                               ON CONFLICT (meeting_id, local_id) DO NOTHING""",
                            [(meeting_id, p["local_id"], Jsonb(p)) for p in proposals])])

    def record_extraction(self, meeting_id: str, proposals: list[dict[str, Any]]) -> None:
        """Proposals + the 'meeting.extracted' marker, atomically, one round trip."""
        self._write_batch([
            ("""INSERT INTO proposals(meeting_id, local_id, payload) VALUES (%s,%s,%s)
                ON CONFLICT (meeting_id, local_id) DO NOTHING""",
             [(meeting_id, p["local_id"], Jsonb(p)) for p in proposals]),
            ("INSERT INTO events(kind, entity, payload, actor) VALUES (%s,%s,%s,%s)",
             [("meeting.extracted", meeting_id, Jsonb({"proposals": len(proposals)}), "extractor")]),
        ])

    def load_extraction_state(self):
        g, ppl, done = self.read_all([(SQL_GOALS, None), (SQL_PEOPLE, None),
                                      ("SELECT entity FROM events WHERE kind='meeting.extracted'", None)])
        goals = [Goal(r["id"], r["name"], r["weight"], r["target_date"]) for r in g]
        return goals, _people(ppl), {r["entity"] for r in done}

    def get_proposals(self, meeting_id: str) -> list[dict[str, Any]]:
        return self._exec("SELECT * FROM proposals WHERE meeting_id=%s ORDER BY local_id", (meeting_id,))

    def get_proposal(self, meeting_id: str, local_id: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT * FROM proposals WHERE meeting_id=%s AND local_id=%s", (meeting_id, local_id))
        return rows[0] if rows else None

    def decide_proposal(self, meeting_id: str, local_id: str, status: str, by: str | None,
                        payload: dict[str, Any] | None = None) -> bool:
        """Move a Proposed row to a decision exactly once (False if it was already decided)."""
        rows = self._exec("""UPDATE proposals SET status=%s, decided_at=now(), decided_by=%s,
                             payload=COALESCE(%s, payload) WHERE meeting_id=%s AND local_id=%s AND status='Proposed'
                             RETURNING 1""", (status, by, Jsonb(payload) if payload is not None else None,
                                              meeting_id, local_id))
        return bool(rows)

    # ----- notion write-back bookkeeping ---------------------------------------------
    def load_notion_writes(self) -> dict[str, tuple[str, datetime]]:
        return {r["page_id"]: (r["payload_hash"], r["written_at"]) for r in self._exec(SQL_WRITES)}

    def record_notion_writes(self, rows: list[tuple[str, str, datetime]]) -> None:
        self._write_batch([("""INSERT INTO notion_writes(page_id, payload_hash, written_at) VALUES (%s,%s,%s)
                               ON CONFLICT (page_id) DO UPDATE SET payload_hash=EXCLUDED.payload_hash,
                               written_at=EXCLUDED.written_at""", rows)])

    def record_notion_write(self, page_id: str, payload_hash: str, written_at: datetime) -> None:
        self.record_notion_writes([(page_id, payload_hash, written_at)])


class PgRunLog:
    """Implements the gateway's RunLog protocol; one row per model attempt (Hub doc 3.6)."""

    _COLS = ("job_type", "trigger", "input_ref", "output_ref", "model", "tier", "attempt", "tokens_in",
             "tokens_out", "cost_usd", "latency_ms", "config_version", "ok", "error_code", "error")

    def __init__(self, db: Database):
        self.db = db

    def write(self, record: dict[str, Any]) -> None:
        self.db._exec(f"INSERT INTO runs({', '.join(self._COLS)}) VALUES ({', '.join(['%s'] * len(self._COLS))})",
                      [record.get(c) for c in self._COLS])
