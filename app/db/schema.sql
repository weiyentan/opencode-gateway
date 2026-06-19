CREATE TABLE IF NOT EXISTS gateway_jobs (
    id              UUID PRIMARY KEY,
    status          TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    task_summary    TEXT NOT NULL,
    runner_id       UUID,
    workspace_name  TEXT,
    opencode_url    TEXT,
    opencode_session_id TEXT,
    executor_type   TEXT NOT NULL,
    executor_job_id TEXT,
    env_vars        JSONB DEFAULT '{}'::jsonb,
    branch_name     TEXT,
    mr_url          TEXT,
    workflow_run_id TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    diff            TEXT
);

ALTER TABLE gateway_jobs ADD COLUMN IF NOT EXISTS diff TEXT;
ALTER TABLE gateway_jobs ADD COLUMN IF NOT EXISTS branch_name TEXT;
ALTER TABLE gateway_jobs ADD COLUMN IF NOT EXISTS commit_sha TEXT;
ALTER TABLE gateway_jobs ADD COLUMN IF NOT EXISTS mr_url TEXT;
ALTER TABLE gateway_jobs ADD COLUMN IF NOT EXISTS workflow_run_id TEXT;
ALTER TABLE gateway_jobs ADD COLUMN IF NOT EXISTS failure_reason TEXT;

CREATE TABLE IF NOT EXISTS approvals (
    id              UUID PRIMARY KEY,
    job_id          UUID NOT NULL REFERENCES gateway_jobs(id),
    requested_by    TEXT NOT NULL,
    requested_action TEXT NOT NULL,
    approval_type   TEXT NOT NULL,
    approved_by     TEXT,
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS workspaces (
    id              UUID PRIMARY KEY,
    runner_id       UUID,
    workspace_name  TEXT NOT NULL,
    path            TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    branch          TEXT,
    port            INTEGER,
    service_name    TEXT,
    pinned          BOOLEAN NOT NULL DEFAULT FALSE,
    cleanup_after   TIMESTAMPTZ,
    cleanup_status  TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_events (
    id              UUID PRIMARY KEY,
    job_id          UUID NOT NULL REFERENCES gateway_jobs(id),
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    details         TEXT,
    previous_status TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhooks (
    id              UUID PRIMARY KEY,
    url             TEXT NOT NULL,
    events          TEXT[] NOT NULL DEFAULT '{}',
    secret          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
