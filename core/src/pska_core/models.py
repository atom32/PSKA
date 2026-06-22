from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, UserStatus, Visibility


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    handle: str
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Team:
    team_id: str
    slug: str
    status: str = "active"


@dataclass(frozen=True, slots=True)
class TeamMembership:
    user_id: str
    team_id: str
    role: str = "member"


@dataclass(frozen=True, slots=True)
class Space:
    space_id: str
    slug: str
    kind: str
    owner_user_id: str | None = None
    team_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_item_id: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    message_id: str | None = None
    path: str | None = None
    url: str | None = None


@dataclass(slots=True)
class ChannelIngestPayload:
    schema_version: str
    source_channel: str
    record_type: str
    source_id: str
    owner_user_id: str
    space_id: str
    visibility: Visibility = Visibility.PRIVATE
    visible_team_ids: list[str] = field(default_factory=list)
    url: str | None = None
    title: str | None = None
    author: dict[str, Any] = field(default_factory=dict)
    content: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    captured_at: str | None = None
    media: list[dict[str, Any]] = field(default_factory=list)
    raw_paths: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ChannelIngestPayload":
        return cls(
            schema_version=str(data["schema_version"]),
            source_channel=str(data["source_channel"]),
            record_type=str(data["record_type"]),
            source_id=str(data["source_id"]),
            owner_user_id=str(data["owner_user_id"]),
            space_id=str(data["space_id"]),
            visibility=Visibility(data.get("visibility", Visibility.PRIVATE)),
            visible_team_ids=list(data.get("visible_team_ids") or []),
            url=data.get("url"),
            title=data.get("title"),
            author=dict(data.get("author") or {}),
            content=dict(data.get("content") or {}),
            created_at=data.get("created_at"),
            captured_at=data.get("captured_at"),
            media=list(data.get("media") or []),
            raw_paths=dict(data.get("raw_paths") or {}),
            extra=dict(data.get("extra") or {}),
        )


@dataclass(slots=True)
class SourceItem:
    source_item_id: str
    source_channel: str
    record_type: str
    source_id: str
    owner_user_id: str
    space_id: str
    visibility: Visibility
    visible_team_ids: list[str]
    title: str
    url: str | None
    content_text: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ConnectorState:
    connector_state_id: str
    connector_id: str
    owner_user_id: str
    enabled: bool = True
    scan_cursor: str | None = None
    sync_status: str = "idle"
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    permission_scope: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class KnowledgeSource:
    knowledge_source_id: str
    owner_user_id: str
    name: str
    source_type: str
    uri: str
    mode: str = "manual"
    status: str = "authorized"
    connector_id: str = "files"
    space_id: str = "private_primary"
    visibility: Visibility = Visibility.PRIVATE
    visible_team_ids: list[str] = field(default_factory=list)
    permission_scope: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    last_sync_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class SyncRun:
    sync_run_id: str
    knowledge_source_id: str
    owner_user_id: str
    connector_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    scanned: int = 0
    ingested: int = 0
    new_files: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    moved_files: int = 0
    missing_files: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None
    report: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Document:
    document_id: str
    source_item_id: str
    owner_user_id: str
    space_id: str
    visibility: Visibility
    visible_team_ids: list[str]
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    document_id: str
    source_item_id: str
    owner_user_id: str
    space_id: str
    visibility: Visibility
    visible_team_ids: list[str]
    text: str
    ordinal: int = 0
    embedding: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OfflineIndexState:
    object_type: str
    object_id: str
    owner_user_id: str
    source_item_id: str | None = None
    content_hash: str | None = None
    mtime: str | None = None
    visibility_version: str | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    index_version: str = "hipporag_offline.v1"
    status: str = "dirty"
    dirty_reason: str | None = None
    last_indexed_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkspaceActivityEvent:
    workspace_activity_event_id: str
    owner_user_id: str
    actor_user_id: str
    activity_type: str
    target_type: str
    target_id: str
    surface: str
    title: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class DiscoveryItem:
    discovery_id: str
    owner_user_id: str
    discovery_type: str
    title: str
    evidence: list[dict[str, Any]]
    confidence: float
    producer: str
    fingerprint: str = ""
    evidence_snapshot: list[dict[str, Any]] = field(default_factory=list)
    discovery_score: float = 0.0
    quality_signals: dict[str, Any] = field(default_factory=dict)
    status: str = "new"
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Memory:
    memory_id: str
    owner_user_id: str
    space_id: str
    visibility: Visibility
    text: str
    memory_type: str
    confidence: float
    source_refs: list[SourceRef] = field(default_factory=list)
    visible_team_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentMemory:
    agent_memory_id: str
    owner_user_id: str
    layer: MemoryLayer
    text: str
    confidence: float
    source_refs: list[SourceRef]
    decay_policy: str = "manual"
    last_verified_at: datetime | None = None
    created_by_user_id: str | None = None

    def __post_init__(self) -> None:
        if self.created_by_user_id == self.owner_user_id:
            return
        if not self.owner_user_id:
            raise ValueError("agent memory must belong to a represented user")


@dataclass(slots=True)
class UserProfileCard:
    profile_card_id: str
    owner_user_id: str
    profile: dict[str, Any]
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: float = 0.0
    last_verified_at: datetime | None = None


@dataclass(slots=True)
class Entity:
    entity_id: str
    entity_type: str
    label: str
    owner_user_id: str
    space_id: str
    visibility: Visibility
    visible_team_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Hyperedge:
    hyperedge_id: str
    relation_type: str
    owner_user_id: str
    space_id: str
    visibility: Visibility
    directionality: Directionality = Directionality.AMBIGUOUS
    visible_team_ids: list[str] = field(default_factory=list)
    evidence_text: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class HyperedgeMember:
    hyperedge_id: str
    entity_id: str
    role: str
    ordinal: int = 0


@dataclass(slots=True)
class ReviewItem:
    review_item_id: str
    owner_user_id: str
    review_type: ReviewType
    title: str
    proposal: dict[str, Any]
    status: str = "pending"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Job:
    job_id: str
    job_type: str
    payload: dict[str, Any]
    status: str = "queued"
    attempts: int = 0
    max_attempts: int = 3
    priority: int = 0
    run_after: datetime | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker_id: str | None = None
    leased_until: datetime | None = None
    heartbeat_at: datetime | None = None
    external_run_id: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(slots=True)
class JobEvent:
    job_event_id: str
    job_id: str
    event_type: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AuditEvent:
    audit_event_id: str
    actor_user_id: str
    action: str
    target_type: str
    target_id: str
    decision: str
    metadata: dict[str, Any] = field(default_factory=dict)
