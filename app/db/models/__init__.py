"""SQLAlchemy ORM models — observability models added in later slices."""

from app.db.models.afk import (
    AFKRun,
    AFKRunEntityLink,
    AFKRunSessionLink,
    DeliveryLog,
    EngineeringEvent,
    ExecutionBinding,
    ExecutionOutcomeCorrection,
    ResourceSessionAssociation,
    UnresolvedCorrelation,
)
from app.db.models.base import Base
from app.db.models.identity import CollectorCredential, OpenCodeClient
from app.db.models.ingest import (
    IngestAudit,
    IngestBatch,
    ObservedModel,
    OpenCodeUsageRecord,
    Session,
    SourceDatabase,
)
from app.db.models.projection import (
    ObservedMessage,
    ObservedPart,
    ObservedToolCall,
    OpenCodeProjectDirectory,
    OpenCodeSessionContext,
    OpenCodeSessionTodo,
    OpenCodeSourceProject,
)
from app.db.models.reporting import (
    DeliveryStateTrail,
    ReportingDelivery,
    ReportingResourceAggregate,
)

__all__ = [
    "AFKRun",
    "AFKRunEntityLink",
    "AFKRunSessionLink",
    "Base",
    "CollectorCredential",
    "DeliveryLog",
    "DeliveryStateTrail",
    "EngineeringEvent",
    "ExecutionBinding",
    "ExecutionOutcomeCorrection",
    "IngestAudit",
    "IngestBatch",
    "ObservedMessage",
    "ObservedModel",
    "ObservedPart",
    "ObservedToolCall",
    "OpenCodeClient",
    "OpenCodeProjectDirectory",
    "OpenCodeSessionContext",
    "OpenCodeSessionTodo",
    "OpenCodeSourceProject",
    "OpenCodeUsageRecord",
    "ReportingDelivery",
    "ReportingResourceAggregate",
    "ResourceSessionAssociation",
    "Session",
    "SourceDatabase",
    "UnresolvedCorrelation",
]
