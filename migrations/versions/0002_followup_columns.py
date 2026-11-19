"""follow-ups carry their recipient role, text and severity"""
revision = "0002"
down_revision = "0001"

from alembic import op


def upgrade() -> None:
    op.execute("""
    ALTER TABLE followups
        ADD COLUMN recipient_role text NOT NULL DEFAULT 'owner' CHECK (recipient_role IN ('owner','lead','backup')),
        ADD COLUMN text text NOT NULL DEFAULT '',
        ADD COLUMN severity text NOT NULL DEFAULT 'info',
        ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE followups DROP COLUMN recipient_role, DROP COLUMN text, DROP COLUMN severity, DROP COLUMN created_at")
