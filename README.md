# Ops Brain

Turns meeting decisions into owned, dated, dependency-linked work in Notion, ranks that work against company goals, and warns the right people in Slack the moment something slips, before it breaks a deadline.

- **Notion** is the task system and the interface. No custom front-end.
- **Slack** is how people hear about things and act on them (buttons, `/ops` commands).
- **One small service** does the thinking. Dates, rankings and risk are plain, tested code. A language model is used only to read meeting notes.
- **The AI proposes, a person confirms.** Nothing is created or changed without a click.

Built for PRD 1 (`../ops-brain.md`) on the shared platform in the design doc (`../Publishing & Insights Hub System Design Doc v0.2.md`, section 3).

> **Status:** the engines, Notion sync, Slack delivery and API are built and covered by 114 automated tests. It is **not hosted anywhere yet**: it runs only while you start it. See [Known limits](#known-limits).

## What it does

1. **Meeting note → proposed tasks.** A note (English, Hindi, Gujarati, mixed) becomes proposals with owner, due date, goal and dependencies. Anything uncertain is flagged ("owner?", "please confirm the date"), never guessed.
2. **Approval in Slack.** A person approves or rejects each proposal; approved ones become Notion tasks.
3. **Slip detection.** When a task's due date moves, the dependency graph is recomputed. Every downstream owner gets one Slack message naming the cause and the goal at risk.
4. **Priorities with a reason.** Each task gets a band (P0–P3) and a plain-language "why". Bands only change when something real changes.
5. **Follow-ups.** Due-soon, overdue (with an escalation ladder) and stale nudges, only inside working hours (default Mon–Sat 09:30–18:30 IST).
6. **Goal health.** Each goal is On track / At risk / Off track, with the reason, written back to Notion.

## How it works

```
 Notion (Tasks · Projects/goals · Meeting Notes)         Slack (DMs · alerts channel · buttons · /ops)
        ▲ │  poll every 5 min, write back                        ▲ │  Socket Mode (outbound only)
        │ ▼                                                      │ ▼
   ┌──────────────────────────── CORE SERVICE (one process) ─────────────────────────────┐
   │ FastAPI · Procrastinate job worker · Slack Bolt                                      │
   │ notion sync → dependency graph → priority engine → slip alerts → Slack               │
   │ task extractor (LLM) → proposals → approval gate     follow-up scheduler · sweeper   │
   └───────────────┬──────────────────────────────────────────────┬───────────────────────┘
                   ▼                                              ▼
        Postgres (Neon): cache + audit log            LiteLLM gateway → hosted model API (Groq)
        + job queue                                   open-weight models; none self-hosted
```

Design rules that matter when you change the code:

- **Notion is the truth; Postgres is a cache plus a log.** The cache is rebuilt from Notion on every sync.
- **Only the "Ops" columns are written automatically** (Ops priority, Rank reason, Risk, Projected finish, Health, Health reason). Dates, owners, status and the existing Priority column change only when a person clicks (Done) or a lead approves a new date.
- **Alerts survive crashes.** An alert is written to `alert_outbox` in the *same atomic write* as the change that caused it. A sweeper (every minute) sends anything unsent, once. Failed sends retry with growing back-off for up to 24 hours and arrive marked "delayed".
- **Everything is idempotent.** Unique keys (`followups.dedupe_key`, `inbox.event_id`, `proposals(meeting_id, local_id)`) and a Postgres advisory lock stop duplicates.
- **Writes are batched.** Neon is far away, so related reads and writes are pipelined into one round trip (see `core/db.py`). Don't add per-row queries in loops.
- **Every model call goes through `core/gateway.py`** (LiteLLM): output is schema-checked, repaired once, fails over to the next model, then escalates to a larger one. Self-hosted model names (ollama, vllm) are rejected at load.

## Quick start

**You need:** Python 3.12, a [Neon](https://neon.tech) Postgres project (free plan is fine), a Notion workspace and an integration/personal token, a Slack workspace, and a [Groq](https://groq.com) API key.

### 1. Install

```bash
cd ops-brain
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # add requirements-dev.txt for tests and the PDF script
```

### 2. Configure

```bash
cp .env.example .env                    # then fill it in; never commit .env
```

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon **pooled** URL, used for app queries |
| `DATABASE_URL_UNPOOLED` | Neon **direct** URL, used by migrations and the job queue (needs real sessions) |
| `TEST_DATABASE_URL`, `TEST_DATABASE_URL_UNPOOLED` | A **throwaway** Neon branch for tests. See [Testing](#testing). |
| `GROQ_API_KEY` | Model API key (any LiteLLM-supported hosted provider works; edit `config/routing.yaml`) |
| `NOTION_PAT` | Notion token |
| `NOTION_TASKS_DS`, `NOTION_PROJECTS_DS`, `NOTION_MEETINGS_DS` | Notion **data-source ids** of your three databases (the defaults belong to the original workspace) |
| `SLACK_OAUTH_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_ALERTS_CHANNEL` | Bot token, app-level token, alerts channel id |
| `ADMIN_TOKEN` | Bearer token for `/admin/*`. Unset means the admin API is disabled. |
| `NOTION_WEBHOOK_SECRET` | Only if you run a public webhook; unset means the webhook refuses everything |
| `RUN_WORKER`, `RUN_SLACK` | Set to `false` to stop the job worker / Slack connection starting inside the API process |
| `TIMEZONE`, `WORK_START`, `WORK_END` | Working hours for follow-ups (default Asia/Kolkata, 09:30–18:30, Mon–Sat) |

### 3. Notion

Three databases. The sync reads these property names **exactly** (they are fixed in `core/notion/mapping.py`).

| Database | Properties you fill in | Properties Ops Brain writes back |
| --- | --- | --- |
| **Tasks Tracker** | `Task name`, `Due date`, `Status` (**Not started, In progress, Blocked, Done**), `Assignee`, `Owner (name)` (text), `Estimate (days)` (number), `Goal` (relation → Projects), `Depends on` (relation → Tasks Tracker) | `Ops priority` (select P0–P3), `Rank reason` (text), `Risk` (select: On track, At risk, Off track, Unknown), `Projected finish` (date) |
| **Projects** (= goals) | `Name`, `Weight` (number 1–5), `Target date` | `Health` (select: On track, At risk, Off track), `Health reason` (text) |
| **Meeting Notes** | `Meeting name`, `Date`; the note text is the page body | none (read-only) |

A task's owner is the `Assignee` if set, otherwise the text in `Owner (name)`. Share all three databases with your integration.

### 4. Slack

Create an app **From a manifest** using [`slack-manifest.yaml`](slack-manifest.yaml) (Socket Mode, so no public URL). Install it, copy the **Bot User OAuth Token** and a **App-Level Token** with `connections:write`, then create the alerts channel and `/invite @Ops Brain`.

### 5. Database

```bash
alembic upgrade head                    # application tables (uses DATABASE_URL_UNPOOLED)
python scripts/init_queue.py            # the job queue's own tables (safe to re-run)
```

### 6. Run

```bash
uvicorn core.app:create_app --factory --host 127.0.0.1 --port 8077
```

One process runs the API, the job worker (scheduled jobs below) and the Slack connection. Run **one** instance.

```bash
curl localhost:8077/readyz                                              # {"status":"ready"}
python scripts/set_lead.py "Your Name"                                  # links Slack users, marks you a lead (approver)
curl -X POST localhost:8077/admin/slack/test -H "Authorization: Bearer $ADMIN_TOKEN"   # posts to the alerts channel
```

Until someone is marked a lead, any linked person can approve (a bootstrap rule). Once a lead exists, only leads approve date changes.

## Using it

**In Slack**

| You see | You can |
| --- | --- |
| A follow-up or slip alert | **Done** (writes Status = Done to Notion), **On track**, **Slipping** (opens a date picker; creates a *request*, and Notion changes only when a lead approves) |
| A meeting approval card | **Approve** / **Reject** each proposal, or **Approve all** / **Reject all**. Approving creates the Notion tasks. |
| `/ops plan` | Your top priorities |
| `/ops why <task>` | Why a task ranks where it does, its risk and slack |

**Scheduled jobs** (inside the service)

| Every | Job |
| --- | --- |
| 5 min | `reconcile_poll`: sync Notion, alert on slips, write back Ops columns, extract new meeting notes (last 14 days), send due follow-ups |
| 1 min | `sweep_outbox`: resend any alert or approval card that was saved but not delivered |
| 15 min | `followup_tick` |
| daily 09:00 IST | `daily_plan_cron`: priority snapshot |

**HTTP** (all `/admin/*` need `Authorization: Bearer $ADMIN_TOKEN`)

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz`, `GET /readyz` | Liveness; readiness checks the database and queue |
| `POST /hooks/notion` | Notion webhook (verifies the signature, de-duplicates, queues). Optional; the poll does not need it. |
| `POST /admin/poll` | Run one poll cycle now |
| `POST /admin/sync` | Sync Notion only |
| `GET /admin/goals`, `GET /admin/tasks/{id}/why` | Goal health; why a task ranks where it does |
| `GET /admin/outbox`, `POST /admin/sweep` | Alert queue health; resend now |
| `POST /admin/followup-tick`, `POST /admin/snapshot` | Run follow-ups; load tasks directly (testing without Notion) |
| `POST /admin/slack/test`, `POST /admin/slack/link` | Post a test message; (re)link Slack users to people |

## Configuration beyond `.env`

- **`config/routing.yaml`**: which model serves which job, by tier, with fail-over. Model ids are verified against the provider; pick final models by evaluating on real notes.
- **`config/prompts/extract_tasks.md`**: the extraction prompt (the meeting note is treated as data, never as instructions).
- **Thresholds** are plain constants: slack buffer and stale/blocked penalties in `core/graph/engine.py` (`RiskParams`), band thresholds in `core/priority/engine.py` (`PriorityParams`), the escalation ladder in `core/followups/rules.py` (`FollowUpConfig`).

## Project layout

```
core/
  app.py            FastAPI app: health, webhook, admin; starts the worker and Slack
  jobs.py, queue.py Procrastinate jobs and app
  poll.py           one poll cycle (sync, extract, follow-ups, sweep)
  services.py       slip alerts, outbox delivery and sweeper, follow-up tick, change preview
  db.py             all SQL (batched, pipelined)
  graph/            dependency graph: projected finish, slack, risk, goal health
  priority/         scoring, stable bands, "why" text
  followups/        working hours and follow-up rules
  extractor/        meeting note → proposals (LLM, then deterministic validation)
  notion/           REST client, property mapping, sync
  slack/            Block Kit layouts, button logic, Bolt app, notifier, user linking
  gateway.py        the single path to every model (LiteLLM)
config/             model routing and prompts
migrations/         Alembic (0001 core, 0002 follow-ups, 0003 notion sync, 0004 alert outbox)
scripts/            demo.py, set_lead.py, init_queue.py, bench_db.py, make_build_plan_pdf.py
demo/               sample meeting note and a recorded model reply
docs/               build plan (PDF), Loom script, core-service plan
tests/              114 tests
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q                                # about 2.5 minutes: every query is a network call to Neon
```

- Tests need `TEST_DATABASE_URL` (and `_UNPOOLED`) pointing at a **disposable Neon branch**, e.g. `neon checkout test --create`.
- **The test fixture drops and recreates the `public` schema on that database.** It refuses to run if the URL equals `DATABASE_URL`, but never point it at anything you care about.
- Slack and Notion are faked; Bolt's routing is tested for real with only the network call stubbed. The model gateway is tested with a fake completion function.
- `python scripts/bench_db.py` times the database-heavy operations (a slip is about 0.7 s against Neon).

## Demo

`scripts/demo.py` seeds a goal and a 4-task dependency chain in your Notion, then drives the flow (`meeting`, `poll`, `slip`, `reset`, `cleanup`). Run it with no arguments for the list. `seed` is safe to re-run and repairs whatever is missing.

- **Recording script:** [`docs/loom-script.md`](docs/loom-script.md) has the full runbook, the manual-seeding table and troubleshooting.
- **Extraction with no network or keys:** `python demo/run_demo.py --mock` turns the sample mixed-language note into structured proposals (drop `--mock` to use the live model).

## When things go wrong

| Failure | What happens |
| --- | --- |
| A Notion change is missed | The 5-minute poll re-reads everything |
| Same event or click twice | Unique keys make it a no-op; one alert per cause per hour |
| Model API down or bad JSON | Schema check, one repair, next provider, bigger model; jobs retry with back-off |
| Bad Notion data | Dependency cycles are rejected with a plain alert; odd values become warnings; unknown owners/goals are flagged |
| Slack can't DM someone | Goes to the alerts channel |
| Slack is down | Alerts wait in the outbox, retry with back-off, arrive marked "delayed"; after 24 h they are dropped as stale (visible at `/admin/outbox`) |
| A Notion write fails mid-approval | The approval is reopened, so nothing is half-applied |

## Known limits

- **Not hosted.** It needs an always-on host (one instance). Notion webhooks need a public HTTPS address, so the 5-minute poll is used instead; a change can take up to 5 minutes to be noticed.
- **Slip alerts ignore working hours** (they go out immediately); only follow-ups respect them.
- **Notion property names are fixed** in code. A personal token can't list workspace users, so people are matched to Slack by email, then exact name. An owner's name must match their Slack name.
- **The 80% "approved without edits" target is unmeasured.** It needs a labelled set of real notes. Extraction was checked on one sample note.
- **Free model tiers may log prompts.** Keep real founder content off them until you use a paid, zero-retention provider (a `config/routing.yaml` edit).
- **No resend after 24 hours,** and nobody is paged if the outbox grows; check `/admin/outbox`.

## More

- [`docs/ops-brain-build-plan.pdf`](docs/ops-brain-build-plan.pdf): the build plan (v1 and why, architecture, data model, buy vs build, failure handling, cost)
- [`docs/plan-core-service.md`](docs/plan-core-service.md): the plan for the Postgres, queue, follow-up and API layer
- [`docs/loom-script.md`](docs/loom-script.md): the recording script and runbook
