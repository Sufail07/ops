import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from core.db import Database
from core.settings import get_settings

_S = get_settings()


@pytest.fixture(scope="session")
def test_db_url():
    """Run tests only against the throwaway Neon test branch, never the production branch."""
    url, direct = _S.test_database_url, _S.test_database_url_unpooled or _S.test_database_url
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (create a Neon test branch: neon checkout test --create)")
    assert url != _S.database_url and direct != _S.direct_url, "refusing to run tests against the production database"
    with psycopg.connect(direct, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "DATABASE_URL_UNPOOLED")}
    os.environ["DATABASE_URL"], os.environ["DATABASE_URL_UNPOOLED"] = url, direct
    command.upgrade(Config("alembic.ini"), "head")
    import asyncio

    import procrastinate
    qa = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=direct))

    async def _apply():
        async with qa.open_async():
            await qa.schema_manager.apply_schema_async()
    asyncio.run(_apply())
    yield url
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="session")
def _shared_db(test_db_url):
    d = Database(test_db_url)
    d.load_graph_inputs()  # open the pool once per session (Neon connection setup is ~1.5 s)
    yield d
    d.close()


@pytest.fixture()
def db(_shared_db):
    with _shared_db.pool.connection() as c:  # one statement: tests must not pay a round trip per table
        c.execute("TRUNCATE inbox, followups, proposals, risk_events, priority_snapshots, notion_writes, "
                  "edges, tasks, goals, people, alert_outbox CASCADE")
    return _shared_db
