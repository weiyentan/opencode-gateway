"""Pydantic schemas for the execution-transcript read-only API (ADR 0016).

These view models map the stored ``observed_messages`` / ``observed_parts`` /
``observed_tool_calls`` rows 1:1 onto the consumer-facing response, following
the ``app/core/schemas/usage.py`` / ``afk.py`` conventions.  Transcript list
endpoints use keyset (cursor) pagination; the smaller count-bounded header and
children endpoints use the shared offset/limit :class:`PaginatedResponse`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):  # noqa: UP046
    """A keyset-paginated response page for append-only transcript streams."""

    items: list[T] = Field(description="The items for the current page")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page (null on the last page)",
    )
    has_more: bool = Field(description="True when another page follows")


class ObservedMessage(BaseModel):
    """One ``observed_messages`` row (a page item of a message stream)."""

    id: uuid.UUID
    external_message_id: str
    external_session_id: str
    session_id: uuid.UUID | None = None
    parent_external_session_id: str | None = None
    role: str
    agent: str | None = None
    mode: str | None = None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    source_created_at: int | None = None
    source_updated_at: int | None = None
    source_created_at_tz: datetime | None = None
    source_updated_at_tz: datetime | None = None
    data: dict | None = None


class ObservedPart(BaseModel):
    """One ``observed_parts`` row (a page item of a part event stream)."""

    id: uuid.UUID
    external_part_id: str
    external_message_id: str
    external_session_id: str
    message_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    part_type: str
    source_created_at: int | None = None
    source_updated_at: int | None = None
    source_created_at_tz: datetime | None = None
    source_updated_at_tz: datetime | None = None
    data: dict | None = None


class ObservedToolCall(BaseModel):
    """One ``observed_tool_calls`` row (a page item of a tool-call query)."""

    id: uuid.UUID
    external_part_id: str
    external_session_id: str
    part_id: uuid.UUID
    message_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    tool_name: str
    tool_status: str | None = None
    tool_input: dict | None = None
    tool_output: dict | None = None
    source_created_at: int | None = None
    source_created_at_tz: datetime | None = None


class ChildSession(BaseModel):
    """A child subagent session of a transcript session."""

    id: uuid.UUID = Field(description="Internal Gateway session UUID")
    external_session_id: str | None = None
    agent: str | None = None


class SessionHeader(BaseModel):
    """Transcript session header: identity, parent/child linkage, counts."""

    id: uuid.UUID = Field(description="Internal Gateway session UUID")
    external_session_id: str | None = None
    agent: str | None = None
    parent_session_id: str | None = Field(
        default=None, description="External session ID of the parent (null for root)"
    )
    parent_internal_id: uuid.UUID | None = Field(
        default=None, description="Internal UUID of the parent session, if resolved"
    )
    child_session_ids: list[uuid.UUID] = Field(
        default_factory=list, description="Internal UUIDs of direct child sessions"
    )
    message_count: int = Field(default=0, ge=0)
    part_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    first_part_at: datetime | None = None
    last_part_at: datetime | None = None


class TimelineEvent(BaseModel):
    """One part event annotated with its owning session and generation depth."""

    part_id: uuid.UUID
    session_id: uuid.UUID
    external_session_id: str
    agent: str | None = None
    depth: int = Field(default=0, ge=0)
    part_type: str
    source_created_at: int | None = None
    source_created_at_tz: datetime | None = None
    data: dict | None = None
