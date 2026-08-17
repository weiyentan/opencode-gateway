"""Generate producer-shaped normalized-event v1 contract fixtures.

Pinned-copy generator for the consumer-side contract fixtures under
``docs/contracts/normalized-event-v1/fixtures/``.  This script reproduces the
EXACT outbound shape of the producer-owned contract — ``build_normalized_message``
in fast-api-eda-gateway (``src/fast_api_eda_gateway/normalized_event.py`` +
``normalized_event_producer.py``): schema_version "1.0", event_type "normalized",
provider, delivery_id, nested resource{type, repository_url, number}, top-level
action, occurred_at, ingested_at, actor, and redacted_payload.reference
{provider, delivery_id} equal to the envelope.

It deliberately does NOT import the consumer's ``NormalizedProviderEvent``
model: the fixtures are the pinned contract's source of truth, never the
consumer's serializer.  Only the producer's real allowlisted (resource_type,
action) pairs are generated.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = "1.0"
EVENT_TYPE = "normalized"

FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "contracts"
    / "normalized-event-v1"
    / "fixtures"
)

#: Every real producer-allowlisted pair (resource type, action, forge).
ALLOWED_PAIRS: list[tuple[str, str, str]] = [
    ("issue", "opened", "github"),
    ("issue", "edited", "github"),
    ("issue", "reopened", "github"),
    ("issue", "closed", "github"),
    ("pull_request", "opened", "github"),
    ("pull_request", "edited", "github"),
    ("pull_request", "reopened", "github"),
    ("pull_request", "closed", "github"),
    ("pull_request", "merged", "github"),
    ("merge_request", "opened", "gitlab"),
    ("merge_request", "updated", "gitlab"),
    ("merge_request", "reopened", "gitlab"),
    ("merge_request", "closed", "gitlab"),
    ("merge_request", "merged", "gitlab"),
]


def _repository_url(provider: str) -> str:
    if provider == "github":
        return "https://github.com/owner/repo"
    return "https://gitlab.com/group/project"


def _message(
    resource_type: str, action: str, provider: str, delivery_suffix: int
) -> dict:
    delivery_id = f"00000000-0000-0000-0000-{delivery_suffix:012d}"
    number = 100 + delivery_suffix
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": EVENT_TYPE,
        "provider": provider,
        "delivery_id": delivery_id,
        "resource": {
            "type": resource_type,
            "repository_url": _repository_url(provider),
            "number": number,
        },
        "action": action,
        "occurred_at": "2026-08-15T10:00:00Z",
        "ingested_at": "2026-08-15T10:00:01Z",
        "actor": "test-user",
        "redacted_payload": {
            "reference": {
                "provider": provider,
                "delivery_id": delivery_id,
            },
        },
    }


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURES_DIR.glob("*.json"):
        stale.unlink()
        print(f"removed stale {stale.name}")
    for index, (resource_type, action, provider) in enumerate(ALLOWED_PAIRS, start=1):
        message = _message(resource_type, action, provider, index)
        path = FIXTURES_DIR / f"{resource_type}.{action}.json"
        path.write_text(json.dumps(message, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
