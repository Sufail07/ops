"""notion sync: remember what we wrote back (so our own edits are not mistaken for human updates)"""
revision = "0003"
down_revision = "0002"

from alembic import op


def upgrade() -> None:
    op.execute("""
    CREATE TABLE notion_writes (
        page_id text PRIMARY KEY,
        payload_hash text NOT NULL,
        written_at timestamptz NOT NULL
    );
    ALTER TABLE people ADD COLUMN email text;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE people DROP COLUMN email; DROP TABLE notion_writes;")
