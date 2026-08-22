#!/usr/bin/env python3
"""Opt-in real-provider AFK lifecycle end-to-end harness (issue #578).

Drives one full AFK lifecycle per provider — GitHub and/or GitLab — through
**disposable** repositories/projects created solely for this run, and records
evidence for every stage: repository, issue (AFK trigger), develop execution,
AWX execution bindings, review / fix / re-review, merge, issue closure,
OpenCode sessions, usage/cost, and the closure-episode projection.

This harness is **deliberately opt-in and HITL**: it depends on real provider
credentials, an operational environment where the EDA gateway, AWX job
templates, OpenCode runners, and the Gateway are already wired to observe the
target org/group, and on provider permissions to create and delete disposable
repositories.  It is **never** invoked by the default frontend test command
(``node frontend/tests/test_pure_functions.js``), by ``pytest`` (testpaths is
``tests``), or by CI.  Run it explicitly::

    python scripts/afk_e2e_test.py --provider github
    python scripts/afk_e2e_test.py --provider gitlab
    python scripts/afk_e2e_test.py --dry-run --provider both

Credentials and other secrets are supplied exclusively through environment
variables (see :func:`load_config`) and are **never printed**: diagnostics are
redacted against the active secret values before output, and evidence files
record only *presence flags* for secrets, never their values.

Async behaviour (develop execution, fixes, AWX bindings, AFK-run
reconstruction, closure projection) is handled with bounded polling: each
phase polls at ``AFK_E2E_POLL_INTERVAL_SECONDS`` until
``AFK_E2E_POLL_TIMEOUT_SECONDS``, recording attempts and elapsed time, then
fails with useful diagnostics instead of hanging.

Cleanup is attempted at the end of every run (unless ``--keep-repos``);
deletion failures are reported clearly and never crash the harness.

See ``docs/afk-e2e-validation.md`` for the operational prerequisites and the
full scenario walkthrough.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets as _secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence

import httpx

# Allow running from any location by resolving the repo root relative to this script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.repository import normalize_repository_url  # noqa: E402

# ── Locked vocabulary ────────────────────────────────────────────────────────

# The eight evidence categories every scenario must record (issue #578):
# repository, change request, issue, AWX jobs, OpenCode sessions, usage/cost,
# merge, closure.  The summary asserts their presence so a partial run can
# never masquerade as a complete lifecycle audit.
REQUIRED_CHECK_CATEGORIES = (
    "repository",
    "change_request",
    "issue",
    "awx_jobs",
    "sessions",
    "usage",
    "merge",
    "closure",
)

DEFAULT_EVIDENCE_DIR = ".status/afk-e2e-evidence"
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
DEFAULT_GITLAB_API_BASE = "https://gitlab.com/api/v4"
DEFAULT_GATEWAY_BASE_URL = "http://localhost:8000"
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_POLL_TIMEOUT_SECONDS = 1800.0
DEFAULT_AFK_LABEL = "afk"
DEFAULT_REPO_PREFIX = "afk-e2e"

REDACTED = "***"


# ── Errors ───────────────────────────────────────────────────────────────────


class HarnessConfigError(Exception):
    """Configuration is incomplete or invalid; nothing was attempted."""


class PollTimeout(Exception):
    """A bounded poll exhausted its deadline without observing the condition."""

    def __init__(self, what: str, elapsed: float, attempts: int) -> None:
        self.what = what
        self.elapsed = elapsed
        self.attempts = attempts
        super().__init__(
            f"timed out after {elapsed:.1f}s ({attempts} attempts) waiting for: {what}"
        )


class ProviderApiError(Exception):
    """A provider API call returned a non-2xx status (or otherwise failed)."""

    def __init__(self, method: str, url: str, status: int, detail: str) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.detail = detail
        super().__init__(f"{method} {url} -> {status}: {detail}")


class StepFailure(Exception):
    """A scenario step failed; carries the evidence detail to record."""

    def __init__(self, detail: Mapping[str, Any]) -> None:
        self.detail = dict(detail)
        super().__init__(detail.get("error", "step failed"))


def _fail(error: str, **extra: Any) -> StepFailure:
    return StepFailure({"error": error, **extra})


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass
class E2EConfig:
    """Resolved harness configuration — secrets present but never printed."""

    providers: list[str]
    github_token: str
    github_org: str
    gitlab_token: str
    gitlab_group: str
    gateway_base_url: str
    gateway_api_key: str
    operator_token: str
    evidence_dir: str
    poll_interval_seconds: float
    poll_timeout_seconds: float
    afk_label: str
    github_api_base: str
    gitlab_api_base: str
    repo_prefix: str
    repo_suffix: str
    dry_run: bool
    keep_repos: bool

    def secret_values(self) -> list[str]:
        return [
            value
            for value in (
                self.github_token,
                self.gitlab_token,
                self.gateway_api_key,
                self.operator_token,
            )
            if value
        ]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Opt-in real-provider AFK lifecycle E2E: drive issue -> develop -> "
            "review -> fix/re-review -> merge -> closure against disposable "
            "GitHub/GitLab repositories and record lifecycle evidence."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["github", "gitlab", "both"],
        default="both",
        help="Which provider(s) to exercise (default: both).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate credentials, provider/org reachability, and Gateway "
            "connectivity without creating any resources."
        ),
    )
    parser.add_argument(
        "--keep-repos",
        action="store_true",
        help="Skip cleanup — keep the disposable repositories for post-mortem.",
    )
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help=f"Directory for evidence files (default: {DEFAULT_EVIDENCE_DIR}).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help=f"Poll interval in seconds (default: {DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=None,
        help=(
            "Per-phase poll timeout in seconds "
            f"(default: {DEFAULT_POLL_TIMEOUT_SECONDS})."
        ),
    )
    return parser


def _missing(required: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    return [name for name in required if not environ.get(name, "").strip()]


def load_config(
    environ: Mapping[str, str], argv: Optional[list[str]] = None
) -> E2EConfig:
    """Resolve the harness configuration from environment variables and flags.

    Secrets are read from ``AFK_E2E_*`` environment variables only — nothing
    is hardcoded, and error messages list variable *names*, never values.
    """
    args = _build_arg_parser().parse_args(argv)

    providers: list[str]
    if args.provider == "both":
        providers = ["github", "gitlab"]
    else:
        providers = [args.provider]

    required: list[str] = []
    if "github" in providers:
        required.extend(["AFK_E2E_GITHUB_TOKEN", "AFK_E2E_GITHUB_ORG"])
    if "gitlab" in providers:
        required.extend(["AFK_E2E_GITLAB_TOKEN", "AFK_E2E_GITLAB_GROUP"])
    required.append("AFK_E2E_GATEWAY_API_KEY")
    missing = _missing(required, environ)
    if missing:
        raise HarnessConfigError(
            "missing required environment variable(s): "
            + ", ".join(sorted(missing))
            + " — see docs/afk-e2e-validation.md for the full list"
        )

    def _float_env(name: str, default: float) -> float:
        raw = environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            raise HarnessConfigError(f"{name} must be a number, got non-numeric value") from None

    interval = args.poll_interval
    if interval is None:
        interval = _float_env("AFK_E2E_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS)
    timeout = args.poll_timeout
    if timeout is None:
        timeout = _float_env("AFK_E2E_POLL_TIMEOUT_SECONDS", DEFAULT_POLL_TIMEOUT_SECONDS)
    if interval <= 0 or timeout <= 0:
        raise HarnessConfigError("poll interval and timeout must be positive")

    return E2EConfig(
        providers=providers,
        github_token=environ.get("AFK_E2E_GITHUB_TOKEN", "").strip(),
        github_org=environ.get("AFK_E2E_GITHUB_ORG", "").strip(),
        gitlab_token=environ.get("AFK_E2E_GITLAB_TOKEN", "").strip(),
        gitlab_group=environ.get("AFK_E2E_GITLAB_GROUP", "").strip(),
        gateway_base_url=environ.get(
            "AFK_E2E_GATEWAY_BASE_URL", DEFAULT_GATEWAY_BASE_URL
        ).strip(),
        gateway_api_key=environ.get("AFK_E2E_GATEWAY_API_KEY", "").strip(),
        operator_token=environ.get("AFK_E2E_OPERATOR_TOKEN", "").strip(),
        evidence_dir=(
            args.evidence_dir
            or environ.get("AFK_E2E_EVIDENCE_DIR", "").strip()
            or DEFAULT_EVIDENCE_DIR
        ),
        poll_interval_seconds=interval,
        poll_timeout_seconds=timeout,
        afk_label=environ.get("AFK_E2E_AFK_LABEL", DEFAULT_AFK_LABEL).strip()
        or DEFAULT_AFK_LABEL,
        github_api_base=environ.get("AFK_E2E_GITHUB_API_BASE", DEFAULT_GITHUB_API_BASE).strip(),
        gitlab_api_base=environ.get("AFK_E2E_GITLAB_API_BASE", DEFAULT_GITLAB_API_BASE).strip(),
        repo_prefix=environ.get("AFK_E2E_REPO_PREFIX", DEFAULT_REPO_PREFIX).strip()
        or DEFAULT_REPO_PREFIX,
        repo_suffix=environ.get("AFK_E2E_REPO_SUFFIX", "").strip()
        or _secrets.token_hex(3),
        dry_run=args.dry_run,
        keep_repos=args.keep_repos,
    )


# ── Secret redaction ─────────────────────────────────────────────────────────


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every occurrence of every secret with a fixed marker."""
    for secret in sorted(set(s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


# ── Bounded polling ──────────────────────────────────────────────────────────


async def poll_until(
    predicate: Callable[[], Awaitable[Any]],
    *,
    interval: float,
    timeout: float,
    what: str,
    report: Optional[Callable[[int, float], None]] = None,
) -> Any:
    """Poll ``predicate`` until it returns a non-None value, else raise.

    ``report`` (optional) receives ``(attempts, elapsed_seconds)`` after each
    unsuccessful attempt so callers can emit progress diagnostics.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    attempts = 0
    while True:
        result = await predicate()
        attempts += 1
        if result is not None:
            return result
        elapsed = loop.time() - start
        if elapsed >= timeout:
            raise PollTimeout(what=what, elapsed=round(elapsed, 1), attempts=attempts)
        if report is not None:
            report(attempts, round(elapsed, 1))
        await asyncio.sleep(interval)


# ── Evidence recording ───────────────────────────────────────────────────────


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 - datetime.UTC is 3.11+


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class EvidenceRecorder:
    """Writes step-by-step evidence and a final summary, secrets redacted."""

    def __init__(self, evidence_dir: Any, secrets: Sequence[str]) -> None:
        self.path = Path(evidence_dir)
        self.path.mkdir(parents=True, exist_ok=True)
        self._secrets = list(secrets)
        self._checks: dict[str, dict[str, Any]] = {}
        self._steps: list[dict[str, Any]] = []
        self._secrets_provided: dict[str, bool] = {}
        self._notes: dict[str, Any] = {}
        self.run_id = f"afk-e2e-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"  # noqa: UP017
        self.started_at = _iso_now()

    @property
    def checks(self) -> dict[str, dict[str, Any]]:
        return self._checks

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact(value, self._secrets)
        if isinstance(value, dict):
            return {str(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def record(self, step: str, status: str, detail: Mapping[str, Any]) -> None:
        """Append one step line (flushed immediately so evidence survives crashes)."""
        entry: dict[str, Any] = {"step": step, "status": status, "at": _iso_now()}
        entry.update(self._redact(dict(detail)))
        self._steps.append(entry)
        with (self.path / "steps.jsonl").open("a") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

    def check(self, category: str, status: str, detail: Mapping[str, Any]) -> None:
        """Record (or merge into) one evidence-category check.

        Details merge across steps (e.g. the closure check gains projection
        fields from the closure-projection step); a ``failed`` status is never
        silently upgraded back to ``passed``.
        """
        cleaned = self._redact(dict(detail))
        existing = self._checks.get(category)
        if existing is None:
            self._checks[category] = {"status": status, **cleaned}
            return
        existing.update(cleaned)
        if status == "failed":
            existing["status"] = "failed"

    def note_secret(self, variable_name: str) -> None:
        """Record that a secret variable was *provided* — presence only, never value."""
        self._secrets_provided[variable_name] = True

    def note(self, key: str, value: Any) -> None:
        self._notes[key] = self._redact(value)

    def finish(self, overall: str) -> Path:
        """Write ``summary.json`` and return its path."""
        summary = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": _iso_now(),
            "overall": overall,
            "secrets_provided": dict(self._secrets_provided),
            "checks": self._checks,
            "steps": self._steps,
            "notes": self._notes,
        }
        summary_path = self.path / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str))
        return summary_path


# ── Provider clients ─────────────────────────────────────────────────────────


class ProviderClient(Protocol):
    """The provider surface a scenario drives (implemented by GitHub/GitLab)."""

    async def create_disposable_repo(self, name: str) -> dict[str, Any]: ...
    async def delete_repo(self, slug: str) -> bool: ...
    async def create_issue(
        self, slug: str, title: str, body: str, label: str
    ) -> dict[str, Any]: ...
    async def find_change_request_for_issue(
        self, slug: str, issue_number: str
    ) -> Optional[dict[str, Any]]: ...
    async def get_change_request_state(
        self, slug: str, cr_number: str
    ) -> dict[str, Any]: ...
    async def post_review(
        self, slug: str, cr_number: str, comment: str, approve: bool
    ) -> dict[str, Any]: ...
    async def count_new_commits(
        self, slug: str, cr_number: str, since: datetime
    ) -> int: ...
    async def merge_change_request(self, slug: str, cr_number: str) -> dict[str, Any]: ...
    async def get_issue_state(self, slug: str, issue_number: str) -> dict[str, Any]: ...
    async def close_issue(self, slug: str, issue_number: str) -> dict[str, Any]: ...
    async def verify_credentials(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


class GitHubClient:
    """Drive disposable GitHub repositories via the REST API."""

    provider = "github"

    def __init__(
        self,
        token: str,
        org: str,
        *,
        base_url: str = DEFAULT_GITHUB_API_BASE,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._org = org
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, transport=transport
        )

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, params: Any = None
    ) -> httpx.Response:
        response = await self._client.request(method, path, json=json_body, params=params)
        if response.status_code >= 400:
            raise ProviderApiError(
                method=method,
                url=str(response.request.url),
                status=response.status_code,
                detail=response.text[:500],
            )
        return response

    async def verify_credentials(self) -> dict[str, Any]:
        response = await self._request("GET", "/user")
        body = response.json()
        return {"login": body.get("login")}

    async def create_disposable_repo(self, name: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/orgs/{self._org}/repos", json_body={"name": name, "private": True}
        )
        body = response.json()
        return {"slug": body["full_name"], "web_url": body["html_url"]}

    async def delete_repo(self, slug: str) -> bool:
        response = await self._client.request("DELETE", f"/repos/{slug}")
        return response.status_code in (200, 204)

    async def create_issue(
        self, slug: str, title: str, body: str, label: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/repos/{slug}/issues", json_body={"title": title, "body": body, "labels": [label]}
        )
        body = response.json()
        return {
            "number": str(body["number"]),
            "title": body.get("title"),
            "web_url": body.get("html_url"),
        }

    async def find_change_request_for_issue(
        self, slug: str, issue_number: str
    ) -> Optional[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/repos/{slug}/pulls",
            params={"state": "all", "sort": "updated", "direction": "desc", "per_page": "50"},
        )
        reference = f"#{issue_number}"
        for pr in response.json():
            title = str(pr.get("title") or "")
            body = str(pr.get("body") or "")
            head = pr.get("head") or {}
            branch = str(head.get("ref") or "")
            if reference in title or reference in body or issue_number in branch:
                return {
                    "number": str(pr["number"]),
                    "title": title,
                    "branch": branch,
                    "state": pr.get("state"),
                    "opened_at": pr.get("created_at"),
                    "web_url": pr.get("html_url"),
                }
        return None

    async def get_change_request_state(self, slug: str, cr_number: str) -> dict[str, Any]:
        response = await self._request("GET", f"/repos/{slug}/pulls/{cr_number}")
        body = response.json()
        return {"merged": bool(body.get("merged")), "merged_at": body.get("merged_at")}

    async def post_review(
        self, slug: str, cr_number: str, comment: str, approve: bool
    ) -> dict[str, Any]:
        event = "APPROVE" if approve else "REQUEST_CHANGES"
        response = await self._request(
            "POST",
            f"/repos/{slug}/pulls/{cr_number}/reviews",
            json_body={"body": comment, "event": event},
        )
        return {"state": response.json().get("state", event)}

    async def count_new_commits(
        self, slug: str, cr_number: str, since: datetime
    ) -> int:
        response = await self._request(
            "GET", f"/repos/{slug}/pulls/{cr_number}/commits", params={"per_page": "100"}
        )
        count = 0
        for entry in response.json():
            authored = _parse_iso((entry.get("commit") or {}).get("author", {}).get("date"))
            if authored is not None and authored > since:
                count += 1
        return count

    async def merge_change_request(self, slug: str, cr_number: str) -> dict[str, Any]:
        response = await self._request("PUT", f"/repos/{slug}/pulls/{cr_number}/merge")
        merged_at = response.json().get("merged_at")
        if merged_at is None:
            state = await self.get_change_request_state(slug, cr_number)
            merged_at = state.get("merged_at")
        return {"merged": True, "merged_at": merged_at}

    async def get_issue_state(self, slug: str, issue_number: str) -> dict[str, Any]:
        response = await self._request("GET", f"/repos/{slug}/issues/{issue_number}")
        body = response.json()
        return {"state": body.get("state"), "closed_at": body.get("closed_at")}

    async def close_issue(self, slug: str, issue_number: str) -> dict[str, Any]:
        response = await self._request(
            "PATCH", f"/repos/{slug}/issues/{issue_number}", json_body={"state": "closed"}
        )
        body = response.json()
        return {"state": body.get("state"), "closed_at": body.get("closed_at")}

    async def aclose(self) -> None:
        await self._client.aclose()


class GitLabClient:
    """Drive disposable GitLab projects via the v4 API."""

    provider = "gitlab"

    def __init__(
        self,
        token: str,
        group: str,
        *,
        base_url: str = DEFAULT_GITLAB_API_BASE,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        headers: dict[str, str] = {}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self._group = group
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, transport=transport
        )

    @staticmethod
    def _project_path(slug: str) -> str:
        from urllib.parse import quote

        return quote(slug, safe="")

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, params: Any = None
    ) -> httpx.Response:
        response = await self._client.request(method, path, json=json_body, params=params)
        if response.status_code >= 400:
            raise ProviderApiError(
                method=method,
                url=str(response.request.url),
                status=response.status_code,
                detail=response.text[:500],
            )
        return response

    async def verify_credentials(self) -> dict[str, Any]:
        response = await self._request("GET", "/user")
        body = response.json()
        return {"login": body.get("username")}

    async def create_disposable_repo(self, name: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/projects",
            json_body={"name": name, "namespace_id": self._group, "visibility": "private"},
        )
        body = response.json()
        return {
            "slug": f"{self._group}/{body['path']}",
            "web_url": body["web_url"],
            "project_id": body["id"],
        }

    async def delete_repo(self, slug: str) -> bool:
        response = await self._client.request(
            "DELETE", f"/projects/{self._project_path(slug)}"
        )
        return response.status_code in (200, 202, 204)

    async def create_issue(
        self, slug: str, title: str, body: str, label: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/projects/{self._project_path(slug)}/issues",
            json_body={"title": title, "description": body, "labels": label},
        )
        body_json = response.json()
        return {
            "number": str(body_json["iid"]),
            "title": body_json.get("title"),
            "web_url": body_json.get("web_url"),
        }

    async def find_change_request_for_issue(
        self, slug: str, issue_number: str
    ) -> Optional[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/projects/{self._project_path(slug)}/merge_requests",
            params={"state": "all", "per_page": "50"},
        )
        reference = f"#{issue_number}"
        for mr in response.json():
            title = str(mr.get("title") or "")
            description = str(mr.get("description") or "")
            branch = str(mr.get("source_branch") or "")
            if (
                reference in title
                or reference in description
                or issue_number in branch
            ):
                return {
                    "number": str(mr["iid"]),
                    "title": title,
                    "branch": branch,
                    "state": mr.get("state"),
                    "opened_at": mr.get("created_at"),
                    "web_url": mr.get("web_url"),
                }
        return None

    async def get_change_request_state(self, slug: str, cr_number: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/projects/{self._project_path(slug)}/merge_requests/{cr_number}"
        )
        body = response.json()
        merged = body.get("state") == "merged"
        return {"merged": merged, "merged_at": body.get("merged_at")}

    async def post_review(
        self, slug: str, cr_number: str, comment: str, approve: bool
    ) -> dict[str, Any]:
        path = f"/projects/{self._project_path(slug)}/merge_requests/{cr_number}"
        if approve:
            response = await self._request("POST", f"{path}/approve")
            return {"state": "APPROVED", "approved": bool(response.json().get("approved"))}
        response = await self._request("POST", f"{path}/notes", json_body={"body": comment})
        return {"note_id": response.json().get("id")}

    async def count_new_commits(
        self, slug: str, cr_number: str, since: datetime
    ) -> int:
        response = await self._request(
            "GET",
            f"/projects/{self._project_path(slug)}/merge_requests/{cr_number}/commits",
            params={"per_page": "100"},
        )
        count = 0
        for entry in response.json():
            authored = _parse_iso(entry.get("authored_date"))
            if authored is not None and authored > since:
                count += 1
        return count

    async def merge_change_request(self, slug: str, cr_number: str) -> dict[str, Any]:
        response = await self._request(
            "PUT",
            f"/projects/{self._project_path(slug)}/merge_requests/{cr_number}/merge",
        )
        body = response.json()
        return {"merged": True, "merged_at": body.get("merged_at")}

    async def get_issue_state(self, slug: str, issue_number: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/projects/{self._project_path(slug)}/issues/{issue_number}"
        )
        body = response.json()
        return {"state": body.get("state"), "closed_at": body.get("closed_at")}

    async def close_issue(self, slug: str, issue_number: str) -> dict[str, Any]:
        response = await self._request(
            "PUT",
            f"/projects/{self._project_path(slug)}/issues/{issue_number}",
            json_body={"state_event": "close"},
        )
        body = response.json()
        return {"state": body.get("state"), "closed_at": body.get("closed_at")}

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Gateway client ───────────────────────────────────────────────────────────


class GatewayClient:
    """Read lifecycle evidence from the Gateway REST API (envelope-aware)."""

    def __init__(
        self,
        api_key: str,
        operator_token: str = "",
        *,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"}
        self._operator_token = operator_token
        if operator_token:
            headers["X-Operator-Token"] = operator_token
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, transport=transport
        )

    async def _get(self, path: str, params: Any = None) -> httpx.Response:
        return await self._client.get(path, params=params)

    @staticmethod
    def _data(response: httpx.Response) -> Optional[dict[str, Any]]:
        body = response.json()
        data = body.get("data")
        return data if isinstance(data, dict) else None

    async def health(self) -> bool:
        response = await self._client.get("/health")
        return response.status_code == 200

    async def list_afk_runs(self, repository: str) -> list[dict[str, Any]]:
        response = await self._get(
            "/api/v1/afk-outcomes/runs",
            params={"repository": repository, "limit": "100"},
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        items = (body.get("data") or {}).get("items") or []
        return [dict(item) for item in items]

    async def get_afk_run_detail(self, afk_run_id: str) -> Optional[dict[str, Any]]:
        response = await self._get(f"/api/v1/afk-outcomes/runs/{afk_run_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = self._data(response)
        return dict(data) if data else None

    async def list_execution_bindings(
        self, provider: str, repository_url: str, entity_number: str
    ) -> list[dict[str, Any]]:
        response = await self._get(
            "/api/v1/afk/executions",
            params={
                "provider": provider,
                "repository_url": repository_url,
                "entity_type": "change_request",
                "entity_number": entity_number,
            },
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        bindings = (body.get("data") or {}).get("bindings") or []
        return [dict(item) for item in bindings]

    async def get_closure_answer(
        self, provider: str, repository: str, external_id: str
    ) -> Optional[dict[str, Any]]:
        response = await self._get(
            "/api/v1/closure-relationships/issues/current",
            params={"provider": provider, "repository": repository, "external_id": external_id},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = self._data(response)
        return dict(data) if data else None

    async def get_reporting_detail(
        self, provider: str, repository_url: str, resource_type: str, resource_number: str
    ) -> Optional[dict[str, Any]]:
        response = await self._get(
            "/api/v1/reporting/resources/detail",
            params={
                "provider": provider,
                "repository_url": repository_url,
                "resource_type": resource_type,
                "resource_number": resource_number,
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = self._data(response)
        return dict(data) if data else None

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Scenario ─────────────────────────────────────────────────────────────────


@dataclass
class ScenarioResult:
    """Machine-checkable outcome of one provider's lifecycle scenario."""

    provider: str
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    passed: bool = False


def _repo_name(config: E2EConfig, provider: str) -> str:
    return f"{config.repo_prefix}-{provider}-{config.repo_suffix}"


async def run_provider_scenario(
    provider: str,
    config: E2EConfig,
    provider_client: ProviderClient,
    gateway: GatewayClient,
    evidence: EvidenceRecorder,
    log: Callable[[str], None] = print,
) -> ScenarioResult:
    """Drive one full lifecycle against a disposable repository and record evidence.

    Every step is bounded: provider/async conditions are polled within
    ``config.poll_timeout_seconds`` and failures record actionable diagnostics
    (attempts, elapsed, last observation) instead of hanging.  Cleanup always
    runs — including on failure paths — unless ``config.keep_repos``.
    """
    result = ScenarioResult(provider=provider)
    name = _repo_name(config, provider)
    slug: Optional[str] = None
    issue_number: Optional[str] = None
    cr_number: Optional[str] = None
    repository_identity = ""

    async def run_step(
        step_name: str, categories: Sequence[str], coro: Callable[[], Awaitable[dict]]
    ) -> bool:
        try:
            detail = await coro()
        except StepFailure as exc:
            detail = exc.detail
        except ProviderApiError as exc:
            detail = {
                "error": "provider-api",
                "method": exc.method,
                "url": exc.url,
                "status": exc.status,
                "detail": exc.detail,
            }
        except Exception as exc:  # never crash the harness mid-scenario
            detail = {"error": "unexpected", "message": redact(str(exc), evidence._secrets)}
        else:
            for category in categories:
                evidence.check(category, "passed", detail)
            evidence.record(step_name, "passed", detail)
            result.steps.append({"step": step_name, "status": "passed"})
            return True
        for category in categories:
            evidence.check(category, "failed", detail)
        evidence.record(step_name, "failed", detail)
        result.steps.append(
            {"step": step_name, "status": "failed", "error": detail.get("error")}
        )
        log(f"  [FAIL] {provider} {step_name}: {detail.get('error')}")
        return False

    # ── Step coroutines ──────────────────────────────────────────────────────

    async def disposable_repo() -> dict:
        nonlocal slug, repository_identity
        log(f"  creating disposable repository {name!r} ...")
        repo = await provider_client.create_disposable_repo(name)
        slug = repo["slug"]
        identity = normalize_repository_url(repo["web_url"])
        if identity is None:
            raise _fail("repository-identity", web_url=repo.get("web_url"))
        repository_identity = identity
        return {
            "slug": slug,
            "web_url": repo.get("web_url"),
            "identity": repository_identity,
            "provider": provider,
        }

    async def afk_trigger() -> dict:
        nonlocal issue_number
        log("  creating AFK-labelled issue (the AFK trigger) ...")
        issue = await provider_client.create_issue(
            slug or "",
            f"[AFK E2E] Implement feature for {name}",
            (
                "This issue is part of the opt-in AFK lifecycle E2E run. "
                "Implement the requested change and open a change request "
                "that references this issue."
            ),
            config.afk_label,
        )
        issue_number = issue["number"]
        return {
            "number": issue_number,
            "title": issue.get("title"),
            "label": config.afk_label,
            "web_url": issue.get("web_url"),
        }

    async def develop_execution() -> dict:
        nonlocal cr_number
        what = f"{provider} develop change request referencing issue #{issue_number}"
        try:
            change_request = await poll_until(
                lambda: provider_client.find_change_request_for_issue(slug or "", issue_number or ""),
                interval=config.poll_interval_seconds,
                timeout=config.poll_timeout_seconds,
                what=what,
                report=lambda a, e: log(f"  [poll] {a} attempt(s), {e}s — {what}"),
            )
        except PollTimeout as exc:
            raise _fail(
                "poll-timeout",
                what=exc.what,
                attempts=exc.attempts,
                timeout_seconds=config.poll_timeout_seconds,
                observed=(
                    "no change request referencing the issue appeared — is the "
                    "EDA gateway / AWX develop loop wired for this org/group?"
                ),
            ) from exc
        if not isinstance(change_request, dict):
            raise _fail("unexpected", message="provider returned a malformed change request")
        cr_number = change_request["number"]
        log(f"  develop change request #{cr_number} observed: {change_request.get('title')}")
        return {
            "number": cr_number,
            "title": change_request.get("title"),
            "branch": change_request.get("branch"),
            "opened_at": change_request.get("opened_at"),
            "web_url": change_request.get("web_url"),
            "state": change_request.get("state"),
        }

    async def awx_execution_bindings() -> dict:
        what = f"{provider} execution bindings for change_request:{cr_number}"
        try:
            bindings = await poll_until(
                lambda: _nonempty_bindings(),
                interval=config.poll_interval_seconds,
                timeout=config.poll_timeout_seconds,
                what=what,
                report=lambda a, e: log(f"  [poll] {a} attempt(s), {e}s — {what}"),
            )
        except PollTimeout as exc:
            raise _fail(
                "poll-timeout",
                what=exc.what,
                attempts=exc.attempts,
                timeout_seconds=config.poll_timeout_seconds,
                observed="no execution bindings observed — is the AWX integration "
                "writing bindings for this environment?",
            ) from exc
        log(f"  {len(bindings)} execution binding(s) observed")
        return {"bindings": bindings}

    async def _nonempty_bindings() -> Optional[list[dict[str, Any]]]:
        bindings = await gateway.list_execution_bindings(
            provider, repository_identity, cr_number or ""
        )
        return bindings or None

    async def review_request() -> dict:
        log("  posting changes-requested review (review trigger) ...")
        review = await provider_client.post_review(
            slug or "", cr_number or "", "AFK E2E review: please address the review comments.", False
        )
        return {"review": review}

    async def fix_re_review() -> dict:
        since = datetime.now(timezone.utc)  # noqa: UP017
        what = f"{provider} fix commit after review on change_request:{cr_number}"
        try:
            await poll_until(
                lambda: _fix_seen(since),
                interval=config.poll_interval_seconds,
                timeout=config.poll_timeout_seconds,
                what=what,
                report=lambda a, e: log(f"  [poll] {a} attempt(s), {e}s — {what}"),
            )
        except PollTimeout as exc:
            raise _fail(
                "poll-timeout",
                what=exc.what,
                attempts=exc.attempts,
                timeout_seconds=config.poll_timeout_seconds,
                observed="no new commit after the review — the develop loop did not respond",
            ) from exc
        approval = await provider_client.post_review(
            slug or "", cr_number or "", "AFK E2E re-review: changes look good.", True
        )
        log("  fix observed and re-review approved")
        return {"approval": approval}

    async def _fix_seen(since: datetime) -> Optional[int]:
        count = await provider_client.count_new_commits(slug or "", cr_number or "", since)
        return count if count > 0 else None

    async def merge() -> dict:
        log("  merging the change request ...")
        merged = await provider_client.merge_change_request(slug or "", cr_number or "")
        state = await provider_client.get_change_request_state(slug or "", cr_number or "")
        return {
            "merged": True,
            "merged_at": merged.get("merged_at") or state.get("merged_at"),
            "mechanism": "harness",
        }

    async def issue_closure() -> dict:
        what = f"{provider} issue #{issue_number} closure after merge"

        async def closed_state() -> Optional[dict[str, Any]]:
            state = await provider_client.get_issue_state(slug or "", issue_number or "")
            return state if state.get("state") == "closed" else None

        mechanism = "provider"
        try:
            state = await poll_until(
                closed_state,
                interval=config.poll_interval_seconds,
                timeout=config.poll_timeout_seconds,
                what=what,
                report=lambda a, e: log(f"  [poll] {a} attempt(s), {e}s — {what}"),
            )
        except PollTimeout as exc:
            log(f"  [note] {what}: provider auto-close not observed; closing via harness")
            state = await provider_client.close_issue(slug or "", issue_number or "")
            mechanism = "harness"
            state = {
                **state,
                "note": (
                    f"provider auto-close not observed within {config.poll_timeout_seconds}s "
                    f"({exc.attempts} attempts); the harness closed the issue directly"
                ),
            }
        return {
            "closed": True,
            "closed_at": state.get("closed_at"),
            "mechanism": mechanism,
            "note": state.get("note"),
        }

    async def gateway_afk_run() -> dict:
        what = f"gateway AFK run containing change_request:{cr_number}"

        async def candidate() -> Optional[dict[str, Any]]:
            runs = await gateway.list_afk_runs(repository_identity)
            for run in runs:
                detail = await gateway.get_afk_run_detail(run["afk_run_id"])
                if detail is None:
                    continue
                change_requests = detail.get("change_requests") or []
                if any(
                    str(item.get("external_id")) == str(cr_number)
                    for item in change_requests
                ):
                    return detail
            return None

        try:
            detail = await poll_until(
                candidate,
                interval=config.poll_interval_seconds,
                timeout=config.poll_timeout_seconds,
                what=what,
                report=lambda a, e: log(f"  [poll] {a} attempt(s), {e}s — {what}"),
            )
        except PollTimeout as exc:
            raise _fail(
                "poll-timeout",
                what=exc.what,
                attempts=exc.attempts,
                timeout_seconds=config.poll_timeout_seconds,
                observed="no AFK run with this change request — is the AFK Outcome "
                "Consumer / backfill reconstructing runs in this environment?",
            ) from exc
        outcome = detail.get("outcome") or {}
        sessions = detail.get("sessions") or []
        usage = detail.get("usage") or {}
        sessions_evidence = {
            "run_sessions": [
                {
                    "session_id": item.get("session_id"),
                    "external_session_id": item.get("external_session_id"),
                    "agent": item.get("agent"),
                    "inferred": item.get("inferred"),
                    "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                }
                for item in sessions
            ]
        }
        usage_evidence = {
            "active_tokens": usage.get("active_tokens"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "message_count": usage.get("message_count"),
        }
        evidence.check("sessions", "passed", sessions_evidence)
        evidence.check("usage", "passed", usage_evidence)
        return {
            "afk_run_id": detail.get("afk_run_id"),
            "run_status": (detail.get("run") or {}).get("status"),
            "outcome_status": outcome.get("status"),
            "merge_event_id": outcome.get("merge_event_id"),
            "merged_at": outcome.get("merged_at"),
            "resolved_issue_ids": outcome.get("resolved_issue_ids"),
            "sessions": sessions_evidence,
            "usage": usage_evidence,
        }

    async def closure_projection() -> dict:
        what = f"{provider} closure projection for issue:{issue_number}"
        try:
            answer = await poll_until(
                lambda: gateway.get_closure_answer(
                    provider, repository_identity, issue_number or ""
                ),
                interval=config.poll_interval_seconds,
                timeout=config.poll_timeout_seconds,
                what=what,
                report=lambda a, e: log(f"  [poll] {a} attempt(s), {e}s — {what}"),
            )
        except PollTimeout as exc:
            raise _fail(
                "poll-timeout",
                what=exc.what,
                attempts=exc.attempts,
                timeout_seconds=config.poll_timeout_seconds,
                observed="no closure episode observed — has the closure projection recomputed?",
            ) from exc
        episode = answer.get("episode") or {}
        return {
            "episode_status": episode.get("status"),
            "attributed_change_request": episode.get("change_request_external_id"),
            "derived_at": answer.get("derived_at"),
            "resolver_version": answer.get("resolver_version"),
        }

    # ── Sequence ─────────────────────────────────────────────────────────────

    steps: list[tuple[str, Sequence[str], Callable[[], Awaitable[dict]]]] = [
        ("disposable-repo", ("repository",), disposable_repo),
        ("afk-trigger", ("issue",), afk_trigger),
        ("develop-execution", ("change_request",), develop_execution),
        ("awx-execution-bindings", ("awx_jobs",), awx_execution_bindings),
        ("review-request", (), review_request),
        ("fix-re-review", (), fix_re_review),
        ("merge", ("merge",), merge),
        ("issue-closure", ("closure",), issue_closure),
        ("gateway-afk-run", (), gateway_afk_run),
        ("closure-projection", ("closure",), closure_projection),
    ]

    try:
        for step_name, categories, coro in steps:
            if not await run_step(step_name, categories, coro):
                break
        for category in REQUIRED_CHECK_CATEGORIES:
            if category not in evidence.checks:
                evidence.check(
                    category,
                    "not_attempted",
                    {"note": "an earlier step failed; this evidence was not collected"},
                )
    finally:
        if slug is not None:
            if config.keep_repos:
                result.cleanup = {"slug": slug, "kept": True, "note": "--keep-repos"}
                evidence.record("cleanup", "skipped", result.cleanup)
                log(f"  [note] keeping disposable repository {slug!r} (--keep-repos)")
            else:
                try:
                    deleted = await provider_client.delete_repo(slug)
                except Exception as exc:  # noqa: BLE001 - cleanup must not crash
                    deleted = False
                    log(f"  [WARN] cleanup failed for {slug!r}: {redact(str(exc), evidence._secrets)}")
                result.cleanup = {"slug": slug, "deleted": deleted}
                evidence.record("cleanup", "passed" if deleted else "failed", result.cleanup)
                log(f"  cleanup for {slug!r}: {'deleted' if deleted else 'FAILED — manual cleanup required'}")

    result.checks = evidence.checks
    result.passed = all(
        result.checks[category].get("status") == "passed"
        for category in REQUIRED_CHECK_CATEGORIES
    )
    log(
        f"  {provider} scenario: {'PASS' if result.passed else 'FAIL'} "
        f"({sum(1 for c in result.checks.values() if c.get('status') == 'passed')}/"
        f"{len(REQUIRED_CHECK_CATEGORIES)} evidence categories passed)"
    )
    return result


# ── Preflight ────────────────────────────────────────────────────────────────


async def preflight(
    provider: str,
    config: E2EConfig,
    provider_client: ProviderClient,
    gateway: GatewayClient,
    evidence: EvidenceRecorder,
    log: Callable[[str], None] = print,
) -> bool:
    """Verify credentials, provider reachability, and Gateway connectivity."""
    ok = True
    try:
        identity = await provider_client.verify_credentials()
        evidence.record("preflight", "passed", {"provider": provider, "login": identity.get("login")})
        log(f"  [{provider}] credentials valid (authenticated as {identity.get('login')!r})")
    except Exception as exc:  # noqa: BLE001 - preflight must degrade gracefully
        ok = False
        evidence.record(
            "preflight", "failed", {"provider": provider, "error": redact(str(exc), evidence._secrets)}
        )
        log(f"  [FAIL] {provider} credentials check failed: {redact(str(exc), evidence._secrets)}")
    try:
        reachable = await gateway.health()
    except Exception as exc:  # noqa: BLE001
        reachable = False
        log(f"  [FAIL] gateway health check failed: {redact(str(exc), evidence._secrets)}")
    evidence.record("preflight", "passed" if reachable else "failed", {"gateway_health": reachable})
    if not reachable:
        ok = False
        log(f"  [FAIL] gateway at {config.gateway_base_url!r} is not reachable")
    return ok


# ── Main ─────────────────────────────────────────────────────────────────────


def _build_clients(
    provider: str, config: E2EConfig
) -> tuple[ProviderClient, GatewayClient]:
    if provider == "github":
        provider_client: ProviderClient = GitHubClient(
            config.github_token, config.github_org, base_url=config.github_api_base
        )
    else:
        provider_client = GitLabClient(
            config.gitlab_token, config.gitlab_group, base_url=config.gitlab_api_base
        )
    gateway = GatewayClient(
        config.gateway_api_key,
        operator_token=config.operator_token,
        base_url=config.gateway_base_url,
    )
    return provider_client, gateway


async def _run(config: E2EConfig, log: Callable[[str], None]) -> int:
    evidence = EvidenceRecorder(config.evidence_dir, secrets=config.secret_values())
    for variable in (
        "AFK_E2E_GITHUB_TOKEN",
        "AFK_E2E_GITLAB_TOKEN",
        "AFK_E2E_GATEWAY_API_KEY",
        "AFK_E2E_OPERATOR_TOKEN",
    ):
        value = os.environ.get(variable, "")
        if value.strip():
            evidence.note_secret(variable)

    log("AFK Outcomes real-provider lifecycle E2E (issue #578)")
    log(f"providers:      {', '.join(config.providers)}")
    log(f"gateway:        {config.gateway_base_url}")
    log(f"evidence dir:   {evidence.path}")
    log(f"mode:           {'dry-run' if config.dry_run else 'live'}")
    log(f"poll:           interval={config.poll_interval_seconds}s timeout={config.poll_timeout_seconds}s")
    log(f"afk label:      {config.afk_label!r}")

    results: dict[str, ScenarioResult] = {}
    preflight_ok = True
    for provider in config.providers:
        log(f"\n=== {provider} ===")
        provider_client, gateway = _build_clients(provider, config)
        try:
            ok = await preflight(provider, config, provider_client, gateway, evidence, log)
            preflight_ok = preflight_ok and ok
            if config.dry_run:
                log("  [dry-run] no resources will be created; preflight only")
                continue
            if not ok:
                log(f"  [FAIL] skipping {provider} scenario — preflight failed")
                continue
            results[provider] = await run_provider_scenario(
                provider, config, provider_client, gateway, evidence, log
            )
        finally:
            await provider_client.aclose()
            await gateway.aclose()

    overall = "fail"
    if config.dry_run:
        overall = "pass" if preflight_ok else "fail"
    elif results:
        overall = "pass" if all(result.passed for result in results.values()) else "fail"

    if len(config.providers) > 1:
        category_sets = {
            name: set(result.checks) for name, result in results.items()
        }
        equivalent = (
            len(category_sets) == len(config.providers)
            and len({frozenset(categories) for categories in category_sets.values()}) == 1
        )
        evidence.note(
            "cross_provider",
            {
                "providers": {
                    name: {
                        "passed": result.passed,
                        "categories": sorted(result.checks),
                    }
                    for name, result in results.items()
                },
                "equivalent_evidence_categories": equivalent,
            },
        )
        log(f"cross-provider evidence equivalence: {equivalent}")

    summary_path = evidence.finish(overall)
    log(f"\noverall: {overall.upper()}")
    log(f"evidence: {summary_path}")
    return 0 if overall == "pass" else 1


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point — resolve config, run preflight + scenarios, return exit code.

    Exit codes: 0 = all checks passed; 1 = at least one check failed; 2 =
    configuration error (missing/invalid environment configuration).
    """
    try:
        config = load_config(os.environ, argv)
    except HarnessConfigError as exc:
        print(f"[FATAL] configuration error: {exc}")
        print(
            "Provide the required AFK_E2E_* environment variables — see "
            "docs/afk-e2e-validation.md.  No secrets were printed."
        )
        return 2

    secrets = config.secret_values()

    def log(message: str) -> None:
        print(redact(message, secrets))

    return asyncio.run(_run(config, log))


if __name__ == "__main__":
    sys.exit(main())
