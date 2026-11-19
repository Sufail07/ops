# Plan: Postgres, job queue, follow-up scheduler, FastAPI service

Builds on the engines already in `core/` (graph, priority, alerts, gateway, extractor). Goal: one container that stays up, receives events, runs jobs on time, and survives restarts, with no Notion/Slack dependency needed for the first milestone.

## Decisions

| Area | Choice | Why |
| --- | --- | --- |
| DB driver, migrations | psycopg 3 (async pool) + Alembic | Procrastinate uses psycopg 3; Alembic is in the Hub doc (3.9) |
| Database | Neon (project `dry-boat-76773483`): `production` branch for the app, short-lived `test` branch for tests | Hub doc 3.11; Docker Postgres dropped at the user's request |
| Connections | Pooled `DATABASE_URL` for app queries (prepared statements off, pgbouncer-safe); `DATABASE_URL_UNPOOLED` for Alembic and the Procrastinate queue | DDL and LISTEN/NOTIFY need real sessions |
| Queue | Procrastinate (Postgres-backed, periodic tasks, delayed jobs, locks) | No Redis; delayed jobs fit reminders |
| Process model | One container: FastAPI + Procrastinate worker started in the app lifespan | Hub doc "one container"; split into two processes later if needed |
| Time | Store UTC, compute in IST via config | Working hours are IST |

## 1. Postgres (migration 0001)

Tables (all with `created_at`; ids are UUID or the Notion page id as text):
- `tasks`, `edges`, `goals`, `people` (name, aliases, slack_user_id, notion_user_id, is_lead, backup_id): cache of Notion, rebuilt by sync.
- `events` (append-only: id, ts, kind, entity, payload, actor): every state change. Enforce append-only with a trigger that blocks UPDATE/DELETE.
- `runs`: the Hub doc 3.6 fields. Replace `JsonlRunLog` with `PgRunLog` behind the existing `RunLog` protocol.
- `priority_snapshots`, `risk_events`, `proposals`, `followups` (see below).
- `inbox(event_id unique, source, received_at, processed_at)`: webhook idempotency; a duplicate event id is a no-op.

Access layer: small `core/db.py` (pool, `fetch_graph()` returning `TaskGraph` inputs, `save_snapshot()`); no ORM.

## 2. Job queue (Procrastinate)

| Job | Trigger | Does |
| --- | --- | --- |
| `sync_graph` | Notion event or `reconcile_poll` | Pull changed pages, upsert tasks/edges, enqueue `recompute_risk` |
| `recompute_risk(scope)` | After sync | Analyze before vs after, `slip_impacts`, write `risk_events`, enqueue alerts. Queue lock per goal so recomputes never race |
| `send_alert(alert_id)` | After recompute | Deliver through a `Notifier` interface (Slack later, a console/log notifier now) |
| `extract_meeting(meeting_id)` | New meeting note | Gateway call, store `proposals` |
| `daily_plan` | Cron 09:00 IST | `prioritise()` with the previous snapshot, store, notify |
| `forward_projection` | Cron daily | Flags predicted slips 3+ days ahead |
| `followup_tick` | Cron every 15 min | Section 3 |
| `reconcile_poll` | Cron every 5 min | Catch missed Notion events by last-edited time |

Rules: every job is idempotent; retries use exponential backoff (max 5); a failed job that exhausts retries writes an `events` row and raises a plain-language alert. Dedupe alerts: one per root cause per hour with a count (Hub doc 3.7).

## 3. Follow-up scheduler

Split into pure logic (testable now) and delivery.
- `core/followups/rules.py`: `due_followups(graph, analysis, now, cfg, sent) -> list[FollowUp]`. Rules from config: due in 2 days, due today, overdue, stale (no update for N days), proposal unconfirmed after 24 h, slipped-task "please confirm a new date". Each carries a `dedupe_key` (task, kind, step, due date).
- `core/followups/hours.py`: `next_send_time(now, cfg)`: working window (default IST Mon to Sat 09:30 to 18:30, holidays list); items outside the window are queued, except a critical slip that goes to the next window start, never dropped.
- Escalation ladder (config): owner, then owner again after 1 working day, then the lead, then the backup. Stops the moment the task is updated or done. Never changes a date, owner or priority; "Slipping" replies create a proposal for a person to confirm.
- `followups` table has a unique index on `dedupe_key`, so a repeated tick cannot double-send.
- `followup_tick` loads the graph, calls the rules, inserts rows (conflict = skip), sends those whose send time has arrived.

## 4. FastAPI service

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz`, `GET /readyz` | Liveness; readiness checks DB and queue |
| `POST /hooks/notion` | Verify signature, insert into `inbox` (dedupe), enqueue `sync_graph`, return 200 fast |
| `POST /hooks/slack` | Later: buttons and slash commands; signature verified |
| `POST /handoff` | Later (Hub) |
| `GET /admin/tasks/{id}/why`, `GET /admin/goals` | Read-only rank explanation and goal health; bearer token |
| `POST /admin/replay` | Re-run recompute for a goal (support tool) |

Structure: `core/app.py` (lifespan: pool, Procrastinate app and worker, config load, Sentry optional), `core/api/*.py` routers, `core/jobs.py`, `core/settings.py` (pydantic-settings, reads `.env`), a `Dockerfile` for the app. Structured JSON logs; secrets never logged.

## 5. Build order (each step ends green on tests)

1. Settings, Neon (production + test branches), Alembic 0001, `db.py`, `PgRunLog`; integration tests against the Neon test branch. DONE.
2. Procrastinate wiring, `recompute_risk` end to end on seeded DB data with a console `Notifier`; test: the 4-task chain slip creates the right `risk_events` and alerts.
3. Follow-up rules and working-hours logic (pure unit tests), then `followup_tick` with dedupe.
4. FastAPI app, health and webhook endpoints, signature check, inbox idempotency; run the worker in the lifespan.
5. Dockerfile; a script that simulates the Done test against Neon and prints the slip-to-alert time.

## 6. Tests

- Unit: working-hours edges (Sunday, after 18:30, holiday), ladder progression, dedupe keys.
- Integration (Neon test branch): migration up/down, append-only trigger, duplicate webhook is a no-op, restart mid-job resumes, two ticks do not double-send.
- Timing: slip edit to alert recorded under 6 minutes with webhook, under 15 with poll only.

## 7. Risks and open questions

- Neon round trips are slow from here (slip-to-alert took 18 s in the smoke test, mostly sequential queries): batch writes and cut per-statement connections before the 15-minute timing test. The test branch auto-expires after 7 days: recreate with `neon checkout test --create`.
- Procrastinate periodic tasks need exactly one worker; keep one instance (Hub doc: one always-on instance).
- Holiday calendar and lead/backup assignments: needed from you (config).
- Need from you: the Notion webhook secret and Slack tokens when we reach those steps.
