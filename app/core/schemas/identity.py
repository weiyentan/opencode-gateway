"""Pydantic schemas for the identity layer — clients and credentials."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Client schemas ────────────────────────────────────────────────────────


class ClientCreate(BaseModel):
    """Payload for creating a new OpenCode client."""

    name: str
    description: Optional[str] = None


class ClientUpdate(BaseModel):
    """Payload for updating an existing OpenCode client.

    All fields are optional — only supplied fields are applied.
    """

    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ClientRead(BaseModel):
    """Public representation of an OpenCode client."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── Token / credential schemas ────────────────────────────────────────────


class TokenProvisionRequest(BaseModel):
    """Payload for provisioning a new collector credential token."""

    label: Optional[str] = None


class TokenProvisionResponse(BaseModel):
    """Returned once when a token is provisioned — includes the raw token.

    **The raw token is only shown here.**  Subsequent reads of the
    credential list will only show metadata (prefix, label, timestamps).
    """

    token: str = Field(
        description="Raw bearer token — shown only once at provision time"
    )
    id: uuid.UUID
    client_id: uuid.UUID
    token_prefix: str
    label: Optional[str] = None
    created_at: datetime


class TokenRead(BaseModel):
    """Metadata for a collector credential — NEVER includes the raw token."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    token_prefix: str
    label: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime
    revoked_at: Optional[datetime] = None


class ClientWithTokens(ClientRead):
    """Client details including its associated credential tokens (metadata only)."""

    tokens: list[TokenRead]
