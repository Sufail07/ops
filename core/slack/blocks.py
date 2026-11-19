"""Slack Block Kit layouts. Pure functions: no Slack calls, easy to test.

Rules baked in (Slack limits): <= 50 blocks per message, section text <= 3000 chars, button text <= 75,
action_id <= 255 and unique within a message. Every button carries the id it acts on in its action_id.
"""
from __future__ import annotations

from typing import Any

from core.alerts.format import Alert

Block = dict[str, Any]
SEVERITY_ICON = {"off_track": "🔴", "at_risk": "🟠", "info": "🔔"}
ACTION_SLUG = {"Done": "done", "On track": "on_track", "Slipping": "slipping"}
MAX_ITEMS = 8
MAX_PROPOSALS = 12


def esc(s: str) -> str:
    """Escape Slack control characters in user-supplied text."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def section(text: str, block_id: str | None = None) -> Block:
    b: Block = {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
    if block_id:
        b["block_id"] = block_id[:255]
    return b


def context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def button(label: str, action_id: str, value: str, style: str | None = None) -> Block:
    b: Block = {"type": "button", "text": {"type": "plain_text", "text": label[:75]},
                "action_id": action_id[:255], "value": value[:1900]}
    if style:
        b["style"] = style
    return b


def task_actions_block(task_id: str, actions: tuple[str, ...]) -> Block:
    els = [button(a, f"ops_{ACTION_SLUG[a]}:{task_id}", task_id, "primary" if a == "Done" else None)
           for a in actions if a in ACTION_SLUG]
    return {"type": "actions", "block_id": f"act_{task_id}"[:255], "elements": els}


def alert_blocks(alert: Alert) -> list[Block]:
    head, _, rest = alert.text.partition("\n")
    blocks: list[Block] = [section(f"{SEVERITY_ICON.get(alert.severity, '🔔')} *{esc(head)}*")]
    if alert.items:
        for it in alert.items[:MAX_ITEMS]:
            blocks.append(section(esc(it.text), f"item_{it.task_id}"))
            if it.actions:
                blocks.append(task_actions_block(it.task_id, it.actions))
        if len(alert.items) > MAX_ITEMS:
            blocks.append(context(f"…and {len(alert.items) - MAX_ITEMS} more. Use `/ops plan` to see everything."))
    elif rest:
        blocks.append(section(esc(rest)))
    if alert.kind == "slip":
        blocks.append(context("Nothing was changed automatically. Tap *Slipping* to propose a new date for your task."))
    return blocks


def replace_task_actions(blocks: list[Block], task_id: str, note: str) -> list[Block]:
    """After someone clicks a button, swap that task's buttons for a one-line result."""
    out: list[Block] = []
    for b in blocks:
        if b.get("type") == "actions" and b.get("block_id") == f"act_{task_id}":
            out.append(context(note))
        else:
            out.append(b)
    return out


# ----- meeting proposals -----------------------------------------------------------------
def _fmt_due(p: dict) -> str:
    return p.get("due") or (f"{p['due_text']} (unclear)" if p.get("due_text") else "no date")


def proposals_card(meeting_id: str, title: str, rows: list[dict], goal_names: dict[str, str]) -> list[Block]:
    """rows: proposals table rows (status + payload). Decided rows show their result instead of buttons."""
    pending = [r for r in rows if r["status"] == "Proposed"]
    blocks: list[Block] = [section(f"📝 *{len(rows)} task(s) proposed from “{esc(title)}”*\n"
                                   "_Nothing is created until you approve._")]
    for r in rows[:MAX_PROPOSALS]:
        p = r["payload"]
        flags = p.get("needs_attention") or []
        line = (f"*{esc(p['title'])}*\nOwner: {esc(p.get('owner') or p.get('owner_spoken') or '?')} · "
                f"Due: {esc(_fmt_due(p))} · Goal: {esc(goal_names.get(p.get('goal_id') or '', 'none'))}")
        if flags:
            line += "\n⚠️ _Check: " + esc("; ".join(flags)) + "_"
        if p.get("evidence_quote"):
            line += "\n> " + esc(p["evidence_quote"][:200])
        blocks.append(section(line))
        if r["status"] == "Proposed":
            blocks.append({"type": "actions", "block_id": f"prop_{r['local_id']}"[:255], "elements": [
                button("Approve", f"ops_prop_approve:{meeting_id}|{r['local_id']}", f"{meeting_id}|{r['local_id']}", "primary"),
                button("Reject", f"ops_prop_reject:{meeting_id}|{r['local_id']}", f"{meeting_id}|{r['local_id']}")]})
        else:
            who = f" by {esc(r['decided_by'])}" if r.get("decided_by") else ""
            blocks.append(context(("✅ Created in Notion" if r["status"] in ("Approved", "Edited") else "✖ Rejected") + who))
    if len(rows) > MAX_PROPOSALS:
        blocks.append(context(f"…and {len(rows) - MAX_PROPOSALS} more are not shown here."))
    if len(pending) > 1:
        blocks.append({"type": "actions", "block_id": "prop_all", "elements": [
            button(f"Approve all {len(pending)}", f"ops_prop_all_approve:{meeting_id}", meeting_id, "primary"),
            button("Reject all", f"ops_prop_all_reject:{meeting_id}", meeting_id)]})
    return blocks


# ----- date-change requests ---------------------------------------------------------------
def date_change_card(meeting_id: str, local_id: str, payload: dict, preview: list[str], status: str = "Proposed",
                     decided_by: str | None = None) -> list[Block]:
    text = (f"📅 *Date change requested*\n“{esc(payload['title'])}”: {esc(payload.get('old_due') or 'no date')} → "
            f"*{esc(payload['new_due'])}*\nRequested by {esc(payload.get('requested_by') or '?')}")
    if payload.get("reason"):
        text += f"\nReason: {esc(payload['reason'])}"
    blocks: list[Block] = [section(text)]
    if preview:
        blocks.append(section("*If approved, this puts at risk:*\n" + "\n".join(f"• {esc(x)}" for x in preview[:8])))
    else:
        blocks.append(context("No downstream task would be put at risk."))
    key = f"{meeting_id}|{local_id}"
    if status == "Proposed":
        blocks.append({"type": "actions", "block_id": "dc_actions", "elements": [
            button("Approve new date", f"ops_dc_approve:{key}", key, "primary"),
            button("Reject", f"ops_dc_reject:{key}", key)]})
    else:
        blocks.append(context(("✅ Approved and written to Notion" if status == "Approved" else "✖ Rejected")
                              + (f" by {esc(decided_by)}" if decided_by else "")))
    return blocks


def slip_modal(task_id: str, title: str) -> dict[str, Any]:
    return {"type": "modal", "callback_id": "ops_slip_submit", "private_metadata": task_id,
            "title": {"type": "plain_text", "text": "Propose a new date"},
            "submit": {"type": "plain_text", "text": "Send for approval"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                section(f"Task: *{esc(title)}*\nThis sends a request. A lead approves before anything changes in Notion."),
                {"type": "input", "block_id": "due", "label": {"type": "plain_text", "text": "New due date"},
                 "element": {"type": "datepicker", "action_id": "new_due"}},
                {"type": "input", "block_id": "reason", "optional": True,
                 "label": {"type": "plain_text", "text": "Reason (optional)"},
                 "element": {"type": "plain_text_input", "action_id": "text", "multiline": True, "max_length": 500}}]}
