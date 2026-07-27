"""Lightweight consumer-side models for the ingest payload.

These models describe the shape of messages the consumer expects on the
``opencode-usage`` Kafka topic without importing Gateway-internal schemas.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    """Consumer-side representation of an ingest payload from Kafka.

    Validates top-level structure but uses generic containers for nested
    objects — the Gateway performs deeper validation at the HTTP layer.
    """

    schema_version: str = Field(description="Schema version of the payload")
    collector_version: str = Field(description="Version of the collector software")
    source_database_id: uuid.UUID = Field(
        description="Source database identifier assigned by the collector"
    )
    records: list[dict[str, Any]] = Field(
        default_factory=list, description="Usage records to ingest"
    )
    session_contexts: list[dict[str, Any]] = Field(
        default_factory=list, description="Session context projections"
    )
    projects: list[dict[str, Any]] = Field(
        default_factory=list, description="Source project projections"
    )
    project_directories: list[dict[str, Any]] = Field(
        default_factory=list, description="Project directory projections"
    )
    session_todos: list[dict[str, Any]] = Field(
        default_factory=list, description="Session todo projections"
    )
