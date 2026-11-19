"""Link Slack users, then mark a person as a lead (approver for date changes). Runs on the DATABASE_URL in .env.

  python scripts/set_lead.py "Sufail"            # make them a lead
  python scripts/set_lead.py "Sufail" --remove   # take lead status away
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.settings import get_settings  # noqa: E402
from core.db import Database  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    sys.exit(__doc__)
name, remove = args[0], "--remove" in sys.argv
s = get_settings()
db = Database(s.database_url)
if s.slack_oauth_token:
    from slack_sdk import WebClient
    from core.slack.linking import link_slack_users
    print("slack link:", link_slack_users(db, WebClient(token=s.slack_oauth_token)))
people = db.load_people()
match = [p for p in people if p.name.casefold() == name.casefold()]
if len(match) != 1:
    sys.exit(f"Expected exactly one person named '{name}', found {len(match)}. People: {[p.name for p in people]}")
db._exec("UPDATE people SET is_lead=%s WHERE id=%s", (not remove, match[0].id))
db.append_event("person.lead_changed", match[0].id, {"is_lead": not remove}, "admin_script")
print(f"{match[0].name}: is_lead={not remove}")
print("leads now:", [p.name for p in db.load_people() if p.is_lead])
db.close()
