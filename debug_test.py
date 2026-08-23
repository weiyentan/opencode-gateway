import uuid
from unittest.mock import AsyncMock, MagicMock
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.afk_executions import AWX_EXECUTION_BINDING_CLIENT_NAME
from tests.conftest import create_client, mock_row

_COLLECTOR_BEARER = "awx-collector-bearer-token"
_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()

def _auth_row():
    return mock_row({
        "credential_id": _CREDENTIAL_ID,
        "revoked_at": None,
        "last_used_at": None,
        "client_id": _CLIENT_ID,
        "client_name": AWX_EXECUTION_BINDING_CLIENT_NAME,
        "client_is_active": True,
    })

def _saved_row():
    return mock_row({
        "id": uuid.uuid4(),
        "awx_job_id": 42,
        "job_template_id": 7,
        "external_session_id": "ses_abc123",
        "provider": "github",
        "repository_url": "github.com/acme/proj",
        "entity_type": "change_request",
        "entity_number": "99",
        "outcome": "completed",
        "source_event_id": "evt_001",
        "branch": None,
        "title": None,
        "failure_reason": None,
        "started_at": None,
        "finished_at": None,
    })

def _valid_binding_payload():
    return {
        "awx_job": {"job_id": "42", "job_template_id": 7},
        "external_session_id": "ses_abc123",
        "resource": {
            "provider": "github",
            "repository": "https://github.com/acme/proj",
            "resource_type": "pull_request",
            "resource_number": "99",
        },
        "outcome": "completed",
    }

@pytest.mark.asyncio
async def test_debug():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None, _saved_row()])
    conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
    conn.execute = AsyncMock()
    # also need fetch for insert RETURNING? The repo uses fetch for save, not execute
    # Let's mock both fetch and fetchrow for insert
    # save_execution_binding uses conn.fetch, not fetchrow
    # So we need to ensure conn.fetch returns rows for insert
    # The test's original had conn.execute mock but repo uses fetch for insert now?
    # Check: repo.save_execution_binding uses fetch, but test provides conn.fetchrow side_effect including saved row, and conn.execute mock
    # Actually earlier passing test used fetchrow for auth and saved row, and fetch for something else, execute for insert?
    # Let's inspect repo: save uses fetch, get uses fetchrow
    # So for this test, side_effect for fetchrow: auth, None (get before insert), saved (get after insert)
    # And fetch for insert should return [id]
    # But test's mock has conn.fetchrow side_effect len 3 and conn.execute mock, but no fetch mock for insert - maybe that's why 500?
    # Let's see what the original passing test does: it had conn.fetchrow = [auth, None, saved] and conn.execute mock, but repo now uses fetch not execute for save
    # So mock is mismatched
    client = create_client(conn)
    resp = await client.post(
        "/api/v1/afk/executions",
        json=_valid_binding_payload(),
        headers={"X-Collector-Token": _COLLECTOR_BEARER},
    )
    print("STATUS", resp.status_code)
    print("TEXT", resp.text)
    import traceback
    assert resp.status_code == 201
