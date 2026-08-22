"""Deterministic tests for the opt-in real-provider AFK lifecycle E2E harness.

Issue #578.  These tests exercise the harness's pure, credential-free seams:

* environment-driven configuration (missing credentials fail with clear,
  secret-free messages),
* secret redaction,
* bounded polling with diagnostics,
* evidence recording (steps + summary with the eight required check
  categories, secrets redacted),
* provider and Gateway HTTP clients against injected httpx transports
  (URL shapes, header conventions, envelope unwrapping),
* the full provider scenario through fake in-memory provider/Gateway
  implementations — happy path and bounded-timeout failure path.

None of these tests touch a real provider, AWX, Kafka, or the network: the
real-provider run remains strictly opt-in via ``python scripts/afk_e2e_test.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from scripts.afk_e2e_test import (
    REQUIRED_CHECK_CATEGORIES,
    E2EConfig,
    EvidenceRecorder,
    GatewayClient,
    GitHubClient,
    GitLabClient,
    HarnessConfigError,
    PollTimeout,
    ScenarioResult,
    load_config,
    poll_until,
    redact,
    run_provider_scenario,
)

# ── Configuration loading ────────────────────────────────────────────────────


def _base_env() -> dict[str, str]:
    return {
        "AFK_E2E_GITHUB_TOKEN": "ghp_test_secret_github",
        "AFK_E2E_GITHUB_ORG": "acme",
        "AFK_E2E_GITLAB_TOKEN": "glpat-test-secret-gitlab",
        "AFK_E2E_GITLAB_GROUP": "acme",
        "AFK_E2E_GATEWAY_API_KEY": "gw-key-secret",
    }


def test_load_config_requires_github_credentials_and_never_echoes_secrets():
    env = _base_env()
    del env["AFK_E2E_GITHUB_ORG"]
    with pytest.raises(HarnessConfigError) as exc:
        load_config(env, ["--provider", "github"])
    message = str(exc.value)
    assert "AFK_E2E_GITHUB_ORG" in message
    assert "ghp_test_secret_github" not in message


def test_load_config_github_only_does_not_require_gitlab_variables():
    env = _base_env()
    del env["AFK_E2E_GITLAB_TOKEN"]
    del env["AFK_E2E_GITLAB_GROUP"]
    config = load_config(env, ["--provider", "github"])
    assert config.providers == ["github"]


def test_load_config_gitlab_requires_gitlab_variables():
    env = _base_env()
    del env["AFK_E2E_GITLAB_TOKEN"]
    with pytest.raises(HarnessConfigError) as exc:
        load_config(env, ["--provider", "gitlab"])
    assert "AFK_E2E_GITLAB_TOKEN" in str(exc.value)


def test_load_config_defaults_and_overrides():
    env = _base_env()
    config = load_config(env, [])
    assert config.providers == ["github", "gitlab"]
    assert config.gateway_base_url == "http://localhost:8000"
    assert config.poll_interval_seconds == 15
    assert config.poll_timeout_seconds == 1800
    assert config.afk_label == "afk"
    assert config.evidence_dir == ".status/afk-e2e-evidence"
    assert not config.dry_run
    assert not config.keep_repos

    overrides = load_config(
        env,
        [
            "--provider",
            "gitlab",
            "--dry-run",
            "--keep-repos",
            "--evidence-dir",
            "/tmp/evidence",
            "--poll-interval",
            "0.5",
            "--poll-timeout",
            "60",
        ],
    )
    assert overrides.providers == ["gitlab"]
    assert overrides.dry_run
    assert overrides.keep_repos
    assert overrides.evidence_dir == "/tmp/evidence"
    assert overrides.poll_interval_seconds == 0.5
    assert overrides.poll_timeout_seconds == 60


def test_load_config_requires_gateway_api_key():
    env = _base_env()
    del env["AFK_E2E_GATEWAY_API_KEY"]
    with pytest.raises(HarnessConfigError) as exc:
        load_config(env, ["--provider", "github"])
    assert "AFK_E2E_GATEWAY_API_KEY" in str(exc.value)


# ── Redaction ────────────────────────────────────────────────────────────────


def test_redact_replaces_every_secret_occurrence():
    text = "used ghp_abc and ghp_abc again; gitlab token glpat-xyz"
    redacted = redact(text, ["ghp_abc", "glpat-xyz"])
    assert "ghp_abc" not in redacted
    assert "glpat-xyz" not in redacted
    assert redacted.count("***") == 3


def test_redact_leaves_ordinary_text_untouched():
    text = "repository github.com/acme/afk-e2e-1 issue 7"
    assert redact(text, ["s3cret"]) == text


# ── Bounded polling ──────────────────────────────────────────────────────────


async def test_poll_until_returns_as_soon_as_predicate_is_truthy():
    calls = {"n": 0}

    async def check() -> str | None:
        calls["n"] += 1
        return "ready" if calls["n"] >= 3 else None

    result = await poll_until(check, interval=0.001, timeout=5, what="develop")
    assert result == "ready"
    assert calls["n"] == 3


async def test_poll_until_raises_poll_timeout_with_attempts_and_elapsed():
    attempts = {"n": 0}

    async def check() -> None:
        attempts["n"] += 1
        return None

    with pytest.raises(PollTimeout) as exc:
        await poll_until(check, interval=0.001, timeout=0.03, what="closure answer")
    assert exc.value.what == "closure answer"
    assert exc.value.elapsed >= 0.0
    assert exc.value.attempts == attempts["n"]
    assert "closure answer" in str(exc.value)


async def test_poll_until_reports_ticks():
    ticks: list[tuple[int, float]] = []

    async def check() -> None:
        return None

    with pytest.raises(PollTimeout):
        await poll_until(
            check,
            interval=0.001,
            timeout=0.03,
            what="merge",
            report=lambda attempts, elapsed: ticks.append((attempts, elapsed)),
        )
    assert len(ticks) >= 1
    assert all(attempt >= 1 for attempt, _ in ticks)


# ── Evidence recording ───────────────────────────────────────────────────────


def test_evidence_recorder_writes_redacted_steps_and_summary(tmp_path):
    evidence_dir = tmp_path / "evidence"
    recorder = EvidenceRecorder(evidence_dir, secrets=["s3cret-token"])
    recorder.record(
        "disposable-repo",
        "passed",
        {"slug": "acme/afk-e2e-x", "token": "s3cret-token"},
    )
    recorder.check(
        "repository",
        "passed",
        {"slug": "acme/afk-e2e-x", "token": "s3cret-token"},
    )
    summary_path = recorder.finish(overall="fail")

    assert summary_path == evidence_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    assert summary["overall"] == "fail"
    assert summary["checks"]["repository"]["slug"] == "acme/afk-e2e-x"
    assert summary["checks"]["repository"]["token"] == "***"
    steps_path = evidence_dir / "steps.jsonl"
    steps = [json.loads(line) for line in steps_path.read_text().splitlines()]
    assert [step["step"] for step in steps] == ["disposable-repo"]
    assert steps[0]["status"] == "passed"
    assert "s3cret-token" not in steps_path.read_text()


def test_evidence_recorder_records_secret_presence_flags_never_values(tmp_path):
    recorder = EvidenceRecorder(tmp_path / "evidence", secrets=["secret-value"])
    recorder.note_secret("AFK_E2E_GITHUB_TOKEN")
    recorder.finish(overall="fail")
    summary = json.loads((tmp_path / "evidence" / "summary.json").read_text())
    assert summary["secrets_provided"]["AFK_E2E_GITHUB_TOKEN"] is True
    assert "secret-value" not in json.dumps(summary)


# ── GitHub provider client ───────────────────────────────────────────────────


def _github_client(handler, org: str = "acme") -> GitHubClient:
    return GitHubClient("test-token", org, transport=httpx.MockTransport(handler))


def test_github_client_creates_repo_under_org_with_bearer_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.method == "POST"
        assert request.url.path == "/orgs/acme/repos"
        return httpx.Response(
            200,
            json={
                "name": "afk-e2e-1",
                "full_name": "acme/afk-e2e-1",
                "html_url": "https://github.com/acme/afk-e2e-1",
            },
        )

    async def scenario() -> dict:
        client = _github_client(handler)
        try:
            return await client.create_disposable_repo("afk-e2e-1")
        finally:
            await client.aclose()

    repo = asyncio.run(scenario())
    assert repo["slug"] == "acme/afk-e2e-1"
    assert repo["web_url"] == "https://github.com/acme/afk-e2e-1"


def test_github_client_delete_repo_reports_failure_not_exception():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, json={"message": "Forbidden"})

    async def scenario() -> bool:
        client = _github_client(handler)
        try:
            return await client.delete_repo("acme/afk-e2e-1")
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) is False
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/repos/acme/afk-e2e-1"


def test_github_client_creates_issue_and_finds_referencing_pr():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/issues") and request.method == "POST":
            return httpx.Response(
                201,
                json={"number": 7, "html_url": "https://github.com/acme/r/issues/7"},
            )
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[
                    {"number": 4, "title": "Unrelated", "body": "nothing", "head": {"ref": "main"}},
                    {
                        "number": 17,
                        "title": "Implement feature",
                        "body": "Closes #7",
                        "head": {"ref": "afk/issue-7-feature"},
                        "created_at": "2026-08-23T10:00:00Z",
                    },
                ],
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario():
        client = _github_client(handler)
        try:
            issue = await client.create_issue("acme/afk-e2e-1", "title", "body", "afk")
            change_request = await client.find_change_request_for_issue(
                "acme/afk-e2e-1", "7"
            )
            return issue, change_request
        finally:
            await client.aclose()

    issue, change_request = asyncio.run(scenario())
    assert issue["number"] == "7"
    assert change_request is not None
    assert change_request["number"] == "17"
    assert change_request["branch"] == "afk/issue-7-feature"


def test_github_client_review_merge_and_issue_state():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"state": body["event"], "id": 1})
        if request.url.path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "abc"})
        if request.url.path.endswith("/issues/7"):
            return httpx.Response(
                200, json={"state": "closed", "closed_at": "2026-08-23T11:00:00Z"}
            )
        if request.url.path.endswith("/pulls/17"):
            return httpx.Response(
                200, json={"merged": True, "merged_at": "2026-08-23T10:59:00Z"}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario():
        client = _github_client(handler)
        try:
            review = await client.post_review("acme/r", "17", "Please fix X", approve=False)
            state = await client.get_change_request_state("acme/r", "17")
            merged = await client.merge_change_request("acme/r", "17")
            issue = await client.get_issue_state("acme/r", "7")
            return review, state, merged, issue
        finally:
            await client.aclose()

    review, state, merged, issue = asyncio.run(scenario())
    assert review["state"] == "REQUEST_CHANGES"
    assert state["merged"] is True
    assert merged["merged_at"] is not None
    assert issue["state"] == "closed"


# ── GitLab provider client ───────────────────────────────────────────────────


def test_gitlab_client_url_encodes_group_and_sends_private_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "glpat-test"
        seen.append(request)
        if request.url.path.endswith("/projects") and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": 42,
                    "path": "afk-e2e-1",
                    "web_url": "https://gitlab.com/acme/sub/afk-e2e-1",
                },
            )
        if request.url.path.endswith("/merge_requests") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 9,
                        "title": "Implement feature",
                        "description": "Closes #5",
                        "source_branch": "afk/issue-5",
                        "created_at": "2026-08-23T10:00:00.000Z",
                    }
                ],
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario():
        client = GitLabClient("glpat-test", "acme/sub", transport=httpx.MockTransport(handler))
        try:
            repo = await client.create_disposable_repo("afk-e2e-1")
            change_request = await client.find_change_request_for_issue(
                repo["slug"], "5"
            )
            return repo, change_request
        finally:
            await client.aclose()

    repo, change_request = asyncio.run(scenario())
    assert repo["slug"] == "acme/sub/afk-e2e-1"
    assert repo["project_id"] == 42
    assert change_request is not None
    assert change_request["number"] == "9"
    # The project path in the MR lookup must be URL-encoded (group contains a slash).
    lookup = seen[-1]
    assert "acme%2Fsub%2Fafk-e2e-1" in lookup.url.raw_path.decode()


def test_gitlab_client_approve_merge_and_issue_state():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/approve"):
            return httpx.Response(201, json={"approved": True})
        if request.url.path.endswith("/merge") and request.method == "PUT":
            return httpx.Response(
                200, json={"state": "merged", "merged_at": "2026-08-23T10:59:00.000Z"}
            )
        if request.url.path.endswith("/notes") and request.method == "POST":
            return httpx.Response(201, json={"id": 1})
        if request.url.path.endswith("/issues/5"):
            return httpx.Response(
                200, json={"state": "closed", "closed_at": "2026-08-23T11:00:00.000Z"}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario():
        client = GitLabClient("glpat-test", "acme", transport=httpx.MockTransport(handler))
        try:
            note = await client.post_review("acme/afk-e2e-1", "9", "Please fix X", approve=False)
            approved = await client.post_review("acme/afk-e2e-1", "9", "LGTM", approve=True)
            merged = await client.merge_change_request("acme/afk-e2e-1", "9")
            issue = await client.get_issue_state("acme/afk-e2e-1", "5")
            return note, approved, merged, issue
        finally:
            await client.aclose()

    note, approved, merged, issue = asyncio.run(scenario())
    assert note["note_id"] == 1
    assert approved["approved"] is True
    assert merged["merged"] is True
    assert merged["merged_at"] is not None
    assert issue["state"] == "closed"


# ── Gateway client ───────────────────────────────────────────────────────────


def test_gateway_client_unwraps_envelope_and_sends_both_auth_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer gw-key"
        assert request.headers["x-operator-token"] == "op-token"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {"items": [{"afk_run_id": "run-1"}], "total": 1},
                "error": None,
            },
        )

    async def scenario() -> list[dict]:
        client = GatewayClient(
            "gw-key",
            operator_token="op-token",
            base_url="http://localhost:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.list_afk_runs("github.com/acme/afk-e2e-1")
        finally:
            await client.aclose()

    runs = asyncio.run(scenario())
    assert runs == [{"afk_run_id": "run-1"}]
    assert seen[0].url.path == "/api/v1/afk-outcomes/runs"
    assert seen[0].url.params.get("repository") == "github.com/acme/afk-e2e-1"


def test_gateway_client_returns_none_on_missing_resources():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"status": "error", "data": None, "error": {"detail": "not found"}},
        )

    async def scenario():
        client = GatewayClient("gw-key", transport=httpx.MockTransport(handler))
        try:
            return await client.get_closure_answer(
                "github", "github.com/acme/r", "7"
            )
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) is None


def test_gateway_client_execution_bindings_unwrap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "resource": {},
                    "bindings": [
                        {"awx_job": {"job_id": "1234"}, "outcome": "completed"}
                    ],
                },
                "error": None,
            },
        )

    async def scenario() -> list[dict]:
        client = GatewayClient("gw-key", transport=httpx.MockTransport(handler))
        try:
            return await client.list_execution_bindings(
                "github", "github.com/acme/r", "17"
            )
        finally:
            await client.aclose()

    bindings = asyncio.run(scenario())
    assert bindings == [{"awx_job": {"job_id": "1234"}, "outcome": "completed"}]


def test_gateway_client_operator_token_optional():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-operator-token" not in request.headers
        return httpx.Response(200, json={"status": "ok", "data": [], "error": None})

    async def scenario() -> list[dict]:
        client = GatewayClient("gw-key", transport=httpx.MockTransport(handler))
        try:
            return await client.list_afk_runs("github.com/acme/r")
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == []


# ── Full scenario through fakes ──────────────────────────────────────────────


class FakeProvider:
    """In-memory provider simulating the asynchronous AFK pipeline."""

    def __init__(
        self, *, develop_ticks: int = 2, fix_ticks: int = 2, close_ticks: int = 2
    ):
        self.develop_ticks = develop_ticks
        self.fix_ticks = fix_ticks
        self.close_ticks = close_ticks
        self.find_calls = 0
        self.commit_calls = 0
        self.close_calls = 0
        self.reviewed = False
        self.approved = False
        self.merged_at: str | None = None
        self.closed_at: str | None = None
        self.delete_calls = 0
        self.merge_calls = 0
        self.issue_number = "7"
        self.cr_number = "17"

    async def create_disposable_repo(self, name: str) -> dict:
        return {"slug": f"acme/{name}", "web_url": f"https://github.com/acme/{name}"}

    async def delete_repo(self, slug: str) -> bool:
        self.delete_calls += 1
        return True

    async def create_issue(self, slug: str, title: str, body: str, label: str) -> dict:
        return {"number": self.issue_number, "title": title, "label": label}

    async def find_change_request_for_issue(
        self, slug: str, issue_number: str
    ) -> dict | None:
        self.find_calls += 1
        if self.find_calls < self.develop_ticks:
            return None
        return {
            "number": self.cr_number,
            "title": "Implement feature",
            "branch": f"afk/issue-{issue_number}",
            "opened_at": "2026-08-23T10:00:00Z",
        }

    async def get_change_request_state(self, slug: str, cr_number: str) -> dict:
        return {"merged": self.merged_at is not None, "merged_at": self.merged_at}

    async def post_review(
        self, slug: str, cr_number: str, comment: str, approve: bool
    ) -> dict:
        if approve:
            self.approved = True
            return {"state": "APPROVED"}
        self.reviewed = True
        return {"state": "REQUEST_CHANGES"}

    async def count_new_commits(
        self, slug: str, cr_number: str, since: datetime
    ) -> int:
        self.commit_calls += 1
        if not self.reviewed or self.commit_calls < self.fix_ticks:
            return 0
        return 1

    async def merge_change_request(self, slug: str, cr_number: str) -> dict:
        self.merge_calls += 1
        self.merged_at = "2026-08-23T10:59:00Z"
        return {"merged_at": self.merged_at}

    async def get_issue_state(self, slug: str, issue_number: str) -> dict:
        self.close_calls += 1
        if self.merged_at and self.close_calls >= self.close_ticks:
            self.closed_at = "2026-08-23T11:00:00Z"
            return {"state": "closed", "closed_at": self.closed_at}
        return {"state": "open", "closed_at": None}

    async def close_issue(self, slug: str, issue_number: str) -> dict:
        self.closed_at = "2026-08-23T11:05:00Z"
        return {"state": "closed", "closed_at": self.closed_at}

    async def verify_credentials(self) -> dict:
        return {"login": "acme-bot"}

    async def aclose(self) -> None:
        return None


class FakeGateway:
    """In-memory Gateway returning evidence once the fake provider advances."""

    def __init__(
        self, provider: FakeProvider, *, bind_ticks: int = 2, closure_ticks: int = 2
    ):
        self.provider = provider
        self.bind_ticks = bind_ticks
        self.closure_ticks = closure_ticks
        self.bind_calls = 0
        self.closure_calls = 0

    async def list_afk_runs(self, repository: str) -> list[dict]:
        if self.provider.merged_at is None:
            return []
        return [
            {
                "afk_run_id": "run-1",
                "provider": "github",
                "status": "completed",
                "outcome_status": "merged",
            }
        ]

    async def get_afk_run_detail(self, afk_run_id: str) -> dict | None:
        if self.provider.merged_at is None:
            return None
        return {
            "afk_run_id": "run-1",
            "run": {"status": "completed", "outcome_status": "merged"},
            "outcome": {
                "status": "merged",
                "merged_at": self.provider.merged_at,
                "merge_event_id": "merge_event:17",
                "change_request_ids": ["change_request:17"],
                "resolved_issue_ids": ["issue:7"],
            },
            "change_requests": [{"entity_id": "change_request:17", "external_id": "17"}],
            "sessions": [
                {
                    "session_id": "uuid-1",
                    "external_session_id": "ses_dev_1",
                    "agent": "build",
                    "inferred": True,
                }
            ],
            "usage": {
                "active_tokens": 123,
                "input_tokens": 100,
                "output_tokens": 23,
                "estimated_cost_usd": "0.0012",
            },
        }

    async def list_execution_bindings(
        self, provider: str, repository_url: str, entity_number: str
    ) -> list[dict]:
        self.bind_calls += 1
        if self.bind_calls < self.bind_ticks:
            return []
        return [
            {
                "awx_job": {"job_id": "1234", "job_template_id": "9"},
                "outcome": "completed",
                "external_session_id": "ses_dev_1",
            }
        ]

    async def get_closure_answer(
        self, provider: str, repository: str, external_id: str
    ) -> dict | None:
        self.closure_calls += 1
        if self.provider.closed_at is None or self.closure_calls < self.closure_ticks:
            return None
        return {
            "episode": {
                "status": "inferred",
                "closed_at": self.provider.closed_at,
                "change_request_external_id": "17",
            },
            "derived_at": "2026-08-23T11:01:00Z",
            "resolver_version": "1",
        }

    async def get_reporting_detail(
        self,
        provider: str,
        repository_url: str,
        resource_type: str,
        resource_number: str,
    ) -> dict | None:
        return None


def _config_for_fakes(**overrides) -> E2EConfig:
    base = dict(
        providers=["github"],
        github_token="test-token",
        github_org="acme",
        gitlab_token="",
        gitlab_group="",
        gateway_base_url="http://localhost:8000",
        gateway_api_key="gw-key",
        operator_token="",
        evidence_dir=".status/afk-e2e-evidence",
        poll_interval_seconds=0.001,
        poll_timeout_seconds=5,
        afk_label="afk",
        github_api_base="https://api.github.com",
        gitlab_api_base="https://gitlab.com/api/v4",
        repo_prefix="afk-e2e",
        repo_suffix="x1",
        dry_run=False,
        keep_repos=False,
    )
    base.update(overrides)
    return E2EConfig(**base)


async def _run_scenario(
    provider: FakeProvider, gateway: FakeGateway, tmp_path
) -> ScenarioResult:
    config = _config_for_fakes(evidence_dir=str(tmp_path / "evidence"))
    recorder = EvidenceRecorder(config.evidence_dir, secrets=["test-token", "gw-key"])
    return await run_provider_scenario(
        "github", config, provider, gateway, recorder
    )


async def test_scenario_happy_path_records_all_evidence_categories(tmp_path):
    provider = FakeProvider()
    gateway = FakeGateway(provider)

    result = await _run_scenario(provider, gateway, tmp_path)

    assert result.passed is True
    assert set(result.checks) == set(REQUIRED_CHECK_CATEGORIES)
    assert all(
        result.checks[category]["status"] == "passed"
        for category in REQUIRED_CHECK_CATEGORIES
    )
    assert result.checks["repository"]["slug"] == "acme/afk-e2e-github-x1"
    assert result.checks["repository"]["identity"] == "github.com/acme/afk-e2e-github-x1"
    assert result.checks["issue"]["number"] == "7"
    assert result.checks["change_request"]["number"] == "17"
    assert result.checks["awx_jobs"]["bindings"][0]["awx_job"]["job_id"] == "1234"
    assert (
        result.checks["sessions"]["run_sessions"][0]["external_session_id"]
        == "ses_dev_1"
    )
    assert result.checks["usage"]["active_tokens"] == 123
    assert result.checks["merge"]["merged"] is True
    assert result.checks["closure"]["closed"] is True
    assert result.checks["closure"]["mechanism"] == "provider"
    assert result.checks["closure"]["episode_status"] == "inferred"
    # The lifecycle exercises develop, review, fix/re-review, merge, and closure.
    step_names = [step["step"] for step in result.steps]
    for expected in (
        "disposable-repo",
        "afk-trigger",
        "develop-execution",
        "awx-execution-bindings",
        "review-request",
        "fix-re-review",
        "merge",
        "issue-closure",
        "gateway-afk-run",
        "closure-projection",
    ):
        assert expected in step_names
    assert provider.reviewed is True
    assert provider.approved is True
    assert provider.merge_calls == 1
    assert provider.delete_calls == 1
    assert result.cleanup["deleted"] is True


async def test_scenario_poll_timeout_fails_clearly_and_still_cleans_up(tmp_path):
    provider = FakeProvider(develop_ticks=10_000)  # develop never appears
    gateway = FakeGateway(provider)
    config = _config_for_fakes(
        evidence_dir=str(tmp_path / "evidence"),
        poll_timeout_seconds=0.05,
    )
    recorder = EvidenceRecorder(config.evidence_dir, secrets=[])
    result = await run_provider_scenario("github", config, provider, gateway, recorder)

    assert result.passed is False
    assert set(result.checks) == set(REQUIRED_CHECK_CATEGORIES)
    assert result.checks["repository"]["status"] == "passed"
    assert result.checks["issue"]["status"] == "passed"
    assert result.checks["change_request"]["status"] == "failed"
    assert result.checks["change_request"]["error"] == "poll-timeout"
    assert result.checks["change_request"]["attempts"] >= 1
    assert result.checks["change_request"]["timeout_seconds"] == 0.05
    # Steps that were never reached are marked explicitly, never fabricated.
    for category in ("awx_jobs", "sessions", "usage", "merge", "closure"):
        assert result.checks[category]["status"] == "not_attempted"
    # Cleanup is still attempted on the failure path.
    assert provider.delete_calls == 1
    assert result.cleanup["deleted"] is True


async def test_scenario_keep_repos_skips_deletion(tmp_path):
    provider = FakeProvider()
    gateway = FakeGateway(provider)
    config = _config_for_fakes(
        evidence_dir=str(tmp_path / "evidence"),
        keep_repos=True,
    )
    recorder = EvidenceRecorder(config.evidence_dir, secrets=[])
    result = await run_provider_scenario("github", config, provider, gateway, recorder)

    assert result.passed is True
    assert provider.delete_calls == 0
    assert result.cleanup["kept"] is True
