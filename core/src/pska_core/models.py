from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, UserStatus, Visibility


DEFAULT_TENANT_ID = "tenant_default"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Tenant:
    tenant_id: str
    slug: str
    name: str = ""
    status: str = "active"


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    handle: str
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(frozen=True, slots=True)
class Team:
    team_id: str
    slug: str
    status: str = "active"
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(frozen=True, slots=True)
class TeamMembership:
    user_id: str
    team_id: str
    role: str = "member"
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(frozen=True, slots=True)
class Space:
    space_id: str
    slug: str
    kind: str
    owner_user_id: str | None = None
    team_id: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_item_id: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    passage_window_id: str | None = None
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
    tenant_id: str = DEFAULT_TENANT_ID

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
            tenant_id=str(data.get("tenant_id") or DEFAULT_TENANT_ID),
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
    lifecycle_status: str = "active"
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    delete_reason: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class KnowledgeBase:
    knowledge_base_id: str
    owner_user_id: str
    name: str
    created_by_user_id: str = ""
    slug: str = ""
    description: str = ""
    kb_type: str = "document"
    status: str = "active"
    visibility: Visibility = Visibility.PRIVATE
    visible_team_ids: list[str] = field(default_factory=list)
    default_space_id: str | None = None
    is_default: bool = False
    pinned_at: datetime | None = None
    config: dict[str, Any] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class KnowledgeBaseSource:
    knowledge_base_id: str
    knowledge_source_id: str
    owner_user_id: str
    added_by_user_id: str
    membership_status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    added_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class KnowledgeBaseSourceItem:
    knowledge_base_id: str
    source_item_id: str
    owner_user_id: str
    added_by_user_id: str
    membership_type: str = "manual"
    membership_status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    added_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class ProcessingSpan:
    processing_span_id: str
    knowledge_source_id: str
    owner_user_id: str
    stage: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    sync_run_id: str | None = None
    source_item_id: str | None = None
    duration_ms: int | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


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
    lifecycle_status: str = "active"
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    delete_reason: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


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
    lifecycle_status: str = "active"
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    delete_reason: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class AskConversation:
    conversation_id: str
    owner_user_id: str
    title: str
    status: str = "active"
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class AskMessage:
    message_id: str
    conversation_id: str
    owner_user_id: str
    role: str
    content: str
    run_id: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class AskRun:
    run_id: str
    conversation_id: str
    owner_user_id: str
    query: str
    status: str = "running"
    result: dict[str, Any] = field(default_factory=dict)
    route: dict[str, Any] = field(default_factory=dict)
    evidence_check: dict[str, Any] = field(default_factory=dict)
    prompt_profile_id: str | None = None
    prompt_profile_version: int | None = None
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class PromptProfile:
    prompt_profile_id: str
    profile_type: str
    scope: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    owner_user_id: str | None = None
    status: str = "active"
    current_version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class PassageWindow:
    passage_window_id: str
    source_item_id: str
    document_id: str
    owner_user_id: str
    ordinal: int
    title: str
    text: str
    start_char: int = 0
    end_char: int = 0
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class WritingBoard:
    board_id: str
    owner_user_id: str
    title: str
    goal: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class WritingNode:
    node_id: str
    board_id: str
    owner_user_id: str
    node_type: str
    title: str
    body_markdown: str = ""
    position: dict[str, Any] = field(default_factory=dict)
    size: dict[str, Any] = field(default_factory=dict)
    status: str = "idle"
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    quality_signals: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class WritingEdge:
    edge_id: str
    board_id: str
    owner_user_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class KnowledgeTopic:
    topic_id: str
    owner_user_id: str
    label: str
    normalized_label: str
    topic_type: str = "topic"
    description: str = ""
    confidence: float = 0.0
    producer: str = "pska.topic_linker"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class TopicMention:
    topic_mention_id: str
    topic_id: str
    owner_user_id: str
    source_item_id: str
    document_id: str | None = None
    chunk_id: str | None = None
    artifact_type: str = "chunk"
    artifact_id: str = ""
    mention_text: str = ""
    confidence: float = 0.0
    producer: str = "pska.topic_linker"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class ArtifactSupport:
    artifact_support_id: str
    owner_user_id: str
    artifact_type: str
    artifact_id: str
    support_type: str
    source_item_id: str
    document_id: str | None = None
    chunk_id: str | None = None
    topic_id: str | None = None
    status: str = "active"
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID

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
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class KnowledgeClaim:
    knowledge_claim_id: str
    owner_user_id: str
    claim_type: str
    statement: str
    source_refs: list[SourceRef]
    evidence_text: str
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    qualifiers: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    producer: str = "fastreact"
    job_id: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class DigestNote:
    digest_note_id: str
    owner_user_id: str
    title: str
    synopsis: str
    source_refs: list[SourceRef]
    key_points: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    memory_suggestions: list[dict[str, Any]] = field(default_factory=list)
    relationship_suggestions: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    producer: str = "fastreact"
    job_id: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID


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
    tenant_id: str = DEFAULT_TENANT_ID
    owner_user_id: str = "user_primary"


@dataclass(slots=True)
class JobEvent:
    job_event_id: str
    job_id: str
    event_type: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(slots=True)
class AuditEvent:
    audit_event_id: str
    actor_user_id: str
    action: str
    target_type: str
    target_id: str
    decision: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    tenant_id: str = DEFAULT_TENANT_ID
