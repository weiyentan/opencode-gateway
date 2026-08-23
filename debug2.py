import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from tests.conftest import mock_row, create_client

_RUN_ID = "01J8ABCDEFGHJKMNPQRSTVWXYZ"
_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
_SESSION_ID = uuid.uuid4()

def _mk_run_row():
    return mock_row({
        "afk_run_id": _RUN_ID,
        "provider": "github",
        "status": "completed",
        "title": "Fix login bug",
        "started_at": _A_TS,
        "finished_at": _B_TS,
        "outcome_status": "merged",
        "outcome": {
            "status": "merged",
            "change_request_ids": ["change_request:42"],
            "resolved_issue_ids": ["issue:37"],
            "merge_event_id": "merge_event:99",
            "merged_at": _B_TS.isoformat(),
        },
        "first_seen_at": _A_TS,
        "last_seen_at": _B_TS,
    })

def _mk_entity_row(**kw):
    from tests.test_api_afk_outcomes import _mk_entity_row as orig
    return orig(**kw)

def _mk_session_row():
    from tests.test_api_afk_outcomes import _mk_session_row as orig
    return orig()

@pytest.mark.asyncio
async def test_debug2():
    from tests.test_api_afk_outcomes import _mk_entity_row, _mk_session_row
    entity_rows = [
        _mk_entity_row(entity_type="issue", external_id="37", role="resolved"),
        _mk_entity_row(entity_type="change_request", external_id="42", role="resolved", correlation_method="issue_reference"),
        _mk_entity_row(entity_type="issue", external_id="88", role="referenced", correlation_method="temporal_inference", correlation_confidence=0.4),
    ]
    session_rows = [_mk_session_row()]
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=_mk_run_row())
    mock_conn.fetch = AsyncMock(side_effect=[entity_rows, session_rows])
    mock_conn.fetchval = AsyncMock(return_value=1)
    # need to mock pool
    from httpx import ASGITransport, AsyncClient
    from app.core.factory import create_app
    from app.db.session import get_session
    from fastapi import Request
    mock_pool = AsyncMock()
    app = create_app(configure_logging=False)
    app.state.pool = mock_pool
    async def _override(request: Request):
        yield mock_conn
    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer test-key"})
    # need to set api key env
    import os
    os.environ["GATEWAY_API_KEY"] = "test-key"
    resp = await client.get(f"/api/v1/afk-outcomes/runs/{_RUN_ID}")
    print("STATUS", resp.status_code)
    print("TEXT", resp.text[:2000])
    print("CALLS", mock_conn.fetch.call_args_list)
