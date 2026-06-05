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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
