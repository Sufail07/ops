"""transactional outbox for alerts, and a 'card posted' marker for meeting proposals

An alert is written here in the SAME atomic batch as the change that caused it, so a crash can no longer lose it.
Delivery marks it sent; a sweeper re-sends anything still unsent.
"""
revision = "0004"
down_revision = "0003"

from alembic import op


def upgrade() -> None:
    op.execute("""
    CREATE TABLE alert_outbox (
        id bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        owner text,
        severity text NOT NULL,
        kind text NOT NULL,
        text text NOT NULL,
        task_ids text[] NOT NULL DEFAULT '{}',
        items jsonb NOT NULL DEFAULT '[]',
        attempts int NOT NULL DEFAULT 0,
        claimed_at timestamptz,
        sent_at timestamptz,
        last_error text
    );
    CREATE INDEX alert_outbox_pending_idx ON alert_outbox(created_at) WHERE sent_at IS NULL;

    ALTER TABLE proposals ADD COLUMN card_sent_at timestamptz;
    UPDATE proposals SET card_sent_at = created_at;   -- cards that already exist are not re-posted
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE proposals DROP COLUMN card_sent_at; DROP TABLE alert_outbox;")
