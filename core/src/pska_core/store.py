from __future__ import annotations

from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from pska_core.models import (
    AgentMemory,
    Chunk,
    ConnectorState,
    DiscoveryItem,
    Document,
    Entity,
    Hyperedge,
    HyperedgeMember,
    Job,
    JobEvent,
    AuditEvent,
    KnowledgeSource,
    OfflineIndexState,
    ReviewItem,
    SourceRef,
    SourceItem,
    SyncRun,
    TeamMembership,
    User,
    UserProfileCard,
    WorkspaceActivityEvent,
    utc_now,
)
from pska_core.enums import Visibility


class KnowledgeStore(Protocol):
    def add_user(self, user: User) -> None: ...
    def get_user(self, user_id: str) -> User: ...
    def team_memberships_for_user(self, user_id: str) -> list[TeamMembership]: ...
    def upsert_source_item(self, item: SourceItem) -> SourceItem: ...
    def upsert_knowledge_source(self, source: KnowledgeSource) -> KnowledgeSource: ...
    def get_knowledge_source(self, knowledge_source_id: str) -> KnowledgeSource: ...
    def list_knowledge_sources(
        self,
        *,
        owner_user_id: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[KnowledgeSource]: ...
    def add_sync_run(self, run: SyncRun) -> SyncRun: ...
    def list_sync_runs(self, *, knowledge_source_id: str | None = None, owner_user_id: str | None = None, limit: int = 50) -> list[SyncRun]: ...
    def upsert_connector_state(self, state: ConnectorState) -> ConnectorState: ...
    def get_connector_state(self, connector_state_id: str) -> ConnectorState: ...
    def list_connector_states(self, *, owner_user_id: str | None = None, connector_id: str | None = None) -> list[ConnectorState]: ...
    def add_document(self, document: Document) -> None: ...
    def add_chunk(self, chunk: Chunk) -> None: ...
    def add_agent_memory(self, memory: AgentMemory) -> None: ...
    def get_agent_memory(self, agent_memory_id: str) -> AgentMemory: ...
    def list_agent_memories(self, *, owner_user_id: str) -> list[AgentMemory]: ...
    def update_agent_memory_lifecycle(
        self,
        agent_memory_id: str,
        *,
        confidence: float,
        decay_policy: str,
        last_verified_at,
        source_refs: list[SourceRef] | None = None,
    ) -> AgentMemory: ...
    def add_profile_card(self, profile_card: UserProfileCard) -> None: ...
    def update_profile_card_lifecycle(
        self,
        profile_card_id: str,
        *,
        confidence: float,
        source_refs: list[SourceRef],
        last_verified_at,
    ) -> UserProfileCard: ...
    def list_profile_cards(self, *, owner_user_id: str) -> list[UserProfileCard]: ...
    def add_entity(self, entity: Entity) -> None: ...
    def add_hyperedge(self, hyperedge: Hyperedge, members: list[HyperedgeMember]) -> None: ...
    def add_review_item(self, review_item: ReviewItem) -> None: ...
    def get_review_item(self, review_item_id: str) -> ReviewItem: ...
    def list_review_items(self) -> list[ReviewItem]: ...
    def update_review_item_status(self, review_item_id: str, status: str) -> ReviewItem: ...
    def add_audit_event(self, event: AuditEvent) -> AuditEvent: ...
    def list_audit_events(self, target_type: str | None = None, target_id: str | None = None) -> list[AuditEvent]: ...
    def update_visibility(
        self,
        *,
        target_type: str,
        target_id: str,
        visibility: str,
        visible_team_ids: list[str],
    ) -> None: ...
    def list_entities(self) -> list[Entity]: ...
    def list_source_items(self) -> list[SourceItem]: ...
    def upsert_offline_index_state(self, state: OfflineIndexState) -> OfflineIndexState: ...
    def list_offline_index_states(
        self,
        *,
        status: str | None = None,
        source_item_id: str | None = None,
        object_type: str | None = None,
        limit: int | None = None,
    ) -> list[OfflineIndexState]: ...
    def mark_offline_index_dirty(
        self,
        *,
        object_type: str,
        object_id: str,
        owner_user_id: str,
        source_item_id: str | None = None,
        content_hash: str | None = None,
        visibility_version: str | None = None,
        dirty_reason: str,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        index_version: str = "hipporag_offline.v1",
    ) -> OfflineIndexState: ...
    def mark_offline_indexed(
        self,
        *,
        object_type: str,
        object_id: str,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        index_version: str = "hipporag_offline.v1",
    ) -> OfflineIndexState: ...
    def tombstone_offline_index_for_source(self, source_item_id: str, *, reason: str) -> list[OfflineIndexState]: ...
    def offline_index_status(self, *, owner_user_id: str | None = None) -> dict: ...
    def add_workspace_activity_event(self, event: WorkspaceActivityEvent) -> WorkspaceActivityEvent: ...
    def list_workspace_activity_events(
        self,
        *,
        owner_user_id: str,
        activity_types: set[str] | None = None,
        limit: int = 50,
    ) -> list[WorkspaceActivityEvent]: ...
    def upsert_discovery_item(self, item: DiscoveryItem) -> DiscoveryItem: ...
    def update_discovery_item_status(self, discovery_id: str, status: str) -> DiscoveryItem: ...
    def list_discovery_items(
        self,
        *,
        owner_user_id: str,
        status: str | None = None,
        since=None,
        limit: int = 50,
    ) -> list[DiscoveryItem]: ...
    def list_chunks_for_sources(self, source_item_ids: set[str]) -> list[Chunk]: ...
    def list_chunks_missing_embedding(self, *, provider: str, model: str, limit: int | None = None) -> list[Chunk]: ...
    def update_chunk_embedding(self, chunk_id: str, embedding: list[float], *, provider: str, model: str) -> None: ...
    def vector_search_chunks(self, source_item_ids: set[str], query_embedding: list[float], *, top_k: int) -> list[tuple[Chunk, float]]: ...
    def list_hyperedges_for_entities(self, entity_ids: set[str]) -> list[tuple[Hyperedge, list[HyperedgeMember]]]: ...
    def count_table(self, table: str) -> int: ...
    def claim_next_job(
        self,
        *,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        excluded_job_types: set[str] | None = None,
    ) -> Job | None: ...
    def lease_job(self, job_id: str, *, worker_id: str | None = None, lease_seconds: int | None = None) -> Job: ...
    def heartbeat_job(self, job_id: str, *, worker_id: str | None = None, lease_seconds: int | None = None, external_run_id: str | None = None) -> Job: ...
    def create_job(self, job_type: str, payload: dict, *, max_attempts: int = 3, priority: int = 0) -> Job: ...
    def list_jobs(self, *, status: str | None = None, job_type: str | None = None, limit: int = 50) -> list[Job]: ...
    def cancel_job(self, job_id: str, *, reason: str = "") -> Job: ...


class InMemoryKnowledgeStore:
    """Small deterministic store for tests and early agent integration."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.team_memberships: list[TeamMembership] = []
        self.source_items: dict[str, SourceItem] = {}
        self.source_items_by_hash: dict[str, str] = {}
        self.knowledge_sources: dict[str, KnowledgeSource] = {}
        self.sync_runs: dict[str, SyncRun] = {}
        self.connector_states: dict[str, ConnectorState] = {}
        self.documents: dict[str, Document] = {}
        self.chunks: dict[str, Chunk] = {}
        self.agent_memories: dict[str, AgentMemory] = {}
        self.profile_cards: dict[str, UserProfileCard] = {}
        self.entities: dict[str, Entity] = {}
        self.hyperedges: dict[str, Hyperedge] = {}
        self.hyperedge_members: list[HyperedgeMember] = []
        self.review_items: dict[str, ReviewItem] = {}
        self.audit_events: list[AuditEvent] = []
        self.jobs: dict[str, Job] = {}
        self.job_events: list[JobEvent] = []
        self.offline_index_states: dict[tuple[str, str], OfflineIndexState] = {}
        self.workspace_activity_events: list[WorkspaceActivityEvent] = []
        self.discovery_items: dict[str, DiscoveryItem] = {}

    def add_user(self, user: User) -> None:
        self.users[user.user_id] = user

    def get_user(self, user_id: str) -> User:
        return self.users[user_id]

    def add_team_membership(self, membership: TeamMembership) -> None:
        self.team_memberships.append(membership)

    def team_memberships_for_user(self, user_id: str) -> list[TeamMembership]:
        return [membership for membership in self.team_memberships if membership.user_id == user_id]

    def upsert_source_item(self, item: SourceItem) -> SourceItem:
        existing_id = self.source_items_by_hash.get(item.content_hash)
        if existing_id:
            return self.source_items[existing_id]
        self.source_items[item.source_item_id] = item
        self.source_items_by_hash[item.content_hash] = item.source_item_id
        return item

    def upsert_knowledge_source(self, source: KnowledgeSource) -> KnowledgeSource:
        source.updated_at = utc_now()
        if source.knowledge_source_id in self.knowledge_sources:
            existing = self.knowledge_sources[source.knowledge_source_id]
            source.created_at = existing.created_at
        self.knowledge_sources[source.knowledge_source_id] = source
        return source

    def get_knowledge_source(self, knowledge_source_id: str) -> KnowledgeSource:
        return self.knowledge_sources[knowledge_source_id]

    def list_knowledge_sources(
        self,
        *,
        owner_user_id: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[KnowledgeSource]:
        sources = list(self.knowledge_sources.values())
        if owner_user_id:
            sources = [source for source in sources if source.owner_user_id == owner_user_id]
        if source_type:
            sources = [source for source in sources if source.source_type == source_type]
        if status:
            sources = [source for source in sources if source.status == status]
        return sorted(sources, key=lambda source: (source.updated_at, source.name), reverse=True)

    def add_sync_run(self, run: SyncRun) -> SyncRun:
        self.sync_runs[run.sync_run_id] = run
        source = self.knowledge_sources.get(run.knowledge_source_id)
        if source:
            source.last_sync_at = run.finished_at or run.started_at
            source.last_error = run.error
            source.status = "failed" if run.status == "failed" else "indexed"
            source.updated_at = utc_now()
        return run

    def list_sync_runs(self, *, knowledge_source_id: str | None = None, owner_user_id: str | None = None, limit: int = 50) -> list[SyncRun]:
        runs = list(self.sync_runs.values())
        if knowledge_source_id:
            runs = [run for run in runs if run.knowledge_source_id == knowledge_source_id]
        if owner_user_id:
            runs = [run for run in runs if run.owner_user_id == owner_user_id]
        return sorted(runs, key=lambda run: run.started_at, reverse=True)[:limit]

    def upsert_connector_state(self, state: ConnectorState) -> ConnectorState:
        state.updated_at = utc_now()
        if state.connector_state_id in self.connector_states:
            existing = self.connector_states[state.connector_state_id]
            state.created_at = existing.created_at
        self.connector_states[state.connector_state_id] = state
        return state

    def get_connector_state(self, connector_state_id: str) -> ConnectorState:
        return self.connector_states[connector_state_id]

    def list_connector_states(self, *, owner_user_id: str | None = None, connector_id: str | None = None) -> list[ConnectorState]:
        states = list(self.connector_states.values())
        if owner_user_id:
            states = [state for state in states if state.owner_user_id == owner_user_id]
        if connector_id:
            states = [state for state in states if state.connector_id == connector_id]
        return sorted(states, key=lambda state: (state.updated_at, state.connector_state_id), reverse=True)

    def add_document(self, document: Document) -> None:
        self.documents[document.document_id] = document

    def add_chunk(self, chunk: Chunk) -> None:
        self.chunks[chunk.chunk_id] = chunk

    def add_agent_memory(self, memory: AgentMemory) -> None:
        self.agent_memories[memory.agent_memory_id] = memory

    def get_agent_memory(self, agent_memory_id: str) -> AgentMemory:
        return self.agent_memories[agent_memory_id]

    def list_agent_memories(self, *, owner_user_id: str) -> list[AgentMemory]:
        return [
            memory
            for memory in self.agent_memories.values()
            if memory.owner_user_id == owner_user_id
        ]

    def update_agent_memory_lifecycle(
        self,
        agent_memory_id: str,
        *,
        confidence: float,
        decay_policy: str,
        last_verified_at,
        source_refs: list[SourceRef] | None = None,
    ) -> AgentMemory:
        memory = self.agent_memories[agent_memory_id]
        memory.confidence = confidence
        memory.decay_policy = decay_policy
        memory.last_verified_at = last_verified_at
        if source_refs is not None:
            memory.source_refs = list(source_refs)
        return memory

    def add_profile_card(self, profile_card: UserProfileCard) -> None:
        self.profile_cards[profile_card.profile_card_id] = profile_card

    def update_profile_card_lifecycle(
        self,
        profile_card_id: str,
        *,
        confidence: float,
        source_refs: list[SourceRef],
        last_verified_at,
    ) -> UserProfileCard:
        card = self.profile_cards[profile_card_id]
        card.confidence = confidence
        card.source_refs = list(source_refs)
        card.last_verified_at = last_verified_at
        return card

    def list_profile_cards(self, *, owner_user_id: str) -> list[UserProfileCard]:
        return [
            card
            for card in self.profile_cards.values()
            if card.owner_user_id == owner_user_id
        ]

    def add_entity(self, entity: Entity) -> None:
        self.entities[entity.entity_id] = entity

    def add_hyperedge(self, hyperedge: Hyperedge, members: list[HyperedgeMember]) -> None:
        self.hyperedges[hyperedge.hyperedge_id] = hyperedge
        self.hyperedge_members.extend(members)

    def add_review_item(self, review_item: ReviewItem) -> None:
        self.review_items[review_item.review_item_id] = review_item

    def get_review_item(self, review_item_id: str) -> ReviewItem:
        return self.review_items[review_item_id]

    def list_review_items(self) -> list[ReviewItem]:
        return list(self.review_items.values())

    def update_review_item_status(self, review_item_id: str, status: str) -> ReviewItem:
        review_item = self.review_items[review_item_id]
        review_item.status = status
        return review_item

    def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        self.audit_events.append(event)
        return event

    def list_audit_events(self, target_type: str | None = None, target_id: str | None = None) -> list[AuditEvent]:
        events = self.audit_events
        if target_type is not None:
            events = [event for event in events if event.target_type == target_type]
        if target_id is not None:
            events = [event for event in events if event.target_id == target_id]
        return list(events)

    def update_visibility(
        self,
        *,
        target_type: str,
        target_id: str,
        visibility: str,
        visible_team_ids: list[str],
    ) -> None:
        targets = {
            "source_item": self.source_items,
            "document": self.documents,
            "chunk": self.chunks,
            "entity": self.entities,
            "hyperedge": self.hyperedges,
        }
        target_map = targets.get(target_type)
        if target_map is None:
            raise ValueError(f"Unsupported visibility target_type: {target_type}")
        target = target_map[target_id]
        target.visibility = Visibility(visibility)
        target.visible_team_ids = list(visible_team_ids)
        source_item_id = getattr(target, "source_item_id", target_id if target_type == "source_item" else None)
        owner_user_id = getattr(target, "owner_user_id", "")
        self.mark_offline_index_dirty(
            object_type=target_type,
            object_id=target_id,
            owner_user_id=owner_user_id,
            source_item_id=source_item_id,
            visibility_version=_visibility_version(owner_user_id, visibility, visible_team_ids),
            dirty_reason="visibility_changed",
        )

    def list_entities(self) -> list[Entity]:
        return list(self.entities.values())

    def list_source_items(self) -> list[SourceItem]:
        return list(self.source_items.values())

    def upsert_offline_index_state(self, state: OfflineIndexState) -> OfflineIndexState:
        state.updated_at = utc_now()
        self.offline_index_states[(state.object_type, state.object_id)] = state
        return state

    def list_offline_index_states(
        self,
        *,
        status: str | None = None,
        source_item_id: str | None = None,
        object_type: str | None = None,
        limit: int | None = None,
    ) -> list[OfflineIndexState]:
        states = list(self.offline_index_states.values())
        if status:
            states = [state for state in states if state.status == status]
        if source_item_id:
            states = [state for state in states if state.source_item_id == source_item_id]
        if object_type:
            states = [state for state in states if state.object_type == object_type]
        states = sorted(states, key=lambda state: (state.updated_at, state.object_type, state.object_id), reverse=True)
        return states[:limit] if limit else states

    def mark_offline_index_dirty(
        self,
        *,
        object_type: str,
        object_id: str,
        owner_user_id: str,
        source_item_id: str | None = None,
        content_hash: str | None = None,
        visibility_version: str | None = None,
        dirty_reason: str,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        index_version: str = "hipporag_offline.v1",
    ) -> OfflineIndexState:
        existing = self.offline_index_states.get((object_type, object_id))
        state = OfflineIndexState(
            object_type=object_type,
            object_id=object_id,
            owner_user_id=owner_user_id or (existing.owner_user_id if existing else ""),
            source_item_id=source_item_id if source_item_id is not None else (existing.source_item_id if existing else None),
            content_hash=content_hash if content_hash is not None else (existing.content_hash if existing else None),
            visibility_version=visibility_version if visibility_version is not None else (existing.visibility_version if existing else None),
            embedding_provider=embedding_provider if embedding_provider is not None else (existing.embedding_provider if existing else None),
            embedding_model=embedding_model if embedding_model is not None else (existing.embedding_model if existing else None),
            index_version=index_version,
            status="dirty",
            dirty_reason=dirty_reason,
            last_indexed_at=existing.last_indexed_at if existing else None,
        )
        return self.upsert_offline_index_state(state)

    def mark_offline_indexed(
        self,
        *,
        object_type: str,
        object_id: str,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        index_version: str = "hipporag_offline.v1",
    ) -> OfflineIndexState:
        existing = self.offline_index_states[(object_type, object_id)]
        existing.status = "indexed"
        existing.dirty_reason = None
        existing.embedding_provider = embedding_provider or existing.embedding_provider
        existing.embedding_model = embedding_model or existing.embedding_model
        existing.index_version = index_version
        existing.last_indexed_at = utc_now()
        existing.updated_at = existing.last_indexed_at
        return existing

    def tombstone_offline_index_for_source(self, source_item_id: str, *, reason: str) -> list[OfflineIndexState]:
        tombstoned = []
        for state in self.offline_index_states.values():
            if state.source_item_id != source_item_id and state.object_id != source_item_id:
                continue
            state.status = "tombstoned"
            state.dirty_reason = reason
            state.updated_at = utc_now()
            tombstoned.append(state)
        return tombstoned

    def offline_index_status(self, *, owner_user_id: str | None = None) -> dict:
        states = list(self.offline_index_states.values())
        if owner_user_id:
            states = [state for state in states if state.owner_user_id == owner_user_id]
        by_status: dict[str, int] = {}
        by_object_type: dict[str, int] = {}
        last_indexed = None
        for state in states:
            by_status[state.status] = by_status.get(state.status, 0) + 1
            by_object_type[state.object_type] = by_object_type.get(state.object_type, 0) + 1
            if state.last_indexed_at and (last_indexed is None or state.last_indexed_at > last_indexed):
                last_indexed = state.last_indexed_at
        return {
            "index_version": "hipporag_offline.v1",
            "total": len(states),
            "dirty": by_status.get("dirty", 0),
            "indexed": by_status.get("indexed", 0),
            "tombstoned": by_status.get("tombstoned", 0),
            "by_status": by_status,
            "by_object_type": by_object_type,
            "last_indexed_at": last_indexed.isoformat() if last_indexed else None,
        }

    def add_workspace_activity_event(self, event: WorkspaceActivityEvent) -> WorkspaceActivityEvent:
        self.workspace_activity_events.append(event)
        return event

    def list_workspace_activity_events(
        self,
        *,
        owner_user_id: str,
        activity_types: set[str] | None = None,
        limit: int = 50,
    ) -> list[WorkspaceActivityEvent]:
        events = [event for event in self.workspace_activity_events if event.owner_user_id == owner_user_id]
        if activity_types:
            events = [event for event in events if event.activity_type in activity_types]
        events = sorted(events, key=lambda event: (event.created_at, event.workspace_activity_event_id), reverse=True)
        return events[: max(0, limit)]

    def upsert_discovery_item(self, item: DiscoveryItem) -> DiscoveryItem:
        existing = self.discovery_items.get(item.discovery_id)
        if existing is None and item.fingerprint:
            existing = next(
                (
                    candidate
                    for candidate in self.discovery_items.values()
                    if candidate.owner_user_id == item.owner_user_id
                    and candidate.producer == item.producer
                    and candidate.fingerprint == item.fingerprint
                ),
                None,
            )
        if existing:
            existing.discovery_type = item.discovery_type
            existing.title = item.title
            existing.evidence = item.evidence
            existing.confidence = item.confidence
            existing.producer = item.producer
            existing.fingerprint = item.fingerprint
            existing.evidence_snapshot = item.evidence_snapshot
            existing.discovery_score = item.discovery_score
            existing.quality_signals = item.quality_signals
            return existing
        self.discovery_items[item.discovery_id] = item
        return item

    def update_discovery_item_status(self, discovery_id: str, status: str) -> DiscoveryItem:
        item = self.discovery_items[discovery_id]
        item.status = status
        return item

    def list_discovery_items(
        self,
        *,
        owner_user_id: str,
        status: str | None = None,
        since=None,
        limit: int = 50,
    ) -> list[DiscoveryItem]:
        items = [item for item in self.discovery_items.values() if item.owner_user_id == owner_user_id]
        if status:
            items = [item for item in items if item.status == status]
        if since is not None:
            items = [item for item in items if item.created_at >= since]
        items = sorted(items, key=lambda item: (item.discovery_score, item.created_at, item.discovery_id), reverse=True)
        return items[: max(0, limit)]

    def list_chunks_for_sources(self, source_item_ids: set[str]) -> list[Chunk]:
        return [chunk for chunk in self.chunks.values() if chunk.source_item_id in source_item_ids]

    def list_chunks_missing_embedding(self, *, provider: str, model: str, limit: int | None = None) -> list[Chunk]:
        chunks = [
            chunk
            for chunk in self.chunks.values()
            if chunk.embedding is None
            or chunk.metadata.get("embedding_provider") != provider
            or chunk.metadata.get("embedding_model") != model
        ]
        return chunks[:limit] if limit else chunks

    def update_chunk_embedding(self, chunk_id: str, embedding: list[float], *, provider: str, model: str) -> None:
        chunk = self.chunks[chunk_id]
        chunk.embedding = list(embedding)
        chunk.metadata["embedding_provider"] = provider
        chunk.metadata["embedding_model"] = model

    def vector_search_chunks(self, source_item_ids: set[str], query_embedding: list[float], *, top_k: int) -> list[tuple[Chunk, float]]:
        scored: list[tuple[Chunk, float]] = []
        for chunk in self.list_chunks_for_sources(source_item_ids):
            if not chunk.embedding:
                continue
            score = _cosine_similarity(query_embedding, chunk.embedding)
            scored.append((chunk, score))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:top_k]

    def list_hyperedges_for_entities(self, entity_ids: set[str]) -> list[tuple[Hyperedge, list[HyperedgeMember]]]:
        edge_ids = {
            member.hyperedge_id
            for member in self.hyperedge_members
            if member.entity_id in entity_ids
        }
        results: list[tuple[Hyperedge, list[HyperedgeMember]]] = []
        for edge_id in edge_ids:
            members = [member for member in self.hyperedge_members if member.hyperedge_id == edge_id]
            results.append((self.hyperedges[edge_id], members))
        return results

    def create_job(self, job_type: str, payload: dict, *, max_attempts: int = 3, priority: int = 0) -> Job:
        job = Job(
            job_id=f"job_{uuid4().hex}",
            job_type=job_type,
            payload=dict(payload),
            max_attempts=max_attempts,
            priority=priority,
            run_after=utc_now(),
            source_refs=_source_refs_from_payload(payload.get("source_refs")),
        )
        self.jobs[job.job_id] = job
        self.add_job_event(job.job_id, "queued", f"Queued {job_type} job", {"payload": payload, "priority": priority})
        return job

    def get_job(self, job_id: str) -> Job:
        return self.jobs[job_id]

    def list_jobs(self, *, status: str | None = None, job_type: str | None = None, limit: int = 50) -> list[Job]:
        jobs = list(self.jobs.values())
        if status:
            jobs = [job for job in jobs if job.status == status]
        if job_type:
            jobs = [job for job in jobs if job.job_type == job_type]
        return sorted(jobs, key=lambda job: (job.created_at, job.job_id), reverse=True)[:limit]

    def list_job_events(self, job_id: str) -> list[JobEvent]:
        return [event for event in self.job_events if event.job_id == job_id]

    def add_job_event(self, job_id: str, event_type: str, message: str, detail: dict | None = None) -> JobEvent:
        event = JobEvent(
            job_event_id=f"evt_{uuid4().hex}",
            job_id=job_id,
            event_type=event_type,
            message=message,
            detail=dict(detail or {}),
        )
        self.job_events.append(event)
        return event

    def claim_next_job(
        self,
        *,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        excluded_job_types: set[str] | None = None,
    ) -> Job | None:
        now = utc_now()
        excluded_job_types = excluded_job_types or set()
        queued = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status == "queued" and (job.run_after is None or job.run_after <= now)
                and job.job_type not in excluded_job_types
            ),
            key=lambda job: (-job.priority, job.run_after or job.created_at, job.created_at, job.job_id),
        )
        if not queued:
            return None
        job = queued[0]
        job.status = "running"
        job.attempts += 1
        job.error = None
        job.started_at = now
        job.finished_at = None
        job.worker_id = worker_id
        job.heartbeat_at = now
        job.leased_until = job.heartbeat_at + timedelta(seconds=lease_seconds) if lease_seconds else None
        job.updated_at = now
        self.add_job_event(
            job.job_id,
            "started",
            f"Started attempt {job.attempts}",
            {"worker_id": worker_id, "leased_until": job.leased_until.isoformat() if job.leased_until else None},
        )
        return job

    def lease_job(self, job_id: str, *, worker_id: str | None = None, lease_seconds: int | None = None) -> Job:
        job = self.jobs[job_id]
        now = utc_now()
        if job.status == "queued":
            if job.run_after is not None and job.run_after > now:
                raise ValueError(f"Job {job_id} is not ready until {job.run_after.isoformat()}")
            job.status = "running"
            job.attempts += 1
            job.started_at = now
            job.finished_at = None
            job.error = None
        elif job.status == "running":
            if job.worker_id and worker_id and job.worker_id != worker_id and job.leased_until and job.leased_until > now:
                raise ValueError(f"Job {job_id} is already leased by {job.worker_id}")
        else:
            raise ValueError(f"Only queued or running jobs can be leased, got {job.status}")
        job.worker_id = worker_id or job.worker_id
        job.heartbeat_at = now
        job.leased_until = now + timedelta(seconds=lease_seconds) if lease_seconds else None
        job.updated_at = now
        self.add_job_event(
            job.job_id,
            "leased",
            "Job leased",
            {"worker_id": job.worker_id, "leased_until": job.leased_until.isoformat() if job.leased_until else None},
        )
        return job

    def heartbeat_job(self, job_id: str, *, worker_id: str | None = None, lease_seconds: int | None = None, external_run_id: str | None = None) -> Job:
        job = self.jobs[job_id]
        if job.status != "running":
            raise ValueError(f"Only running jobs can heartbeat, got {job.status}")
        now = utc_now()
        job.worker_id = worker_id or job.worker_id
        job.heartbeat_at = now
        job.leased_until = now + timedelta(seconds=lease_seconds) if lease_seconds else job.leased_until
        if external_run_id:
            job.external_run_id = external_run_id
        job.updated_at = now
        self.add_job_event(
            job.job_id,
            "heartbeat",
            "Worker heartbeat",
            {
                "worker_id": job.worker_id,
                "leased_until": job.leased_until.isoformat() if job.leased_until else None,
                "external_run_id": job.external_run_id,
            },
        )
        return job

    def finish_job(self, job_id: str, result: dict) -> Job:
        job = self.jobs[job_id]
        job.status = "succeeded"
        job.result = dict(result)
        job.error = None
        job.finished_at = utc_now()
        job.leased_until = None
        job.updated_at = utc_now()
        self.add_job_event(job.job_id, "succeeded", "Job succeeded", {"result": result})
        return job

    def fail_job(self, job_id: str, error: str, *, retryable: bool = True) -> Job:
        job = self.jobs[job_id]
        job.status = "queued" if retryable and job.attempts < job.max_attempts else "failed"
        job.error = error
        delay_seconds = _retry_delay_seconds(job.payload, job.attempts) if job.status == "queued" else 0
        job.run_after = utc_now() + timedelta(seconds=delay_seconds) if job.status == "queued" else None
        if job.status == "failed":
            job.finished_at = utc_now()
        job.worker_id = None
        job.leased_until = None
        job.heartbeat_at = None
        job.updated_at = utc_now()
        event_type = "retry_queued" if job.status == "queued" else "failed"
        self.add_job_event(
            job.job_id,
            event_type,
            error,
            {"run_after": job.run_after.isoformat() if job.run_after else None, "backoff_seconds": delay_seconds},
        )
        return job

    def retry_job(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.status not in {"failed", "canceled"}:
            raise ValueError(f"Only failed or canceled jobs can be retried, got {job.status}")
        job.status = "queued"
        job.error = None
        job.run_after = utc_now()
        job.finished_at = None
        job.worker_id = None
        job.leased_until = None
        job.heartbeat_at = None
        job.external_run_id = None
        job.updated_at = utc_now()
        self.add_job_event(job.job_id, "retry_queued", "Job manually queued for retry")
        return job

    def cancel_job(self, job_id: str, *, reason: str = "") -> Job:
        job = self.jobs[job_id]
        if job.status in {"succeeded", "failed", "canceled"}:
            raise ValueError(f"Only queued or running jobs can be canceled, got {job.status}")
        now = utc_now()
        job.status = "canceled"
        job.error = reason or "Job canceled"
        job.finished_at = now
        job.worker_id = None
        job.leased_until = None
        job.heartbeat_at = None
        job.external_run_id = None
        job.updated_at = now
        self.add_job_event(job.job_id, "canceled", job.error)
        return job

    def recover_stale_jobs(self, *, max_age_seconds: int) -> list[Job]:
        now = utc_now()
        recovered: list[Job] = []
        for job in self.jobs.values():
            if job.status != "running" or job.started_at is None:
                continue
            if (now - job.started_at).total_seconds() < max_age_seconds:
                continue
            if job.attempts < job.max_attempts:
                job.status = "queued"
                job.error = "Recovered stale running job"
                job.run_after = now
                event_type = "stale_requeued"
            else:
                job.status = "failed"
                job.error = "Stale running job exceeded max attempts"
                job.run_after = None
                job.finished_at = now
                event_type = "stale_failed"
            job.worker_id = None
            job.leased_until = None
            job.heartbeat_at = None
            job.updated_at = now
            recovered.append(job)
            self.add_job_event(job.job_id, event_type, job.error)
        return recovered

    def count_table(self, table: str) -> int:
        tables = {
            "source_items": self.source_items,
            "connector_states": self.connector_states,
            "documents": self.documents,
            "chunks": self.chunks,
            "users": self.users,
            "entities": self.entities,
            "hyperedges": self.hyperedges,
            "review_items": self.review_items,
            "agent_memories": self.agent_memories,
            "user_profile_cards": self.profile_cards,
            "jobs": self.jobs,
            "job_events": self.job_events,
            "offline_index_states": self.offline_index_states,
            "workspace_activity_events": self.workspace_activity_events,
            "discovery_items": self.discovery_items,
        }
        if table not in tables:
            raise ValueError(f"Unsupported table: {table}")
        return len(tables[table])


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _source_refs_from_payload(value) -> list[SourceRef]:
    if not isinstance(value, list):
        return []
    allowed_keys = set(SourceRef.__dataclass_fields__)
    return [
        SourceRef(**{key: item for key, item in ref.items() if key in allowed_keys})
        for ref in value
        if isinstance(ref, dict)
    ]


def _visibility_version(owner_user_id: str, visibility: str, visible_team_ids: list[str]) -> str:
    return "|".join([owner_user_id, visibility, ",".join(sorted(visible_team_ids))])


def _retry_delay_seconds(payload: dict, attempts: int) -> int:
    raw = payload.get("retry_backoff_seconds", payload.get("backoff_seconds", 60)) if isinstance(payload, dict) else 60
    try:
        base = max(0, int(raw))
    except (TypeError, ValueError):
        base = 60
    exponent = max(0, attempts - 1)
    return min(base * (2**exponent), 3600)
