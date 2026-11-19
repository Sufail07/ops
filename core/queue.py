from __future__ import annotations

import procrastinate

from core.settings import get_settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=get_settings().direct_url),
    import_paths=["core.jobs"],
)
