#!/usr/bin/env python3
"""Generate serializer-produced fixture examples for every allowed v1
(resource_type, action) pair.

Each fixture is built through the real NormalizedProviderEvent Pydantic model
(serializer), validated against the published JSON Schema, and written to
docs/contracts/normalized-event-v1/fixtures/.

Usage:
    python scripts/generate_normalized_event_fixtures.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so we can import the consumer module.
_PROJ_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ_ROOT))

from app.consumer.afk_consumer import NormalizedProviderEvent  # noqa: E402

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

# ── Allowed v1 (resource_type, action) pairs ─────────────────────────────────
#
# These are the producer-contract vocabulary: every pair the producer may emit
# for v1.  The consumer's mapping bridge validates these against the locked
# canonical vocabulary (_MAPPED_EVENT_TYPES).

ALLOWED_PAIRS: list[tuple[str, str]] = [
    # issue lifecycle
    ("issue", "opened"),
    ("issue", "closed"),
    # pull_request lifecycle (GitHub)
    ("pull_request", "opened"),
    ("pull_request", "review_requested"),
    ("pull_request", "changes_requested"),
    ("pull_request", "approved"),
    ("pull_request", "merged"),
    ("pull_request", "closed"),
    # merge_request lifecycle (GitLab)
    ("merge_request", "opened"),
    ("merge_request", "review_requested"),
    ("merge_request", "changes_requested"),
    ("merge_request", "approved"),
    ("merge_request", "merged"),
    ("merge_request", "closed"),
]

# ── Fixture base data ────────────────────────────────────────────────────────
#
# Every fixture shares the same provider, delivery_id, repository, and
# timestamps so payload references identify the same provider and delivery_id
# as their containing envelope (acceptance criterion).

FIXTURE_PROVIDER = "github"
FIXTURE_DELIVERY_ID = "00000000-0000-0000-0000-000000000001"
FIXTURE_REPOSITORY = "owner/repo"
FIXTURE_OCCURRED_AT = datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC)
FIXTURE_INGESTED_AT = datetime(2026, 8, 15, 10, 0, 1, tzinfo=UTC)
FIXTURE_ACTOR = "test-user"

# Resource IDs are stable across fixtures so the same resource appears in
# multiple lifecycle states (e.g. issue:100 opened → closed).
_RESOURCE_ID_MAP: dict[str, str] = {
    "issue": "100",
    "pull_request": "200",
    "merge_request": "300",
}


def _fixture_filename(resource_type: str, action: str) -> str:
    return f"{resource_type}.{action}.json"


def _build_fixture(resource_type: str, action: str) -> dict:
    """Build one fixture through the real NormalizedProviderEvent serializer."""
    resource_id = _RESOURCE_ID_MAP[resource_type]
    message = NormalizedProviderEvent(
        schema_version="1.0",
        provider=FIXTURE_PROVIDER,
        delivery_id=FIXTURE_DELIVERY_ID,
        resource_type=resource_type,
        resource_id=resource_id,
        repository=FIXTURE_REPOSITORY,
        action=action,
        occurred_at=FIXTURE_OCCURRED_AT,
        ingested_at=FIXTURE_INGESTED_AT,
        actor=FIXTURE_ACTOR,
        payload_ref=f"redacted-payload-ref-{resource_id}",
    )
    # Serialize through the model to get the exact producer output shape.
    return json.loads(message.model_dump_json())


def main() -> None:
    fixtures_dir = (
        _PROJ_ROOT / "docs" / "contracts" / "normalized-event-v1" / "fixtures"
    )
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    for resource_type, action in ALLOWED_PAIRS:
        fixture = _build_fixture(resource_type, action)
        filename = _fixture_filename(resource_type, action)
        filepath = fixtures_dir / filename
        filepath.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        generated += 1
        print(f"  wrote {filename}")

    print(f"\nGenerated {generated} fixture(s) in {fixtures_dir}")


if __name__ == "__main__":
    main()
