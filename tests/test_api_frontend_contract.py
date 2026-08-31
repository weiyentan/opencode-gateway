"""API-to-frontend contract tests (issues #610–#614).

Verifies that the backend API response schemas
(``app/core/schemas/afk.py``) match what the frontend change-request
adapters (``frontend/adapters/change_request_adapters.js``) expect, so
contract drift between backend and frontend is caught in CI instead of
silently breaking the change-request summary and detail views.

Three facts are checked for every contract surface the adapters consume:

1. **Field presence** — every canonical field the adapters read exists on
   the backend Pydantic model.  A removed or renamed backend field would
   silently degrade the UI.
2. **Type compatibility** — the backend field's JSON type (string, number,
   boolean, object, array) is what the adapter's JS actually handles
   (e.g. counts are numeric, identities are strings, ``inferred`` is a
   boolean, ``total_estimated_cost_usd`` is numeric).
3. **Fixture conformance** — the committed frontend fixtures validate
   against the backend schemas where they claim to follow the contract.
   The summary-list fixtures and the captured run-detail JSON fixtures
   validate strictly.  The change-request *detail* fixtures additionally
   exercise the adapter's documented defensive fallbacks (flat
   ``awx_job_id``, string ``job_template_id``, string ``merge_state``) —
   those are the adapter's resilience surface, not backend contract
   fields, so the test pins the canonical subset against the schema and
   pins the defensive surface against the adapter's own field vocabulary.

Run with ``pytest tests/test_api_frontend_contract.py`` — no database, no
network, no AWX.  The JS fixtures are loaded from disk via ``node`` (the
same mechanism ``tests/test_js_frontend.py`` uses); the schema imports are
pure Pydantic.
"""

from __future__ import annotations

import json
import subprocess
import types
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from app.core.schemas.afk import (
    AWXJobIdentity,
    ChangeRequestDetail,
    ChangeRequestDetailSummary,
    ChangeRequestExecutionCounts,
    ChangeRequestExecutionItem,
    ChangeRequestLinkedRun,
    ChangeRequestMergeState,
    ChangeRequestSummaryRow,
    ChangeRequestTimeline,
    ChangeRequestTimelineEvent,
    RunDetail,
    SessionLink,
    UsageAggregate,
)
from app.core.schemas.usage import PaginatedResponse

REPO_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_DIR / "frontend" / "fixtures"

# ``types.UnionType`` (the PEP 604 ``X | Y`` origin) only exists on Python
# 3.10+; build the union-origin tuple dynamically so this module imports and
# runs on Python 3.9.
_UNION_TYPES = (Union,)
if hasattr(types, "UnionType"):
    _UNION_TYPES = (Union, types.UnionType)

# ── Fixture registry (deterministic, committed) ──────────────────────────────

SUMMARY_FIXTURES = [
    "change_request_summary_github.js",
    "change_request_summary_gitlab.js",
]

# The change-request detail fixtures (from both the summary modules' legacy
# ``buildDetail`` and the dedicated detail modules).  They follow the #611
# composite detail contract and also carry the adapter's defensive shapes.
DETAIL_FIXTURES = [
    "change_request_summary_github.js",
    "change_request_summary_gitlab.js",
    "change_request_detail_github.js",
    "change_request_detail_gitlab.js",
]

# Captured API responses (status/data envelope) — the strongest ground truth:
# the backend actually produced these shapes.
RUN_DETAIL_JSON_FIXTURES = [
    "github_afk_run_detail.json",
    "gitlab_afk_run_detail.json",
    "github_ambiguous_detail.json",
    "github_parked_detail.json",
    "gitlab_unresolved_detail.json",
]

# The adapter's defensive fallback aliases (never sent by the backend) that
# the detail fixtures use to exercise adapter resilience.  These are NOT
# contract fields — they are the adapter's tolerance surface.
_EXECUTION_DEFENSIVE_ALIASES = {
    "awx_job_id",       # flat AWX job id (provenance-timeline shape)
    "status",           # provenance-timeline status (backend sends ``outcome``)
    "phase",            # provenance-timeline purpose alias (develop/review)
    "input_tokens",     # flat token aliases (backend sends total_*)
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",         # flat cost alias
    "job_template_id",  # flat job-template alias (backend nests under awx_job)
}

# Frontend-only renderer-hint fields carried by the detail fixtures.  The
# renderer reads them (``item.summary || item.description``), but the backend
# never sends them — they are display hints, not contract fields.
_TIMELINE_RENDERER_HINTS = {"summary", "description"}


# ── JS fixture loader (same mechanism as tests/test_js_frontend.py) ──────────


def _load_js_builder(fixture_name: str, builder: str) -> dict:
    """Require ``frontend/fixtures/<fixture_name>`` and call ``<builder>()``.

    Returns the parsed JSON value.  Failures (missing module, non-zero exit)
    surface with the fixture name in the assertion message.
    """
    module_path = FIXTURES_DIR / fixture_name
    script = (
        "const m = require(process.argv[1]);"
        f"process.stdout.write(JSON.stringify(m.{builder}()));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(module_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"node failed to load fixture {fixture_name}: "
        f"{result.stderr[-1000:]}"
    )
    return json.loads(result.stdout)


def _load_json_fixture(fixture_name: str) -> dict:
    """Read a committed JSON fixture (``{status, data}`` envelope)."""
    return json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))


# ── Schema introspection helpers ─────────────────────────────────────────────


def _json_type_tag(annotation: Any) -> str:
    """Map a backend schema annotation to the JSON type the adapter handles.

    * ``Optional[X]`` collapses to ``X``.
    * ``Decimal`` → ``number`` (the serializer emits a numeric string the
      adapter parses with ``Number()``).
    * ``datetime`` → ``string`` (ISO-8601, parsed with ``new Date()``).
    * Pydantic models → ``object``.
    """
    origin = get_origin(annotation)
    if origin in _UNION_TYPES:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if not args:
            return "null"
        if len(args) == 1:
            return _json_type_tag(args[0])
        return "any"  # multi-type union — permissive
    if origin is list:
        return "array"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    if annotation in (str, bytes):
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float, Decimal):
        return "number"
    if annotation is datetime:
        return "string"
    return "any"


def _assert_vocabulary(model: type[BaseModel], vocabulary: dict[str, str], label: str) -> None:
    """Assert every canonical adapter field exists on the backend model with a
    compatible JSON type."""
    # Build resolved annotations from Pydantic's own field metadata.
    # get_type_hints() fails on Python 3.9 when the schema uses X | None
    # syntax (PEP 604) behind from __future__ import annotations.
    resolved_annotations = {
        name: field.annotation
        for name, field in model.model_fields.items()
    }
    missing = sorted(f for f in vocabulary if f not in model.model_fields)
    assert not missing, (
        f"{label}: backend schema {model.__name__} is missing adapter-required "
        f"field(s): {missing}"
    )
    mismatched = []
    for field, expected in sorted(vocabulary.items()):
        actual = _json_type_tag(resolved_annotations[field])
        if actual != expected:
            mismatched.append(
                f"{field}: schema type {actual} != adapter expectation {expected}"
            )
    assert not mismatched, (
        f"{label}: type drift on backend schema {model.__name__}:\n"
        + "\n".join(f"  - {m}" for m in mismatched)
    )


def _assert_keys_equal(dumped: dict, raw: dict, label: str) -> None:
    """Assert a serialized schema dict carries exactly the raw fixture keys."""
    assert set(dumped.keys()) == set(raw.keys()), (
        f"{label}: fixture keys {sorted(raw)} do not match backend "
        f"serialization {sorted(dumped)}"
    )


def _assert_keys_subset(dumped: dict, raw: dict, label: str) -> None:
    """Assert the fixture block keys are a subset of the serialized schema
    keys — the fixture may abbreviate defaulted fields, but every fixture
    field must be a real backend contract field."""
    unknown = set(raw.keys()) - set(dumped.keys())
    assert not unknown, (
        f"{label}: fixture fields {sorted(unknown)} are not produced by the "
        f"backend schema (serialized keys: {sorted(dumped)})"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Canonical adapter vocabulary — every field the frontend reads from the API
# ═══════════════════════════════════════════════════════════════════════════

_SUMMARY_ROW_VOCABULARY = {
    "provider": "string",
    "repository": "string",
    "external_id": "string",
    "resource_type": "string",
    "provider_state": "string",
    "automation_state": "string",
    "total_estimated_cost_usd": "number",
    "latest_linked_activity": "string",
    "provider_state_observed_at": "string",
    "executions": "object",
}

_EXECUTION_COUNTS_VOCABULARY = {
    "total": "number",
    "running": "number",
    "completed": "number",
    "failed": "number",
    "cancelled": "number",
}

_DETAIL_SUMMARY_VOCABULARY = {
    **_SUMMARY_ROW_VOCABULARY,
    "title": "string",
    "merged_at": "string",
    "provider_state_observed_at": "string",
}

_AWX_JOB_VOCABULARY = {
    "job_id": "string",
    "job_template_id": "number",
}

_EXECUTION_ITEM_VOCABULARY = {
    "awx_job": "object",
    "external_session_id": "string",
    "session_id": "string",
    "afk_run_id": "string",
    "outcome": "string",
    "purpose": "string",
    "trigger_type": "string",
    "source_event_id": "string",
    "branch": "string",
    "title": "string",
    "started_at": "string",
    "finished_at": "string",
    "duration_seconds": "number",
    "failure_reason": "string",
    "failure_summary": "string",
    "total_input_tokens": "number",
    "total_output_tokens": "number",
    "total_cache_read_tokens": "number",
    "total_cache_write_tokens": "number",
    "estimated_cost_usd": "number",
}

_LINKED_RUN_VOCABULARY = {
    "afk_run_id": "string",
    "provider": "string",
    "status": "string",
    "title": "string",
    "started_at": "string",
    "finished_at": "string",
    "outcome_status": "string",
    "first_seen_at": "string",
    "last_seen_at": "string",
    "link_sources": "array",
}

_SESSION_LINK_VOCABULARY = {
    "session_id": "string",
    "external_session_id": "string",
    "started_at": "string",
    "finished_at": "string",
    "inferred": "boolean",
    "agent": "string",
    "message_count": "number",
    "total_input_tokens": "number",
    "total_output_tokens": "number",
    "total_cache_read_tokens": "number",
    "total_cache_write_tokens": "number",
    "total_estimated_cost_usd": "number",
    "parent_session_id": "string",
}

_USAGE_AGGREGATE_VOCABULARY = {
    "active_tokens": "number",
    "input_tokens": "number",
    "output_tokens": "number",
    "cache_read_tokens": "number",
    "cache_write_tokens": "number",
    "estimated_cost_usd": "number",
    "message_count": "number",
    "session_count": "number",
}

_MERGE_STATE_VOCABULARY = {
    "state": "string",
    "merged_at": "string",
}

_TIMELINE_EVENT_VOCABULARY = {
    "event_type": "string",
    "occurred_at": "string",
    "observed_via": "string",
    "snapshot_at": "string",
    "actor": "string",
}

_DETAIL_TOP_LEVEL_VOCABULARY = {
    "change_request": "object",
    "afk_runs": "array",
    "executions": "array",
    "sessions": "array",
    "usage": "object",
    "total_estimated_cost_usd": "number",
    "merge_state": "object",
    "timeline": "object",
}


# ═══════════════════════════════════════════════════════════════════════════
# Summary contract (issue #610)
# ═══════════════════════════════════════════════════════════════════════════


class TestSummaryContract:
    """The change-request summary row contract and its fixtures."""

    @pytest.mark.parametrize(
        "fixture_name", SUMMARY_FIXTURES, ids=lambda p: Path(p).stem
    )
    def test_summary_fixture_validates_against_backend_schema(self, fixture_name):
        """The committed summary-list fixtures are producible by the backend."""
        payload = _load_js_builder(fixture_name, "buildSummaryList")
        parsed = PaginatedResponse[ChangeRequestSummaryRow].model_validate(payload)
        items = parsed.model_dump(mode="json")["items"]
        assert parsed.total == payload["total"]
        assert parsed.limit == payload["limit"]
        assert parsed.offset == payload["offset"]
        assert len(items) == len(payload["items"])
        for raw, dumped in zip(payload["items"], items):
            _assert_keys_equal(dumped, raw, f"{fixture_name}.summary row")
            assert set(dumped["executions"].keys()) == set(
                ChangeRequestExecutionCounts.model_fields
            ), f"{fixture_name}: executions aggregate keys drift"

    def test_summary_row_adapter_vocabulary_present_in_schema(self):
        """The canonical fields the #612 summary adapter reads exist on the
        backend row model with compatible types."""
        _assert_vocabulary(
            ChangeRequestSummaryRow, _SUMMARY_ROW_VOCABULARY, "summary row"
        )
        _assert_vocabulary(
            ChangeRequestExecutionCounts,
            _EXECUTION_COUNTS_VOCABULARY,
            "execution counts",
        )

    def test_summary_pagination_envelope_present_in_schema(self):
        """The ``{items, total, limit, offset}`` list envelope the adapter
        unwraps is the backend's paginated response."""
        fields = PaginatedResponse[ChangeRequestSummaryRow].model_fields
        for name, expected in {
            "items": "array",
            "total": "number",
            "limit": "number",
            "offset": "number",
        }.items():
            assert _json_type_tag(fields[name].annotation) == expected, name


# ═══════════════════════════════════════════════════════════════════════════
# Detail contract (issue #611)
# ═══════════════════════════════════════════════════════════════════════════


class TestDetailContract:
    """The change-request detail composite contract and its fixtures."""

    @pytest.mark.parametrize(
        "fixture_name", DETAIL_FIXTURES, ids=lambda p: Path(p).stem
    )
    def test_detail_fixture_canonical_blocks_validate(self, fixture_name):
        """The canonical blocks of every detail fixture (change_request
        summary, sessions, usage, timeline events, object merge_state)
        validate against the backend schemas.

        The execution entries deliberately mix the canonical ``awx_job``
        object shape with the adapter's defensive flat shapes
        (``awx_job_id`` / string ``job_template_id``), so they are checked
        for field-vocabulary containment rather than strict validation.
        """
        detail = _load_js_builder(fixture_name, "buildDetail")

        # change_request block is the detail summary contract.  The fixture
        # may abbreviate defaulted fields (e.g. executions counts), so every
        # fixture key must exist on the schema, not the reverse.
        parsed_summary = ChangeRequestDetailSummary.model_validate(
            detail["change_request"]
        )
        _assert_keys_subset(
            parsed_summary.model_dump(mode="json"),
            detail["change_request"],
            f"{fixture_name}.change_request",
        )

        # Sessions validate strictly against SessionLink (fixture may omit
        # defaulted fields such as ``parent_session_id``).
        for session in detail.get("sessions", []):
            parsed = SessionLink.model_validate(session)
            _assert_keys_subset(
                parsed.model_dump(mode="json"), session, f"{fixture_name}.session"
            )

        # Usage aggregate (when present) validates strictly.
        if detail.get("usage") is not None:
            parsed = UsageAggregate.model_validate(detail["usage"])
            _assert_keys_equal(
                parsed.model_dump(mode="json"), detail["usage"], f"{fixture_name}.usage"
            )

        # Timeline events validate strictly (the renderer only reads the
        # schema's fields; the fixture may omit defaulted fields such as
        # ``snapshot_at`` and may carry renderer-hint fields such as
        # ``summary``).  Every fixture field must be a real contract field
        # or a documented renderer hint.
        timeline = detail.get("timeline") or {}
        for event in timeline.get("events", []):
            parsed = ChangeRequestTimelineEvent.model_validate(event)
            unknown = set(event.keys()) - set(parsed.model_dump(mode="json").keys())
            unknown -= _TIMELINE_RENDERER_HINTS
            assert not unknown, (
                f"{fixture_name}.timeline.event: fixture field(s) "
                f"{sorted(unknown)} are not produced by the backend schema"
            )

        # merge_state: the object shape validates strictly; the string
        # shorthand is the adapter's documented defensive fallback.
        merge_state = detail.get("merge_state")
        if isinstance(merge_state, dict):
            ChangeRequestMergeState.model_validate(merge_state)

        # Execution entries: every field either is a canonical backend field
        # or is a documented defensive alias the adapter tolerates.
        schema_fields = set(ChangeRequestExecutionItem.model_fields)
        for execution in detail.get("executions", []):
            unknown = set(execution.keys()) - schema_fields - _EXECUTION_DEFENSIVE_ALIASES
            assert not unknown, (
                f"{fixture_name}: execution field(s) {sorted(unknown)} are "
                f"neither backend contract fields nor documented adapter "
                f"defensive aliases"
            )
        # At least one execution carries the canonical awx_job object so the
        # fixture exercises the real contract shape too.
        assert any("awx_job" in e for e in detail.get("executions", [])), (
            f"{fixture_name}: no execution uses the canonical awx_job shape"
        )

    def test_detail_top_level_adapter_vocabulary_present_in_schema(self):
        """The composite fields the #612 detail adapter reads exist on the
        backend ChangeRequestDetail model with compatible types."""
        _assert_vocabulary(
            ChangeRequestDetail, _DETAIL_TOP_LEVEL_VOCABULARY, "detail top level"
        )

    def test_detail_summary_block_vocabulary_present_in_schema(self):
        _assert_vocabulary(
            ChangeRequestDetailSummary,
            _DETAIL_SUMMARY_VOCABULARY,
            "detail summary block",
        )

    def test_execution_item_vocabulary_present_in_schema(self):
        _assert_vocabulary(
            ChangeRequestExecutionItem,
            _EXECUTION_ITEM_VOCABULARY,
            "execution item",
        )
        _assert_vocabulary(AWXJobIdentity, _AWX_JOB_VOCABULARY, "AWX job identity")

    def test_linked_run_vocabulary_present_in_schema(self):
        _assert_vocabulary(
            ChangeRequestLinkedRun, _LINKED_RUN_VOCABULARY, "linked run"
        )

    def test_session_and_usage_vocabulary_present_in_schema(self):
        _assert_vocabulary(SessionLink, _SESSION_LINK_VOCABULARY, "session link")
        _assert_vocabulary(
            UsageAggregate, _USAGE_AGGREGATE_VOCABULARY, "usage aggregate"
        )

    def test_merge_state_and_timeline_vocabulary_present_in_schema(self):
        _assert_vocabulary(
            ChangeRequestMergeState, _MERGE_STATE_VOCABULARY, "merge state"
        )
        _assert_vocabulary(
            ChangeRequestTimelineEvent,
            _TIMELINE_EVENT_VOCABULARY,
            "timeline event",
        )
        # The timeline envelope the adapter flattens is ``{events: [...]}``.
        _assert_vocabulary(
            ChangeRequestTimeline, {"events": "array"}, "timeline envelope"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Captured API responses (strongest ground truth)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunDetailJsonFixtures:
    """The committed JSON fixtures are captured API responses: they validate
    strictly against the backend RunDetail schema and use the
    ``{status: 'ok', data: ...}`` envelope."""

    @pytest.mark.parametrize(
        "fixture_name", RUN_DETAIL_JSON_FIXTURES, ids=lambda p: Path(p).stem
    )
    def test_json_fixture_validates_against_run_detail_schema(self, fixture_name):
        doc = _load_json_fixture(fixture_name)
        assert doc["status"] == "ok"
        assert "data" in doc
        parsed = RunDetail.model_validate(doc["data"])
        _assert_keys_equal(parsed.model_dump(mode="json"), doc["data"], fixture_name)
