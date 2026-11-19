from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Neon: pooled URL (pgbouncer) for app queries; unpooled for migrations and the job queue (LISTEN/NOTIFY).
    database_url: str
    database_url_unpooled: str | None = None
    test_database_url: str | None = None            # a throwaway Neon branch; tests never touch production
    test_database_url_unpooled: str | None = None
    notion_webhook_secret: str | None = None   # Notion webhook verification token; unset = webhook rejects all
    admin_token: str | None = None             # bearer token for /admin/*; unset = admin disabled
    groq_api_key: str | None = None
    notion_pat: str | None = None
    notion_tasks_ds: str = "41ddb6e6-56fd-8023-af64-000bdf23a577"      # Tasks Tracker (data source id)
    notion_projects_ds: str = "d629a324-d4b2-4cce-918b-4c86730e5eac"   # Projects = goals
    notion_meetings_ds: str = "41ddb6e6-56fd-8059-a56e-000bbb9e79e5"   # Meeting Notes
    slack_oauth_token: str | None = None
    slack_app_token: str | None = None
    slack_alerts_channel: str | None = None
    run_slack: bool = True                     # connect Slack Socket Mode inside the API process
    run_worker: bool = True                    # run the job worker inside the API process
    timezone: str = "Asia/Kolkata"
    # Default working hours (Hub/PRD assumption): Mon-Sat 09:30-18:30 IST
    work_start: str = "09:30"
    work_end: str = "18:30"
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)  # Monday=0 ... Saturday=5


    @property
    def direct_url(self) -> str:
        return self.database_url_unpooled or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
