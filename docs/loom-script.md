# Loom script: Ops Brain (max 5 minutes)

Target: **4:40 spoken + on-screen waits**. About 520 words of narration; the rest is the demo running.
The take-home asks for the plan, a working demo (meeting note to structured tasks) and your reasoning. This covers all three.

---

## Before you hit record (10 minutes, do it once, then again after any retake)

**Start the service** (leave this terminal visible but small):
```
cd ops-brain
.venv/bin/uvicorn core.app:create_app --factory --host 127.0.0.1 --port 8077
```
**Check the starting state** (every demo task should say `ok`, goal `on_track`):
```
.venv/bin/python scripts/demo.py status
```
**Windows to have open, in this order:**
1. The PDF: `docs/ops-brain-build-plan.pdf`
2. Notion: the **Tasks Tracker** (sort by Due date) and the **Projects** table (find "[DEMO] Launch partner program")
3. Slack: the alerts channel, and your DM with Ops Brain
4. A terminal, in `ops-brain`

**Clean up Slack first.** My verification runs left 4 alert messages (3 in the channel, 1 in your DM with Ops Brain) and an old approval card in the channel. Delete them so the recording starts empty.

**Rehearse once, fully, including the Approve click.** The approval step (it creates real tasks in Notion) is the one piece I could not run for you; it is tested with a fake Notion, not live. Rehearse it, then run `python scripts/demo.py reset` and go again.

**Is the Tasks Tracker empty?** Seed it first (see "Seeding the demo data" below).

**Do not run `meeting` before you are on camera.** The service polls Notion every 5 minutes and would pick the note up early.

---

## The script

### 0:00 – 0:25 · The problem (face or PDF page, no demo yet)
> "Decisions get made in meetings and then live in people's heads. A goal depends on a task due in ten days, which depends on one due in thirty. One silent slip early in that chain breaks the goal, and nobody notices until it's too late. Ops Brain turns meeting notes into owned, dated, linked tasks, ranks them against goals, and tells the right people the moment something slips."

### 0:25 – 1:05 · What I'd ship first, and why (show the PDF, top half and diagram)
> "My v1 is one loop: a meeting note becomes proposed tasks, a person approves them in Slack, they land in Notion, and when any date slips, every downstream owner gets an alert naming the cause and the goal at risk.
> I chose it because it's the trial's Done test, a slip that reaches everyone within fifteen minutes, and it builds the spine everything else reuses: the Notion sync, the dependency graph, the approval gate, Slack delivery.
> Architecture in one breath: Notion is the system of record and the interface, Slack is how people hear and act, and one small service in the middle does the thinking. The only part that uses an AI model is reading the meeting note. Dates, rankings and risk are plain, tested code."

### 1:05 – 2:20 · Demo 1: meeting note to tasks
**Terminal:** `python scripts/demo.py meeting`  → then open the new page in Notion (Meeting Notes, "[DEMO] Partner onboarding kickoff (take 1)").
> "Here's a kickoff note, the way our meetings really sound: English, Hindi and Gujarati mixed together."

*(scroll the note slowly, 5 seconds)*

**Terminal:** `python scripts/demo.py poll`   *(takes about 12 seconds; talk over it or cut it)*
> "The system reads it, and proposes tasks. Nothing is created yet."

**Switch to Slack: the proposals card.** Point at three things:
- the owners and dates it picked up correctly from Hindi and Gujarati
- **"please confirm the date (next Friday)"**: it won't guess a vague date
- the FAQ page flagged **"owner? no due date"**: it won't invent an owner

> "It flags what it isn't sure about instead of guessing. I'll reject the one nobody owns, and approve the rest."

**Click** Reject on the FAQ task, then **Approve all**. Wait about 10 seconds (the system syncs straight after an approval), then switch to Notion and refresh Tasks Tracker.
> "And they're in Notion, with owners, due dates, the goal link, and the email template depends on the guide."

### 2:20 – 3:50 · Demo 2: a slip reaches everyone (the Done test)
**Notion:** show the four-task chain for **"[DEMO] Launch partner program"** (API spec → backend → QA → launch page). Point at the **Risk** column: all *On track*.
> "Four tasks in a chain, all healthy. Rahul owns the API spec and tells me it's going to slip by four weeks."

**In Notion (on camera):** open *Finalise partner API spec* and change its **Due date** to **today + 36 days** (28 days later). Click away so it saves.
**Terminal:** `python scripts/demo.py poll`   *(reads Notion only; it does not edit anything. About 15 seconds: talk over it, or cut it)*
> Without that command the system finds the change by itself within 5 minutes. The command just skips the wait.

*(Prefer not to type it by hand? `python scripts/demo.py slip` makes the same date change for you, then polls.)*
**Slack:** show your DM and the channel messages.
> "Within seconds, everyone downstream is told. Meera's backend, Priya's QA, and my launch page, each with the cause, the chain it breaks, and the goal at risk. Notice it says nothing was changed automatically; it only informs."

*(open one alert: read "Because: API spec is now expected 25 Dec. Puts at risk: … → Goal").*
**Notion:** show **Risk = Off track**, **Projected finish**, **Rank reason**, and the Project's **Health = Off track**.
> "The same answer is written back to Notion, so founders see goal health without asking anyone."

### 3:50 – 4:15 · Priorities, and why
**Slack:** type `/ops plan`, then `/ops why launch partner`.
> "Anyone can ask what matters today, and why a task ranks where it does. The ranking is deterministic and stable: it only changes when something real changes."

### 4:15 – 5:00 · Decisions, failure handling, cost, what's next (PDF, bottom half)
> "On buy versus build: I bought Notion, Slack, Postgres and hosted open-weight models, and built only the small core that Notion can't do: chain slack, propose-then-confirm, and stable rankings.
> Failures: a five-minute poll backs up every webhook, writes are idempotent, a failed Slack message falls back to the channel, and the AI never changes a date, owner or priority on its own. A person confirms. And an alert is saved in the same atomic write as the slip it describes, so even a crash can't lose it: a sweeper sends anything unsent within a couple of minutes.
> Cost: about zero for an MVP and an estimated ten to thirty dollars a month in production. Those are estimates, not measurements.
> Next: host it always-on next to the database, turn on Notion webhooks, and measure real approval rates on real meetings."

---

## Seeding the demo data

You deleted the Tasks Tracker rows, so the chain has to come back. **Pick one route per take. Don't mix them.**

### Route A: one command (about 20 seconds)
```
python scripts/demo.py seed
```
It creates only what is missing (the goal and the four tasks, with their dependency links), then syncs. It is safe to run again. Use `python scripts/demo.py reset` between takes.

### Route B: type it in Notion by hand (if you want to build it on camera)
First run `rm demo/state.json` so the script never recreates tasks next to yours. Then, in this order:

**1. Projects:** add a row
| Name | Weight | Target date |
| --- | --- | --- |
| [DEMO] Launch partner program | 5 | today + 52 days |

**2. Tasks Tracker:** add four rows, oldest first. Leave **Assignee empty** and type the name in **Owner (name)** (your Notion name is "Sufail Salim", which would not match Slack's "Sufail", so the launch-page alert would miss your DM).

| Task name | Owner (name) | Due date | Estimate (days) | Status | Goal | Depends on |
| --- | --- | --- | --- | --- | --- | --- |
| Finalise partner API spec | Rahul | today + 8 | 3 | In progress | [DEMO] Launch partner program | (none) |
| Build partner backend | Meera | today + 26 | 5 | Not started | same goal | Finalise partner API spec |
| QA pass on the backend | Priya | today + 33 | 4 | Not started | same goal | Build partner backend |
| Launch partner page | **Sufail** | today + 40 | 2 | Not started | same goal | QA pass on the backend |

Worked example for 19 Nov 2026: due **27 Nov, 15 Dec, 22 Dec, 29 Dec**; goal target **10 Jan 2027**. Keep the gaps between dates. If they are tighter, a task will start out "At risk" instead of "On track".

**3. Sync:** wait up to 5 minutes, or run `python scripts/demo.py poll`.
**4. Check:** the Risk column fills in **On track** for all four, and the project's Health says **On track**. If not, a date is too tight.

**The slip, by hand:** this is now the default in the script above. (`demo.py slip` only works after Route A.)

---

## What you can claim, and what you can't

| Safe to say | Don't say |
| --- | --- |
| Extraction handled a mixed English/Hindi/Gujarati note and flagged vague dates and missing owners (verified live on this exact demo note) | "It's running in production" or "always on". It runs only while you start it. |
| A slip alerted all four owners in about 16 s including the Notion round trips (measured live) | That Notion webhooks are on (they aren't; it's the 5-minute poll, which you trigger manually for the demo) |
| Priorities, risk and goal health are written back to Notion | That the Slack buttons and `/ops` were exercised by a person before you: rehearse them |
| 111 automated tests pass | Any hard accuracy number (the 80% target needs a labelled set of notes, not built yet) |
| An alert can't be lost to a crash: it is saved with the slip, and a sweeper resends it. Tested with a simulated crash, and live: a saved-but-unsent test alert reached Slack via the sweeper in about 100 s | "Alerts can never fail": after 10 failed tries an alert gives up and shows only in the admin view; nobody is paged |
| Cost is an estimate | A precise monthly cost |

## If something goes wrong on camera

| Symptom | Fix |
| --- | --- |
| No Slack card after `poll` | The poll prints what it did; if `proposals: 0`, the note was already processed. Run `python scripts/demo.py meeting` again (a new take) then `poll`. |
| `poll` says "another sync is running" | Wait 10 seconds and run it again. |
| Slack says the app didn't respond | The service isn't running. Start it (see the top). |
| Approve / Approve all does nothing | Either the service isn't running, or you clicked an **old card**. `reset` deletes the proposals behind earlier cards, so those buttons are dead for good. Always click the newest card, after the service is up. |
| Alerts didn't fire on `slip` | Run `python scripts/demo.py reset` and retry; identical alerts are suppressed for an hour, and reset clears that. |
| Extraction fails live | Show the saved example instead: `python demo/run_demo.py --mock` prints the same kind of table (no network). |
| Need another take | `python scripts/demo.py reset` (about 20 seconds), then start from "Before you hit record". |

## When you're done
`python scripts/demo.py cleanup` removes every demo page from Notion and the demo people. Then stop the service (Ctrl+C).
