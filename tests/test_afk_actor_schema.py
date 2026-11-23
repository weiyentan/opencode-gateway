"""Schema tests for the optional provisioned AFK trigger actor (issue #859).

The actor is the provider-qualified username of the account that originally
triggered the run (``github:login`` / ``gitlab:username``).  It is optional
for backward compatibility, bounded (3-255 chars), and validated at the API
boundary before the repository sees the value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.schemas.afk_lifecycle import AFKRunProvisionRequest


def _payload(**overrides) -> dict:
    payload = {
        "provider": "github",
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
    }
    payload.update(overrides)
    return payload


def test_actor_is_optional_and_defaults_to_none() -> None:
    """Old clients omit the field; the run carries no actor."""
    request = AFKRunProvisionRequest(**_payload())
    assert request.actor is None


@pytest.mark.parametrize(
    "actor",
    [
        "github:octocat",
        "gitlab:some.user_name-1",
        "github:a",
        "github:" + "a" * 248,  # 255 characters — the inclusive maximum
    ],
)
def test_actor_accepts_provider_qualified_username(actor: str) -> None:
    request = AFKRunProvisionRequest(**_payload(actor=actor))
    assert request.actor == actor


@pytest.mark.parametrize(
    "actor",
    [
        "octocat",  # no provider qualifier
        "github:",  # empty username
        "bitbucket:bob",  # unsupported provider
        "GitHub:octocat",  # provider must be lowercase
        "github:bad name",  # whitespace is not a username character
        "github:" + "a" * 249,  # 256 characters — over the maximum
    ],
)
def test_actor_rejects_invalid_values(actor: str) -> None:
    with pytest.raises(ValidationError):
        AFKRunProvisionRequest(**_payload(actor=actor))
