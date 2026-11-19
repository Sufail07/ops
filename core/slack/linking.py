"""Link Slack users to the people table (by email, then exact name). Never guesses beyond that."""
from __future__ import annotations

from typing import Any

from core.db import Database


def link_slack_users(db: Database, slack_client: Any) -> dict[str, int]:
    people = db.load_people()
    by_email = {p.email.casefold(): p for p in people if p.email}
    by_name = {p.name.casefold(): p for p in people}
    linked = created = 0
    cursor = None
    while True:
        res = slack_client.users_list(limit=200, **({"cursor": cursor} if cursor else {}))
        for m in res["members"]:
            if m.get("is_bot") or m.get("deleted") or m["id"] == "USLACKBOT":
                continue
            prof = m.get("profile", {})
            email = (prof.get("email") or "").casefold()
            name = (m.get("real_name") or prof.get("real_name") or m.get("name") or m["id"]).strip()
            match = by_email.get(email) if email else None
            match = match or by_name.get(name.casefold())
            if match and match.slack_user_id == m["id"]:
                continue
            if match:
                db.set_slack_user(match.id, m["id"])
                linked += 1
            elif not any(p.slack_user_id == m["id"] for p in people):
                db.add_slack_person(m["id"], name, email or None)
                created += 1
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return {"linked": linked, "created": created}
