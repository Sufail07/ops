"""core schema: cache of Notion, append-only events, runs, snapshots, follow-ups, inbox"""
revision = "0001"
down_revision = None

from alembic import op

TABLES = ["inbox", "followups", "proposals", "risk_events", "priority_snapshots", "runs", "events",
          "edges", "tasks", "goals", "people"]


def upgrade() -> None:
    op.execute("""
    CREATE TABLE people (
        id text PRIMARY KEY,
        name text NOT NULL UNIQUE,
        aliases text[] NOT NULL DEFAULT '{}',
        slack_user_id text,
        notion_user_id text,
        is_lead boolean NOT NULL DEFAULT false,
        backup_id text REFERENCES people(id),
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE goals (
        id text PRIMARY KEY,
        name text NOT NULL,
        weight int NOT NULL DEFAULT 3 CHECK (weight BETWEEN 1 AND 5),
        target_date date,
        synced_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE tasks (
        id text PRIMARY KEY,
        title text NOT NULL,
        owner text,
        due date,
        estimate_days int NOT NULL DEFAULT 1 CHECK (estimate_days >= 0),
        status text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','doing','blocked','done')),
        goal_id text REFERENCES goals(id) ON DELETE SET NULL,
        last_update date,
        notion_edited_at timestamptz,
        synced_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX tasks_goal_idx ON tasks(goal_id);
    CREATE INDEX tasks_owner_idx ON tasks(owner);
    CREATE TABLE edges (
        task_id text NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        depends_on_id text NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        PRIMARY KEY (task_id, depends_on_id),
        CHECK (task_id <> depends_on_id)
    );
    CREATE INDEX edges_dep_idx ON edges(depends_on_id);

    CREATE TABLE events (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        kind text NOT NULL,
        entity text,
        payload jsonb NOT NULL DEFAULT '{}',
        actor text
    );
    CREATE INDEX events_entity_idx ON events(entity, ts);
    CREATE FUNCTION events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'events is append-only'; END $$;
    CREATE TRIGGER events_no_update_delete BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION events_immutable();
    CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
        FOR EACH STATEMENT EXECUTE FUNCTION events_immutable();

    CREATE TABLE runs (
        run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ts timestamptz NOT NULL DEFAULT now(),
        job_type text NOT NULL,
        trigger text,
        input_ref text,
        output_ref text,
        model text,
        tier text,
        attempt int,
        tokens_in int,
        tokens_out int,
        cost_usd numeric(12,6),
        latency_ms int,
        config_version text,
        ok boolean,
        human_outcome text CHECK (human_outcome IN ('approved','edited','rejected','ignored')),
        edit_diff jsonb,
        error_code text,
        error text
    );
    CREATE INDEX runs_job_ts_idx ON runs(job_type, ts);

    CREATE TABLE priority_snapshots (
        day date NOT NULL,
        task_id text NOT NULL,
        score numeric(8,3) NOT NULL,
        band text NOT NULL CHECK (band IN ('P0','P1','P2','P3')),
        fingerprint text NOT NULL,
        why text NOT NULL,
        PRIMARY KEY (day, task_id)
    );
    CREATE TABLE risk_events (
        id bigserial PRIMARY KEY,
        task_id text NOT NULL,
        cause_task_id text,
        old_risk text,
        new_risk text NOT NULL,
        delay_days int NOT NULL DEFAULT 0,
        slack_days int,
        detected_at timestamptz NOT NULL DEFAULT now(),
        notified_at timestamptz
    );
    CREATE INDEX risk_events_task_idx ON risk_events(task_id, detected_at);
    CREATE TABLE proposals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        meeting_id text NOT NULL,
        local_id text NOT NULL,
        payload jsonb NOT NULL,
        status text NOT NULL DEFAULT 'Proposed' CHECK (status IN ('Proposed','Approved','Edited','Rejected')),
        created_at timestamptz NOT NULL DEFAULT now(),
        decided_at timestamptz,
        decided_by text,
        UNIQUE (meeting_id, local_id)
    );
    CREATE TABLE followups (
        id bigserial PRIMARY KEY,
        task_id text NOT NULL,
        kind text NOT NULL,
        step int NOT NULL DEFAULT 0,
        dedupe_key text NOT NULL UNIQUE,
        due_at timestamptz NOT NULL,
        sent_at timestamptz,
        outcome text
    );
    CREATE INDEX followups_pending_idx ON followups(due_at) WHERE sent_at IS NULL;
    CREATE TABLE inbox (
        event_id text PRIMARY KEY,
        source text NOT NULL,
        received_at timestamptz NOT NULL DEFAULT now(),
        processed_at timestamptz
    );
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_no_truncate ON events; DROP TRIGGER IF EXISTS events_no_update_delete ON events;")
    for t in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS events_immutable()")
