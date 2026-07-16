"""Admin API endpoints for managing OpenCode clients and collector credentials.

All endpoints require API-key authentication (enforced by
:class:`~app.core.auth.ApiKeyMiddleware`).  Collector token auth is a
separate dependency used by future collector-facing endpoints.
"""

import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.identity import generate_collector_token
from app.core.schemas.identity import (
    ClientCreate,
    ClientRead,
    ClientUpdate,
    ClientWithTokens,
    TokenProvisionRequest,
    TokenProvisionResponse,
    TokenRead,
)
from app.core.schemas.usage import PaginatedResponse
from app.db.session import get_session

router = APIRouter(prefix="/admin/clients", tags=["admin"])


# ── Helpers ───────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_client_read(row: asyncpg.Record) -> ClientRead:
    return ClientRead(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_token_read(row: asyncpg.Record) -> TokenRead:
    return TokenRead(
        id=row["id"],
        client_id=row["client_id"],
        token_prefix=row["token_prefix"],
        label=row["label"],
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


# ── Client CRUD ───────────────────────────────────────────────────────────


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ClientCreate,
    conn: asyncpg.Connection = Depends(get_session),
) -> ClientRead:
    """Create a new OpenCode client."""
    row = await conn.fetchrow(
        """
        INSERT INTO opencode_clients (name, description)
        VALUES ($1, $2)
        RETURNING id, name, description, is_active, created_at, updated_at
        """,
        body.name,
        body.description,
    )
    return _row_to_client_read(row)


@router.get("", response_model=PaginatedResponse[ClientRead])
async def list_clients(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[ClientRead]:
    """List registered OpenCode clients with pagination."""
    total: int = await conn.fetchval("SELECT COUNT(*) FROM opencode_clients")

    rows = await conn.fetch(
        "SELECT id, name, description, is_active, created_at, updated_at "
        "FROM opencode_clients ORDER BY name "
        "LIMIT $1 OFFSET $2",
        limit,
        offset,
    )
    items = [_row_to_client_read(r) for r in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{client_id}", response_model=ClientWithTokens)
async def get_client(
    client_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> ClientWithTokens:
    """Get a client by ID, including its credential tokens (metadata only)."""
    client_row = await conn.fetchrow(
        "SELECT id, name, description, is_active, created_at, updated_at "
        "FROM opencode_clients WHERE id = $1",
        client_id,
    )
    if client_row is None:
        raise HTTPException(status_code=404, detail="Client not found")

    token_rows = await conn.fetch(
        "SELECT id, client_id, token_prefix, label, last_used_at, created_at, revoked_at "
        "FROM collector_credentials WHERE client_id = $1 ORDER BY created_at DESC",
        client_id,
    )
    return ClientWithTokens(
        id=client_row["id"],
        name=client_row["name"],
        description=client_row["description"],
        is_active=client_row["is_active"],
        created_at=client_row["created_at"],
        updated_at=client_row["updated_at"],
        tokens=[_row_to_token_read(r) for r in token_rows],
    )


@router.patch("/{client_id}", response_model=ClientRead)
async def update_client(
    client_id: uuid.UUID,
    body: ClientUpdate,
    conn: asyncpg.Connection = Depends(get_session),
) -> ClientRead:
    """Update an existing OpenCode client.

    Only supplied fields are applied.  Returns 404 if the client does
    not exist.
    """
    # Fetch current state first to verify existence
    existing = await conn.fetchrow(
        "SELECT id, name, description, is_active, created_at, updated_at "
        "FROM opencode_clients WHERE id = $1",
        client_id,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Client not found")

    # Build dynamic SET clause from supplied fields only
    set_parts: list[str] = []
    params: list = []
    idx = 1

    for field_name in ("name", "description", "is_active"):
        value = getattr(body, field_name)
        if value is not None:
            set_parts.append(f"{field_name} = ${idx}")
            params.append(value)
            idx += 1

    if not set_parts:
        # Nothing to update — return current state
        return _row_to_client_read(existing)

    # Always bump updated_at
    set_parts.append(f"updated_at = ${idx}")
    params.append(_utcnow())
    idx += 1

    params.append(client_id)
    set_clause = ", ".join(set_parts)

    row = await conn.fetchrow(
        f"""
        UPDATE opencode_clients
        SET {set_clause}
        WHERE id = ${idx}
        RETURNING id, name, description, is_active, created_at, updated_at
        """,
        *params,
    )
    return _row_to_client_read(row)


@router.delete(
    "/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_client(
    client_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> None:
    """Soft-delete an OpenCode client (sets is_active=false).

    Returns 404 if the client does not exist.
    """
    result = await conn.execute(
        "UPDATE opencode_clients SET is_active = false, updated_at = $2 "
        "WHERE id = $1",
        client_id,
        _utcnow(),
    )
    # asyncpg execute returns a string like "UPDATE 1" — parse the count
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Client not found")


# ── Token management ──────────────────────────────────────────────────────


@router.post(
    "/{client_id}/tokens",
    response_model=TokenProvisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def provision_token(
    client_id: uuid.UUID,
    body: TokenProvisionRequest = TokenProvisionRequest(),
    conn: asyncpg.Connection = Depends(get_session),
) -> TokenProvisionResponse:
    """Provision a new collector bearer token for a client.

    Returns the **raw token in the response** — this is the only time
    the raw value is shown.  Subsequent reads return only metadata.
    """
    # Verify client exists
    client = await conn.fetchrow(
        "SELECT id, is_active FROM opencode_clients WHERE id = $1", client_id
    )
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if not client["is_active"]:
        raise HTTPException(
            status_code=409, detail="Cannot provision tokens for a deactivated client"
        )

    raw_token, token_hash, token_prefix = generate_collector_token()

    row = await conn.fetchrow(
        """
        INSERT INTO collector_credentials (client_id, token_hash, token_prefix, label)
        VALUES ($1, $2, $3, $4)
        RETURNING id, client_id, token_prefix, label, created_at
        """,
        client_id,
        token_hash,
        token_prefix,
        body.label,
    )

    return TokenProvisionResponse(
        token=raw_token,
        id=row["id"],
        client_id=row["client_id"],
        token_prefix=row["token_prefix"],
        label=row["label"],
        created_at=row["created_at"],
    )


@router.get("/{client_id}/tokens", response_model=list[TokenRead])
async def list_tokens(
    client_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> list[TokenRead]:
    """List credential tokens for a client — metadata only, **no raw tokens**."""
    # Verify client exists
    client = await conn.fetchrow(
        "SELECT id FROM opencode_clients WHERE id = $1", client_id
    )
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    rows = await conn.fetch(
        "SELECT id, client_id, token_prefix, label, last_used_at, created_at, revoked_at "
        "FROM collector_credentials WHERE client_id = $1 ORDER BY created_at DESC",
        client_id,
    )
    return [_row_to_token_read(r) for r in rows]


@router.post(
    "/{client_id}/tokens/{token_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def revoke_token(
    client_id: uuid.UUID,
    token_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_session),
) -> None:
    """Revoke a collector credential token immediately.

    Revoked tokens are rejected at auth.  Returns 404 if the credential
    does not exist or does not belong to the given client.
    """
    result = await conn.execute(
        "UPDATE collector_credentials "
        "SET revoked_at = $3 "
        "WHERE id = $1 AND client_id = $2 AND revoked_at IS NULL",
        token_id,
        client_id,
        _utcnow(),
    )
    if result == "UPDATE 0":
        raise HTTPException(
            status_code=404, detail="Credential not found or already revoked"
        )
