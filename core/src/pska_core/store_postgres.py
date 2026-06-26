from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, UserStatus, Visibility
from pska_core.models import (
    AgentMemory,
    Chunk,
    ConnectorState,
    DEFAULT_TENANT_ID,
    DiscoveryItem,
    Document,
    DigestNote,
    Entity,
    Hyperedge,
    HyperedgeMember,
    Job,
    JobEvent,
    AuditEvent,
    KnowledgeClaim,
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
from pska_core.serde import to_jsonable


class PostgresKnowledgeStore:
    """PostgreSQL implementation for the PSKA v1 store protocol."""

    def __init__(self, database_url: str = "postgresql:///pska") -> None:
        self.database_url = database_url

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            yield conn

    def add_user(self, user: User) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into users(user_id, handle, role, status, tenant_id)
                values (%s, %s, %s, %s, %s)
                on conflict (user_id) do update
                set handle = excluded.handle,
                    role = excluded.role,
                    status = excluded.status,
                    tenant_id = excluded.tenant_id,
                    updated_at = now()
                """,
                (user.user_id, user.handle, user.role.value, user.status.value, user.tenant_id),
            )

    def get_user(self, user_id: str, *, tenant_id: str | None = None) -> User:
        clauses = ["user_id = %s"]
        params: list[Any] = [user_id]
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        with self.connect() as conn:
            row = conn.execute(f"select * from users where {' and '.join(clauses)}", tuple(params)).fetchone()
        if not row:
            raise KeyError(user_id)
        return User(
            user_id=row["user_id"],
            handle=row["handle"],
            role=UserRole(row["role"]),
            status=UserStatus(row["status"]),
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def team_memberships_for_user(self, user_id: str, *, tenant_id: str | None = None) -> list[TeamMembership]:
        clauses = ["user_id = %s"]
        params: list[Any] = [user_id]
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        with self.connect() as conn:
            rows = conn.execute(f"select * from team_memberships where {' and '.join(clauses)}", tuple(params)).fetchall()
        return [
            TeamMembership(
                user_id=row["user_id"],
                team_id=row["team_id"],
                role=row["role"],
                tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
            )
            for row in rows
        ]

    def upsert_source_item(self, item: SourceItem) -> SourceItem:
        with self.connect() as conn:
            existing = conn.execute(
                "select * from source_items where tenant_id = %s and content_hash = %s",
                (item.tenant_id, item.content_hash),
            ).fetchone()
            if existing:
                return self._source_item_from_row(existing)
            existing_by_id = conn.execute(
                "select * from source_items where tenant_id = %s and source_item_id = %s",
                (item.tenant_id, item.source_item_id),
            ).fetchone()
            if existing_by_id:
                row = conn.execute(
                    """
                    update source_items
                    set source_channel = %s,
                        record_type = %s,
                        source_id = %s,
                        owner_user_id = %s,
                        space_id = %s,
                        visibility = %s,
                        visible_team_ids = %s,
                        title = %s,
                        url = %s,
                        content_text = %s,
                        content_hash = %s,
                        metadata = %s,
                        tenant_id = %s,
                        updated_at = now()
                    where source_item_id = %s
                    returning *
                    """,
                    (
                        item.source_channel,
                        item.record_type,
                        item.source_id,
                        item.owner_user_id,
                        item.space_id,
                        item.visibility.value,
                        item.visible_team_ids,
                        item.title,
                        item.url,
                        item.content_text,
                        item.content_hash,
                        Jsonb(to_jsonable(item.metadata)),
                        item.tenant_id,
                        item.source_item_id,
                    ),
                ).fetchone()
                return self._source_item_from_row(row)
            conn.execute(
                """
                insert into source_items(
                    source_item_id, source_channel, record_type, source_id, owner_user_id,
                    space_id, visibility, visible_team_ids, title, url, content_text,
                    content_hash, metadata, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    item.source_item_id,
                    item.source_channel,
                    item.record_type,
                    item.source_id,
                    item.owner_user_id,
                    item.space_id,
                    item.visibility.value,
                    item.visible_team_ids,
                    item.title,
                    item.url,
                    item.content_text,
                    item.content_hash,
                    Jsonb(to_jsonable(item.metadata)),
                    item.tenant_id,
                ),
            )
            return item

    def upsert_connector_state(self, state: ConnectorState) -> ConnectorState:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into connector_states(
                    connector_state_id, connector_id, owner_user_id, enabled, scan_cursor,
                    sync_status, last_success_at, last_error_at, last_error, permission_scope, config,
                    tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (connector_state_id) do update
                set connector_id = excluded.connector_id,
                    owner_user_id = excluded.owner_user_id,
                    tenant_id = excluded.tenant_id,
                    enabled = excluded.enabled,
                    scan_cursor = excluded.scan_cursor,
                    sync_status = excluded.sync_status,
                    last_success_at = excluded.last_success_at,
                    last_error_at = excluded.last_error_at,
                    last_error = excluded.last_error,
                    permission_scope = excluded.permission_scope,
                    config = excluded.config,
                    updated_at = now()
                returning *
                """,
                (
                    state.connector_state_id,
                    state.connector_id,
                    state.owner_user_id,
                    state.enabled,
                    state.scan_cursor,
                    state.sync_status,
                    state.last_success_at,
                    state.last_error_at,
                    state.last_error,
                    Jsonb(to_jsonable(state.permission_scope)),
                    Jsonb(to_jsonable(state.config)),
                    state.tenant_id,
                ),
            ).fetchone()
        return self._connector_state_from_row(row)

    def upsert_knowledge_source(self, source: KnowledgeSource) -> KnowledgeSource:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into knowledge_sources(
                    knowledge_source_id, owner_user_id, name, source_type, uri,
                    mode, status, connector_id, space_id, visibility, visible_team_ids,
                    permission_scope, config, last_sync_at, last_error, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, owner_user_id, uri) do update
                set name = excluded.name,
                    source_type = excluded.source_type,
                    mode = excluded.mode,
                    status = excluded.status,
                    connector_id = excluded.connector_id,
                    space_id = excluded.space_id,
                    visibility = excluded.visibility,
                    visible_team_ids = excluded.visible_team_ids,
                    tenant_id = excluded.tenant_id,
                    permission_scope = excluded.permission_scope,
                    config = excluded.config,
                    last_sync_at = coalesce(knowledge_sources.last_sync_at, excluded.last_sync_at),
                    last_error = excluded.last_error,
                    updated_at = now()
                returning *
                """,
                (
                    source.knowledge_source_id,
                    source.owner_user_id,
                    source.name,
                    source.source_type,
                    source.uri,
                    source.mode,
                    source.status,
                    source.connector_id,
                    source.space_id,
                    source.visibility.value,
                    source.visible_team_ids,
                    Jsonb(to_jsonable(source.permission_scope)),
                    Jsonb(to_jsonable(source.config)),
                    source.last_sync_at,
                    source.last_error,
                    source.tenant_id,
                ),
            ).fetchone()
        return self._knowledge_source_from_row(row)

    def get_knowledge_source(self, knowledge_source_id: str) -> KnowledgeSource:
        with self.connect() as conn:
            row = conn.execute("select * from knowledge_sources where knowledge_source_id = %s", (knowledge_source_id,)).fetchone()
        if not row:
            raise KeyError(knowledge_source_id)
        return self._knowledge_source_from_row(row)

    def list_knowledge_sources(
        self,
        *,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[KnowledgeSource]:
        clauses: list[str] = []
        params: list[str] = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if owner_user_id:
            clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        if source_type:
            clauses.append("source_type = %s")
            params.append(source_type)
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = " where " + " and ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from knowledge_sources{where} order by updated_at desc, name",  # noqa: S608 - fixed clauses only.
                params,
            ).fetchall()
        return [self._knowledge_source_from_row(row) for row in rows]

    def add_sync_run(self, run: SyncRun) -> SyncRun:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into sync_runs(
                    sync_run_id, knowledge_source_id, owner_user_id, connector_id, status,
                    started_at, finished_at, scanned, ingested, new_files, changed_files,
                    unchanged_files, moved_files, missing_files, skipped, failed, error, report,
                    tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    run.sync_run_id,
                    run.knowledge_source_id,
                    run.owner_user_id,
                    run.connector_id,
                    run.status,
                    run.started_at,
                    run.finished_at,
                    run.scanned,
                    run.ingested,
                    run.new_files,
                    run.changed_files,
                    run.unchanged_files,
                    run.moved_files,
                    run.missing_files,
                    run.skipped,
                    run.failed,
                    run.error,
                    Jsonb(to_jsonable(run.report)),
                    run.tenant_id,
                ),
            ).fetchone()
            conn.execute(
                """
                update knowledge_sources
                set status = %s,
                    last_sync_at = coalesce(%s, now()),
                    last_error = %s,
                    updated_at = now()
                where knowledge_source_id = %s
                """,
                ("failed" if run.status == "failed" else "indexed", run.finished_at or run.started_at, run.error, run.knowledge_source_id),
            )
        return self._sync_run_from_row(row)

    def list_sync_runs(self, *, tenant_id: str | None = None, knowledge_source_id: str | None = None, owner_user_id: str | None = None, limit: int = 50) -> list[SyncRun]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if knowledge_source_id:
            clauses.append("knowledge_source_id = %s")
            params.append(knowledge_source_id)
        if owner_user_id:
            clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        where = " where " + " and ".join(clauses) if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from sync_runs{where} order by started_at desc limit %s",  # noqa: S608 - fixed clauses only.
                params,
            ).fetchall()
        return [self._sync_run_from_row(row) for row in rows]

    def get_connector_state(self, connector_state_id: str) -> ConnectorState:
        with self.connect() as conn:
            row = conn.execute("select * from connector_states where connector_state_id = %s", (connector_state_id,)).fetchone()
        if not row:
            raise KeyError(connector_state_id)
        return self._connector_state_from_row(row)

    def list_connector_states(self, *, tenant_id: str | None = None, owner_user_id: str | None = None, connector_id: str | None = None) -> list[ConnectorState]:
        clauses: list[str] = []
        params: list[str] = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if owner_user_id:
            clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        if connector_id:
            clauses.append("connector_id = %s")
            params.append(connector_id)
        where = " where " + " and ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from connector_states{where} order by updated_at desc, connector_state_id",  # noqa: S608 - fixed clauses only.
                params,
            ).fetchall()
        return [self._connector_state_from_row(row) for row in rows]

    def add_document(self, document: Document) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into documents(
                    document_id, source_item_id, owner_user_id, space_id,
                    visibility, visible_team_ids, title, body, metadata, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (document_id) do nothing
                """,
                (
                    document.document_id,
                    document.source_item_id,
                    document.owner_user_id,
                    document.space_id,
                    document.visibility.value,
                    document.visible_team_ids,
                    document.title,
                    document.body,
                    Jsonb(to_jsonable(document.metadata)),
                    document.tenant_id,
                ),
            )

    def add_chunk(self, chunk: Chunk) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into chunks(
                    chunk_id, document_id, source_item_id, owner_user_id, space_id,
                    visibility, visible_team_ids, ordinal, text, embedding,
                    embedding_provider, embedding_model, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                on conflict (chunk_id) do nothing
                """,
                (
                    chunk.chunk_id,
                    chunk.document_id,
                    chunk.source_item_id,
                    chunk.owner_user_id,
                    chunk.space_id,
                    chunk.visibility.value,
                    chunk.visible_team_ids,
                    chunk.ordinal,
                    chunk.text,
                    _vector_literal(chunk.embedding) if chunk.embedding else None,
                    chunk.metadata.get("embedding_provider") if chunk.metadata else None,
                    chunk.metadata.get("embedding_model") if chunk.metadata else None,
                    chunk.tenant_id,
                ),
            )

    def replace_source_documents(self, source_item_id: str, documents: list[Document], chunks: list[Chunk]) -> None:
        with self.connect() as conn:
            conn.execute("delete from chunks where source_item_id = %s", (source_item_id,))
            conn.execute("delete from documents where source_item_id = %s", (source_item_id,))
            for document in documents:
                conn.execute(
                    """
                    insert into documents(
                        document_id, source_item_id, owner_user_id, space_id,
                        visibility, visible_team_ids, title, body, metadata, tenant_id
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        document.document_id,
                        document.source_item_id,
                        document.owner_user_id,
                        document.space_id,
                        document.visibility.value,
                        document.visible_team_ids,
                        document.title,
                        document.body,
                        Jsonb(to_jsonable(document.metadata)),
                        document.tenant_id,
                    ),
                )
            for chunk in chunks:
                conn.execute(
                    """
                    insert into chunks(
                        chunk_id, document_id, source_item_id, owner_user_id, space_id,
                        visibility, visible_team_ids, ordinal, text, embedding,
                        embedding_provider, embedding_model, tenant_id
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.source_item_id,
                        chunk.owner_user_id,
                        chunk.space_id,
                        chunk.visibility.value,
                        chunk.visible_team_ids,
                        chunk.ordinal,
                        chunk.text,
                        _vector_literal(chunk.embedding) if chunk.embedding else None,
                        chunk.metadata.get("embedding_provider") if chunk.metadata else None,
                        chunk.metadata.get("embedding_model") if chunk.metadata else None,
                        chunk.tenant_id,
                    ),
                )

    def add_agent_memory(self, memory: AgentMemory) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into agent_memories(
                    agent_memory_id, owner_user_id, created_by_user_id, layer, text,
                    confidence, source_refs, decay_policy, last_verified_at, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (agent_memory_id) do nothing
                """,
                (
                    memory.agent_memory_id,
                    memory.owner_user_id,
                    memory.created_by_user_id,
                    memory.layer.value if isinstance(memory.layer, MemoryLayer) else memory.layer,
                    memory.text,
                    memory.confidence,
                    Jsonb(to_jsonable(memory.source_refs)),
                    memory.decay_policy,
                    memory.last_verified_at,
                    memory.tenant_id,
                ),
            )

    def get_agent_memory(self, agent_memory_id: str) -> AgentMemory:
        with self.connect() as conn:
            row = conn.execute("select * from agent_memories where agent_memory_id = %s", (agent_memory_id,)).fetchone()
        if not row:
            raise KeyError(agent_memory_id)
        return self._agent_memory_from_row(row)

    def list_agent_memories(self, *, owner_user_id: str, tenant_id: str | None = None) -> list[AgentMemory]:
        tenant_clause = "and tenant_id = %s" if tenant_id else ""
        params: tuple[Any, ...] = (owner_user_id, tenant_id) if tenant_id else (owner_user_id,)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select * from agent_memories
                where owner_user_id = %s
                {tenant_clause}
                order by confidence desc, updated_at desc, agent_memory_id
                """,
                params,
            ).fetchall()
        return [self._agent_memory_from_row(row) for row in rows]

    def update_agent_memory_lifecycle(
        self,
        agent_memory_id: str,
        *,
        confidence: float,
        decay_policy: str,
        last_verified_at,
        source_refs: list[SourceRef] | None = None,
    ) -> AgentMemory:
        with self.connect() as conn:
            if source_refs is None:
                row = conn.execute(
                    """
                    update agent_memories
                    set confidence = %s, decay_policy = %s, last_verified_at = %s, updated_at = now()
                    where agent_memory_id = %s
                    returning *
                    """,
                    (confidence, decay_policy, last_verified_at, agent_memory_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    update agent_memories
                    set confidence = %s, decay_policy = %s, last_verified_at = %s, source_refs = %s, updated_at = now()
                    where agent_memory_id = %s
                    returning *
                    """,
                    (confidence, decay_policy, last_verified_at, Jsonb(to_jsonable(source_refs)), agent_memory_id),
                ).fetchone()
        if not row:
            raise KeyError(agent_memory_id)
        return self._agent_memory_from_row(row)

    def add_profile_card(self, profile_card: UserProfileCard) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into user_profile_cards(profile_card_id, owner_user_id, profile, confidence, source_refs, tenant_id)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (profile_card_id) do nothing
                """,
                (
                    profile_card.profile_card_id,
                    profile_card.owner_user_id,
                    Jsonb(to_jsonable(profile_card.profile)),
                    profile_card.confidence,
                    Jsonb(to_jsonable(profile_card.source_refs)),
                    profile_card.tenant_id,
                ),
            )

    def update_profile_card_lifecycle(
        self,
        profile_card_id: str,
        *,
        confidence: float,
        source_refs: list[SourceRef],
        last_verified_at,
    ) -> UserProfileCard:
        with self.connect() as conn:
            row = conn.execute(
                """
                update user_profile_cards
                set confidence = %s, source_refs = %s, updated_at = %s
                where profile_card_id = %s
                returning *
                """,
                (confidence, Jsonb(to_jsonable(source_refs)), last_verified_at, profile_card_id),
            ).fetchone()
        if not row:
            raise KeyError(profile_card_id)
        return self._profile_card_from_row(row)

    def list_profile_cards(self, *, owner_user_id: str, tenant_id: str | None = None) -> list[UserProfileCard]:
        tenant_clause = "and tenant_id = %s" if tenant_id else ""
        params: tuple[Any, ...] = (owner_user_id, tenant_id) if tenant_id else (owner_user_id,)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select * from user_profile_cards
                where owner_user_id = %s
                {tenant_clause}
                order by confidence desc, updated_at desc, profile_card_id
                """,
                params,
            ).fetchall()
        return [self._profile_card_from_row(row) for row in rows]

    def add_entity(self, entity: Entity) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into entities(entity_id, entity_type, label, owner_user_id, space_id, visibility, visible_team_ids, metadata, tenant_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (entity_id) do nothing
                """,
                (
                    entity.entity_id,
                    entity.entity_type,
                    entity.label,
                    entity.owner_user_id,
                    entity.space_id,
                    entity.visibility.value,
                    entity.visible_team_ids,
                    Jsonb(to_jsonable(entity.metadata)),
                    entity.tenant_id,
                ),
            )

    def add_hyperedge(self, hyperedge: Hyperedge, members: list[HyperedgeMember]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into hyperedges(
                    hyperedge_id, relation_type, owner_user_id, space_id, visibility,
                    visible_team_ids, directionality, evidence_text, source_refs, confidence,
                    tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (hyperedge_id) do nothing
                """,
                (
                    hyperedge.hyperedge_id,
                    hyperedge.relation_type,
                    hyperedge.owner_user_id,
                    hyperedge.space_id,
                    hyperedge.visibility.value,
                    hyperedge.visible_team_ids,
                    hyperedge.directionality.value
                    if isinstance(hyperedge.directionality, Directionality)
                    else hyperedge.directionality,
                    hyperedge.evidence_text,
                    Jsonb(to_jsonable(hyperedge.source_refs)),
                    hyperedge.confidence,
                    hyperedge.tenant_id,
                ),
            )
            for member in members:
                conn.execute(
                    """
                    insert into hyperedge_members(hyperedge_id, entity_id, role, ordinal)
                    values (%s, %s, %s, %s)
                    on conflict do nothing
                    """,
                    (member.hyperedge_id, member.entity_id, member.role, member.ordinal),
                )

    def add_knowledge_claim(self, claim: KnowledgeClaim) -> KnowledgeClaim:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into knowledge_claims(
                    knowledge_claim_id, owner_user_id, claim_type, statement,
                    subject, predicate, object, qualifiers, evidence_text,
                    source_refs, confidence, producer, job_id, request_id,
                    metadata, created_at, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (knowledge_claim_id) do update
                set claim_type = excluded.claim_type,
                    statement = excluded.statement,
                    subject = excluded.subject,
                    predicate = excluded.predicate,
                    object = excluded.object,
                    qualifiers = excluded.qualifiers,
                    evidence_text = excluded.evidence_text,
                    source_refs = excluded.source_refs,
                    confidence = excluded.confidence,
                    producer = excluded.producer,
                    job_id = excluded.job_id,
                    request_id = excluded.request_id,
                    metadata = excluded.metadata,
                    tenant_id = excluded.tenant_id
                returning *
                """,
                (
                    claim.knowledge_claim_id,
                    claim.owner_user_id,
                    claim.claim_type,
                    claim.statement,
                    claim.subject,
                    claim.predicate,
                    claim.object,
                    Jsonb(to_jsonable(claim.qualifiers)),
                    claim.evidence_text,
                    Jsonb(to_jsonable(claim.source_refs)),
                    claim.confidence,
                    claim.producer,
                    claim.job_id,
                    claim.request_id,
                    Jsonb(to_jsonable(claim.metadata)),
                    claim.created_at,
                    claim.tenant_id,
                ),
            ).fetchone()
        return self._knowledge_claim_from_row(row)

    def list_knowledge_claims(
        self,
        *,
        owner_user_id: str,
        tenant_id: str | None = None,
        source_item_ids: set[str] | None = None,
        job_id: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeClaim]:
        clauses = ["owner_user_id = %s"]
        params: list[Any] = [owner_user_id]
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if job_id:
            clauses.append("job_id = %s")
            params.append(job_id)
        if source_item_ids:
            clauses.append("(" + " or ".join(["source_refs @> %s"] * len(source_item_ids)) + ")")
            params.extend(Jsonb([{"source_item_id": source_item_id}]) for source_item_id in sorted(source_item_ids))
        params.append(max(0, limit))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from knowledge_claims
                where {" and ".join(clauses)}
                order by created_at desc, knowledge_claim_id desc
                limit %s
                """,  # noqa: S608 - clauses are fixed.
                params,
            ).fetchall()
        return [self._knowledge_claim_from_row(row) for row in rows]

    def add_digest_note(self, note: DigestNote) -> DigestNote:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into digest_notes(
                    digest_note_id, owner_user_id, title, synopsis, key_points,
                    actions, open_questions, risks, memory_suggestions,
                    relationship_suggestions, source_refs, confidence, producer,
                    job_id, request_id, metadata, created_at, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (digest_note_id) do update
                set title = excluded.title,
                    synopsis = excluded.synopsis,
                    key_points = excluded.key_points,
                    actions = excluded.actions,
                    open_questions = excluded.open_questions,
                    risks = excluded.risks,
                    memory_suggestions = excluded.memory_suggestions,
                    relationship_suggestions = excluded.relationship_suggestions,
                    source_refs = excluded.source_refs,
                    confidence = excluded.confidence,
                    producer = excluded.producer,
                    job_id = excluded.job_id,
                    request_id = excluded.request_id,
                    metadata = excluded.metadata,
                    tenant_id = excluded.tenant_id
                returning *
                """,
                (
                    note.digest_note_id,
                    note.owner_user_id,
                    note.title,
                    note.synopsis,
                    Jsonb(to_jsonable(note.key_points)),
                    Jsonb(to_jsonable(note.actions)),
                    Jsonb(to_jsonable(note.open_questions)),
                    Jsonb(to_jsonable(note.risks)),
                    Jsonb(to_jsonable(note.memory_suggestions)),
                    Jsonb(to_jsonable(note.relationship_suggestions)),
                    Jsonb(to_jsonable(note.source_refs)),
                    note.confidence,
                    note.producer,
                    note.job_id,
                    note.request_id,
                    Jsonb(to_jsonable(note.metadata)),
                    note.created_at,
                    note.tenant_id,
                ),
            ).fetchone()
        return self._digest_note_from_row(row)

    def list_digest_notes(
        self,
        *,
        owner_user_id: str,
        tenant_id: str | None = None,
        source_item_ids: set[str] | None = None,
        job_id: str | None = None,
        limit: int = 50,
    ) -> list[DigestNote]:
        clauses = ["owner_user_id = %s"]
        params: list[Any] = [owner_user_id]
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if job_id:
            clauses.append("job_id = %s")
            params.append(job_id)
        if source_item_ids:
            clauses.append("(" + " or ".join(["source_refs @> %s"] * len(source_item_ids)) + ")")
            params.extend(Jsonb([{"source_item_id": source_item_id}]) for source_item_id in sorted(source_item_ids))
        params.append(max(0, limit))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from digest_notes
                where {" and ".join(clauses)}
                order by created_at desc, digest_note_id desc
                limit %s
                """,  # noqa: S608 - clauses are fixed.
                params,
            ).fetchall()
        return [self._digest_note_from_row(row) for row in rows]

    def add_review_item(self, review_item: ReviewItem) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into review_items(review_item_id, owner_user_id, review_type, title, proposal, status, tenant_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (review_item_id) do nothing
                """,
                (
                    review_item.review_item_id,
                    review_item.owner_user_id,
                    review_item.review_type.value if isinstance(review_item.review_type, ReviewType) else review_item.review_type,
                    review_item.title,
                    Jsonb(to_jsonable(review_item.proposal)),
                    review_item.status,
                    review_item.tenant_id,
                ),
            )

    def get_review_item(self, review_item_id: str) -> ReviewItem:
        with self.connect() as conn:
            row = conn.execute("select * from review_items where review_item_id = %s", (review_item_id,)).fetchone()
        if not row:
            raise KeyError(review_item_id)
        return self._review_item_from_row(row)

    def list_review_items(self, *, tenant_id: str | None = None) -> list[ReviewItem]:
        where = "where tenant_id = %s" if tenant_id else ""
        params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from review_items {where} order by created_at, review_item_id",
                params,
            ).fetchall()
        return [self._review_item_from_row(row) for row in rows]

    def update_review_item_status(self, review_item_id: str, status: str) -> ReviewItem:
        with self.connect() as conn:
            row = conn.execute(
                """
                update review_items
                set status = %s, updated_at = now()
                where review_item_id = %s
                returning *
                """,
                (status, review_item_id),
            ).fetchone()
        if not row:
            raise KeyError(review_item_id)
        return self._review_item_from_row(row)

    def update_review_item_proposal(self, review_item_id: str, proposal: dict[str, Any]) -> ReviewItem:
        with self.connect() as conn:
            row = conn.execute(
                """
                update review_items
                set proposal = %s, updated_at = now()
                where review_item_id = %s
                returning *
                """,
                (Jsonb(to_jsonable(proposal)), review_item_id),
            ).fetchone()
        if not row:
            raise KeyError(review_item_id)
        return self._review_item_from_row(row)

    def replace_graph_projection(self, *, owner_user_id: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], tenant_id: str | None = None) -> dict[str, int]:
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        with self.connect() as conn:
            conn.execute("delete from graph_edges where tenant_id = %s and owner_user_id = %s", (tenant_id, owner_user_id))
            conn.execute("delete from graph_nodes where tenant_id = %s and owner_user_id = %s", (tenant_id, owner_user_id))
            for node in nodes:
                node_id = str(node.get("id") or "")
                if not node_id:
                    continue
                conn.execute(
                    """
                    insert into graph_nodes(
                        graph_node_id, owner_user_id, node_type, object_type, object_id,
                        label, summary, source_refs, confidence, metadata, tenant_id, updated_at
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    """,
                    (
                        node_id,
                        owner_user_id,
                        str(node.get("type") or ""),
                        str(node.get("object_type") or node.get("type") or ""),
                        str(node.get("object_id") or node_id),
                        str(node.get("label") or ""),
                        str(node.get("summary") or ""),
                        Jsonb(to_jsonable(node.get("source_refs") or [])),
                        node.get("confidence"),
                        Jsonb(to_jsonable({key: value for key, value in node.items() if key not in {"id", "type", "object_type", "object_id", "label", "summary", "source_refs", "confidence"}})),
                        tenant_id,
                    ),
                )
            for edge in edges:
                edge_id = str(edge.get("id") or "")
                source_id = str(edge.get("source") or "")
                target_id = str(edge.get("target") or "")
                if not edge_id or not source_id or not target_id:
                    continue
                conn.execute(
                    """
                    insert into graph_edges(
                        graph_edge_id, owner_user_id, edge_type, source_graph_node_id,
                        target_graph_node_id, label, source_refs, confidence, metadata, tenant_id, updated_at
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    """,
                    (
                        edge_id,
                        owner_user_id,
                        str(edge.get("type") or ""),
                        source_id,
                        target_id,
                        str(edge.get("label") or edge.get("type") or ""),
                        Jsonb(to_jsonable(edge.get("source_refs") or [])),
                        edge.get("confidence"),
                        Jsonb(to_jsonable({key: value for key, value in edge.items() if key not in {"id", "source", "target", "type", "label", "source_refs", "confidence"}})),
                        tenant_id,
                    ),
                )
        return {"graph_nodes": len(nodes), "graph_edges": len(edges)}

    def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self.connect() as conn:
            conn.execute(
                """
                insert into audit_events(audit_event_id, actor_user_id, action, target_type, target_id, decision, metadata, tenant_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (audit_event_id) do nothing
                """,
                (
                    event.audit_event_id,
                    event.actor_user_id,
                    event.action,
                    event.target_type,
                    event.target_id,
                    event.decision,
                    Jsonb(to_jsonable(event.metadata)),
                    event.tenant_id,
                ),
            )
        return event

    def list_audit_events(self, target_type: str | None = None, target_id: str | None = None) -> list[AuditEvent]:
        clauses: list[str] = []
        params: list[str] = []
        if target_type is not None:
            clauses.append("target_type = %s")
            params.append(target_type)
        if target_id is not None:
            clauses.append("target_id = %s")
            params.append(target_id)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from audit_events {where} order by created_at, audit_event_id",
                tuple(params),
            ).fetchall()
        return [
            AuditEvent(
                audit_event_id=row["audit_event_id"],
                actor_user_id=row["actor_user_id"],
                action=row["action"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                decision=row["decision"],
                metadata=dict(row["metadata"] or {}),
                tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
            )
            for row in rows
        ]

    def update_visibility(
        self,
        *,
        target_type: str,
        target_id: str,
        visibility: str,
        visible_team_ids: list[str],
    ) -> None:
        tables = {
            "source_item": ("source_items", "source_item_id"),
            "document": ("documents", "document_id"),
            "chunk": ("chunks", "chunk_id"),
            "entity": ("entities", "entity_id"),
            "hyperedge": ("hyperedges", "hyperedge_id"),
        }
        table_info = tables.get(target_type)
        if table_info is None:
            raise ValueError(f"Unsupported visibility target_type: {target_type}")
        table, id_column = table_info
        with self.connect() as conn:
            row = conn.execute(
                f"""
                update {table}
                set visibility = %s, visible_team_ids = %s
                where {id_column} = %s
                returning *
                """,
                (visibility, visible_team_ids, target_id),
            ).fetchone()
        if not row:
            raise KeyError(target_id)
        source_item_id = row.get("source_item_id") or (target_id if target_type == "source_item" else None)
        self.mark_offline_index_dirty(
            object_type=target_type,
            object_id=target_id,
            owner_user_id=str(row.get("owner_user_id") or ""),
            source_item_id=source_item_id,
            visibility_version=_visibility_version(str(row.get("owner_user_id") or ""), visibility, visible_team_ids),
            dirty_reason="visibility_changed",
            tenant_id=str(row.get("tenant_id") or DEFAULT_TENANT_ID),
        )

    def list_source_items(self, *, tenant_id: str | None = None) -> list[SourceItem]:
        where = "where tenant_id = %s" if tenant_id else ""
        params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from source_items {where} order by created_at, source_item_id",
                params,
            ).fetchall()
        return [self._source_item_from_row(row) for row in rows]

    def upsert_offline_index_state(self, state: OfflineIndexState) -> OfflineIndexState:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into offline_index_states(
                    object_type, object_id, owner_user_id, source_item_id, content_hash, mtime,
                    visibility_version, embedding_provider, embedding_model, index_version,
                    status, dirty_reason, last_indexed_at, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (object_type, object_id) do update
                set owner_user_id = excluded.owner_user_id,
                    source_item_id = excluded.source_item_id,
                    content_hash = excluded.content_hash,
                    mtime = excluded.mtime,
                    visibility_version = excluded.visibility_version,
                    embedding_provider = excluded.embedding_provider,
                    embedding_model = excluded.embedding_model,
                    index_version = excluded.index_version,
                    status = excluded.status,
                    dirty_reason = excluded.dirty_reason,
                    last_indexed_at = excluded.last_indexed_at,
                    tenant_id = excluded.tenant_id,
                    updated_at = now()
                returning *
                """,
                (
                    state.object_type,
                    state.object_id,
                    state.owner_user_id,
                    state.source_item_id,
                    state.content_hash,
                    state.mtime,
                    state.visibility_version,
                    state.embedding_provider,
                    state.embedding_model,
                    state.index_version,
                    state.status,
                    state.dirty_reason,
                    state.last_indexed_at,
                    state.tenant_id,
                ),
            ).fetchone()
        return self._offline_index_state_from_row(row)

    def list_offline_index_states(
        self,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        source_item_id: str | None = None,
        object_type: str | None = None,
        limit: int | None = None,
    ) -> list[OfflineIndexState]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if status:
            clauses.append("status = %s")
            params.append(status)
        if source_item_id:
            clauses.append("source_item_id = %s")
            params.append(source_item_id)
        if object_type:
            clauses.append("object_type = %s")
            params.append(object_type)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        params.append(limit if limit is not None else 2147483647)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select * from offline_index_states
                {where}
                order by updated_at desc, object_type, object_id
                limit %s
                """,
                tuple(params),
            ).fetchall()
        return [self._offline_index_state_from_row(row) for row in rows]

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
        tenant_id: str | None = None,
    ) -> OfflineIndexState:
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        with self.connect() as conn:
            existing = conn.execute(
                "select * from offline_index_states where tenant_id = %s and object_type = %s and object_id = %s",
                (tenant_id, object_type, object_id),
            ).fetchone()
        state = OfflineIndexState(
            object_type=object_type,
            object_id=object_id,
            owner_user_id=owner_user_id or (existing["owner_user_id"] if existing else ""),
            source_item_id=source_item_id if source_item_id is not None else (existing["source_item_id"] if existing else None),
            content_hash=content_hash if content_hash is not None else (existing["content_hash"] if existing else None),
            visibility_version=visibility_version if visibility_version is not None else (existing["visibility_version"] if existing else None),
            embedding_provider=embedding_provider if embedding_provider is not None else (existing["embedding_provider"] if existing else None),
            embedding_model=embedding_model if embedding_model is not None else (existing["embedding_model"] if existing else None),
            index_version=index_version,
            status="dirty",
            dirty_reason=dirty_reason,
            last_indexed_at=existing["last_indexed_at"] if existing else None,
            tenant_id=tenant_id,
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
        with self.connect() as conn:
            row = conn.execute(
                """
                update offline_index_states
                set status = 'indexed',
                    dirty_reason = null,
                    embedding_provider = coalesce(%s, embedding_provider),
                    embedding_model = coalesce(%s, embedding_model),
                    index_version = %s,
                    last_indexed_at = now(),
                    updated_at = now()
                where object_type = %s and object_id = %s
                returning *
                """,
                (embedding_provider, embedding_model, index_version, object_type, object_id),
            ).fetchone()
        if not row:
            raise KeyError(f"{object_type}:{object_id}")
        return self._offline_index_state_from_row(row)

    def tombstone_offline_index_for_source(self, source_item_id: str, *, reason: str) -> list[OfflineIndexState]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                update offline_index_states
                set status = 'tombstoned',
                    dirty_reason = %s,
                    updated_at = now()
                where source_item_id = %s or (object_type = 'source_item' and object_id = %s)
                returning *
                """,
                (reason, source_item_id, source_item_id),
            ).fetchall()
        return [self._offline_index_state_from_row(row) for row in rows]

    def offline_index_status(self, *, tenant_id: str | None = None, owner_user_id: str | None = None) -> dict:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if owner_user_id:
            clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        clause = f"where {' and '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select status, object_type, count(*) as count, max(last_indexed_at) as last_indexed_at
                from offline_index_states
                {clause}
                group by status, object_type
                """,
                tuple(params),
            ).fetchall()
        by_status: dict[str, int] = {}
        by_object_type: dict[str, int] = {}
        last_indexed = None
        total = 0
        for row in rows:
            count = int(row["count"])
            total += count
            by_status[row["status"]] = by_status.get(row["status"], 0) + count
            by_object_type[row["object_type"]] = by_object_type.get(row["object_type"], 0) + count
            if row["last_indexed_at"] and (last_indexed is None or row["last_indexed_at"] > last_indexed):
                last_indexed = row["last_indexed_at"]
        return {
            "index_version": "hipporag_offline.v1",
            "total": total,
            "dirty": by_status.get("dirty", 0),
            "indexed": by_status.get("indexed", 0),
            "tombstoned": by_status.get("tombstoned", 0),
            "by_status": by_status,
            "by_object_type": by_object_type,
            "last_indexed_at": last_indexed.isoformat() if last_indexed else None,
        }

    def list_entities(self, *, tenant_id: str | None = None) -> list[Entity]:
        where = "where tenant_id = %s" if tenant_id else ""
        params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from entities {where} order by created_at, entity_id",
                params,
            ).fetchall()
        return [
            Entity(
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                label=row["label"],
                owner_user_id=row["owner_user_id"],
                space_id=row["space_id"],
                visibility=Visibility(row["visibility"]),
                visible_team_ids=list(row["visible_team_ids"] or []),
                metadata=dict(row["metadata"] or {}),
                tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
            )
            for row in rows
        ]

    def list_chunks_for_sources(self, source_item_ids: set[str]) -> list[Chunk]:
        if not source_item_ids:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from chunks
                where source_item_id = any(%s)
                order by source_item_id, ordinal
                """,
                (list(source_item_ids),),
            ).fetchall()
        return [self._chunk_from_row(row) for row in rows]

    def list_documents_for_sources(self, source_item_ids: set[str]) -> list[Document]:
        if not source_item_ids:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from documents
                where source_item_id = any(%s)
                order by source_item_id, created_at, document_id
                """,
                (list(source_item_ids),),
            ).fetchall()
        return [self._document_from_row(row) for row in rows]

    def list_chunks_missing_embedding(self, *, provider: str, model: str, limit: int | None = None) -> list[Chunk]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from chunks
                where embedding is null
                   or embedding_provider is distinct from %s
                   or embedding_model is distinct from %s
                order by created_at, chunk_id
                limit %s
                """,
                (provider, model, limit if limit is not None else 2147483647),
            ).fetchall()
        return [self._chunk_from_row(row) for row in rows]

    def update_chunk_embedding(self, chunk_id: str, embedding: list[float], *, provider: str, model: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update chunks
                set embedding = %s::vector,
                    embedding_provider = %s,
                    embedding_model = %s,
                    embedding_created_at = now()
                where chunk_id = %s
                """,
                (_vector_literal(embedding), provider, model, chunk_id),
            )

    def vector_search_chunks(self, source_item_ids: set[str], query_embedding: list[float], *, top_k: int) -> list[tuple[Chunk, float]]:
        if not source_item_ids:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *, (embedding <=> %s::vector) as distance
                from chunks
                where source_item_id = any(%s)
                  and embedding is not null
                order by embedding <=> %s::vector
                limit %s
                """,
                (_vector_literal(query_embedding), list(source_item_ids), _vector_literal(query_embedding), top_k),
            ).fetchall()
        return [(self._chunk_from_row(row), max(0.0, 1.0 - float(row["distance"]))) for row in rows]

    def add_workspace_activity_event(self, event: WorkspaceActivityEvent) -> WorkspaceActivityEvent:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into workspace_activity_events(
                    workspace_activity_event_id, owner_user_id, actor_user_id, activity_type,
                    target_type, target_id, surface, title, summary, metadata, created_at,
                    tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    event.workspace_activity_event_id,
                    event.owner_user_id,
                    event.actor_user_id,
                    event.activity_type,
                    event.target_type,
                    event.target_id,
                    event.surface,
                    event.title,
                    event.summary,
                    Jsonb(to_jsonable(event.metadata)),
                    event.created_at,
                    event.tenant_id,
                ),
            ).fetchone()
        return self._workspace_activity_event_from_row(row)

    def list_workspace_activity_events(
        self,
        *,
        owner_user_id: str,
        tenant_id: str | None = None,
        activity_types: set[str] | None = None,
        limit: int = 50,
    ) -> list[WorkspaceActivityEvent]:
        clauses = ["owner_user_id = %s"]
        params: list[Any] = [owner_user_id]
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if activity_types:
            clauses.append("activity_type = any(%s)")
            params.append(sorted(activity_types))
        params.append(max(0, limit))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from workspace_activity_events
                where {" and ".join(clauses)}
                order by created_at desc, workspace_activity_event_id desc
                limit %s
                """,  # noqa: S608 - clauses are fixed.
                params,
            ).fetchall()
        return [self._workspace_activity_event_from_row(row) for row in rows]

    def upsert_discovery_item(self, item: DiscoveryItem) -> DiscoveryItem:
        with self.connect() as conn:
            existing_id = conn.execute(
                "select discovery_id from discovery_items where tenant_id = %s and discovery_id = %s",
                (item.tenant_id, item.discovery_id),
            ).fetchone()
            if existing_id is None and item.fingerprint:
                existing_id = conn.execute(
                    """
                    select discovery_id
                    from discovery_items
                    where tenant_id = %s
                      and owner_user_id = %s
                      and producer = %s
                      and fingerprint = %s
                    order by created_at desc, discovery_id desc
                    limit 1
                    """,
                    (item.tenant_id, item.owner_user_id, item.producer, item.fingerprint),
                ).fetchone()
            discovery_id = existing_id["discovery_id"] if existing_id else item.discovery_id
            row = conn.execute(
                """
                insert into discovery_items(
                    discovery_id, owner_user_id, discovery_type, title, evidence,
                    confidence, producer, fingerprint, evidence_snapshot,
                    discovery_score, quality_signals, status, created_at,
                    tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (discovery_id) do update
                set discovery_type = excluded.discovery_type,
                    title = excluded.title,
                    evidence = excluded.evidence,
                    confidence = excluded.confidence,
                    producer = excluded.producer,
                    fingerprint = excluded.fingerprint,
                    evidence_snapshot = excluded.evidence_snapshot,
                    discovery_score = excluded.discovery_score,
                    quality_signals = excluded.quality_signals,
                    tenant_id = excluded.tenant_id,
                    status = discovery_items.status,
                    created_at = discovery_items.created_at
                returning *
                """,
                (
                    discovery_id,
                    item.owner_user_id,
                    item.discovery_type,
                    item.title,
                    Jsonb(to_jsonable(item.evidence)),
                    item.confidence,
                    item.producer,
                    item.fingerprint,
                    Jsonb(to_jsonable(item.evidence_snapshot)),
                    item.discovery_score,
                    Jsonb(to_jsonable(item.quality_signals)),
                    item.status,
                    item.created_at,
                    item.tenant_id,
                ),
            ).fetchone()
        return self._discovery_item_from_row(row)

    def list_discovery_items(
        self,
        *,
        owner_user_id: str,
        tenant_id: str | None = None,
        status: str | None = None,
        since=None,
        limit: int = 50,
    ) -> list[DiscoveryItem]:
        clauses = ["owner_user_id = %s"]
        params: list[Any] = [owner_user_id]
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if status:
            clauses.append("status = %s")
            params.append(status)
        if since is not None:
            clauses.append("created_at >= %s")
            params.append(since)
        params.append(max(0, limit))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from discovery_items
                where {" and ".join(clauses)}
                order by discovery_score desc, created_at desc, discovery_id desc
                limit %s
                """,  # noqa: S608 - clauses are fixed.
                params,
            ).fetchall()
        return [self._discovery_item_from_row(row) for row in rows]

    def update_discovery_item_status(self, discovery_id: str, status: str) -> DiscoveryItem:
        with self.connect() as conn:
            row = conn.execute(
                """
                update discovery_items
                set status = %s
                where discovery_id = %s
                returning *
                """,
                (status, discovery_id),
            ).fetchone()
        if row is None:
            raise KeyError(discovery_id)
        return self._discovery_item_from_row(row)

    def list_hyperedges_for_entities(self, entity_ids: set[str]) -> list[tuple[Hyperedge, list[HyperedgeMember]]]:
        if not entity_ids:
            return []
        with self.connect() as conn:
            edge_rows = conn.execute(
                """
                select distinct h.*
                from hyperedges h
                join hyperedge_members m on m.hyperedge_id = h.hyperedge_id
                where m.entity_id = any(%s)
                order by h.created_at, h.hyperedge_id
                """,
                (list(entity_ids),),
            ).fetchall()
            edge_ids = [row["hyperedge_id"] for row in edge_rows]
            member_rows = conn.execute(
                """
                select * from hyperedge_members
                where hyperedge_id = any(%s)
                order by hyperedge_id, ordinal
                """,
                (edge_ids,),
            ).fetchall() if edge_ids else []
        members_by_edge: dict[str, list[HyperedgeMember]] = {}
        for row in member_rows:
            members_by_edge.setdefault(row["hyperedge_id"], []).append(
                HyperedgeMember(
                    hyperedge_id=row["hyperedge_id"],
                    entity_id=row["entity_id"],
                    role=row["role"],
                    ordinal=row["ordinal"],
                )
            )
        return [(self._hyperedge_from_row(row), members_by_edge.get(row["hyperedge_id"], [])) for row in edge_rows]

    def create_job(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        max_attempts: int = 3,
        priority: int = 0,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> Job:
        tenant_id = str(tenant_id or payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = str(owner_user_id or payload.get("owner_user_id") or "user_primary")
        payload = {**dict(payload), "tenant_id": tenant_id, "owner_user_id": owner_user_id}
        source_refs = payload.get("source_refs") if isinstance(payload.get("source_refs"), list) else []
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into jobs(job_type, payload, max_attempts, source_refs, priority, run_after, tenant_id, owner_user_id)
                values (%s, %s, %s, %s, %s, now(), %s, %s)
                returning *
                """,
                (job_type, Jsonb(to_jsonable(payload)), max_attempts, Jsonb(to_jsonable(source_refs)), priority, tenant_id, owner_user_id),
            ).fetchone()
            job = self._job_from_row(row)
        self.add_job_event(job.job_id, "queued", f"Queued {job_type} job", {"payload": payload, "priority": priority})
        return job

    def get_job(self, job_id: str) -> Job:
        with self.connect() as conn:
            row = conn.execute("select * from jobs where job_id = %s", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return self._job_from_row(row)

    def list_jobs(self, *, tenant_id: str | None = None, status: str | None = None, job_type: str | None = None, limit: int = 50) -> list[Job]:
        conditions: list[str] = []
        params: list[Any] = []
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)
        if status:
            conditions.append("status = %s")
            params.append(status)
        if job_type:
            conditions.append("job_type = %s")
            params.append(job_type)
        where = f"where {' and '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from jobs {where} order by created_at desc, job_id desc limit %s",
                tuple(params),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def list_job_events(self, job_id: str) -> list[JobEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from job_events where job_id = %s order by created_at, job_event_id",
                (job_id,),
            ).fetchall()
        return [self._job_event_from_row(row) for row in rows]

    def add_job_event(
        self,
        job_id: str,
        event_type: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> JobEvent:
        job = self.get_job(job_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into job_events(job_id, event_type, message, detail, tenant_id)
                values (%s, %s, %s, %s, %s)
                returning *
                """,
                (job_id, event_type, message, Jsonb(to_jsonable(detail or {})), job.tenant_id),
            ).fetchone()
        return self._job_event_from_row(row)

    def claim_next_job(
        self,
        *,
        worker_id: str | None = None,
        tenant_id: str | None = None,
        lease_seconds: int | None = None,
        excluded_job_types: set[str] | None = None,
    ) -> Job | None:
        excluded = sorted(excluded_job_types or set())
        tenant_filter = "and tenant_id = %s" if tenant_id else ""
        tenant_params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        with self.connect() as conn:
            row = conn.execute(
                f"""
                update jobs
                set status = 'running',
                    attempts = attempts + 1,
                    started_at = now(),
                    finished_at = null,
                    error = null,
                    worker_id = %s,
                    heartbeat_at = now(),
                    leased_until = case when %s::integer is null then null else now() + (%s::integer * interval '1 second') end,
                    updated_at = now()
                where job_id = (
                    select job_id
                    from jobs
                    where status = 'queued'
                      and run_after <= now()
                      {tenant_filter}
                      and (cardinality(%s::text[]) = 0 or not (job_type = any(%s::text[])))
                    order by priority desc, run_after, created_at, job_id
                    for update skip locked
                    limit 1
                )
                returning *
                """,
                (worker_id, lease_seconds, lease_seconds, *tenant_params, excluded, excluded),
            ).fetchone()
        if not row:
            return None
        job = self._job_from_row(row)
        self.add_job_event(
            job.job_id,
            "started",
            f"Started attempt {job.attempts}",
            {"worker_id": worker_id, "leased_until": job.leased_until.isoformat() if job.leased_until else None},
        )
        return job

    def lease_job(self, job_id: str, *, worker_id: str | None = None, lease_seconds: int | None = None) -> Job:
        with self.connect() as conn:
            row = conn.execute(
                """
                update jobs
                set status = 'running',
                    attempts = case when status = 'queued' then attempts + 1 else attempts end,
                    started_at = case when status = 'queued' then now() else started_at end,
                    finished_at = null,
                    error = null,
                    worker_id = coalesce(%s, worker_id),
                    heartbeat_at = now(),
                    leased_until = case when %s::integer is null then null else now() + (%s::integer * interval '1 second') end,
                    updated_at = now()
                where job_id = %s
                  and (
                    (status = 'queued' and run_after <= now())
                    or (
                      status = 'running'
                      and (
                        worker_id is null
                        or worker_id = %s
                        or leased_until is null
                        or leased_until < now()
                      )
                    )
                  )
                returning *
                """,
                (worker_id, lease_seconds, lease_seconds, job_id, worker_id),
            ).fetchone()
        if not row:
            current = self.get_job(job_id)
            raise ValueError(f"Job {job_id} cannot be leased from status {current.status}")
        job = self._job_from_row(row)
        self.add_job_event(
            job.job_id,
            "leased",
            "Job leased",
            {"worker_id": job.worker_id, "leased_until": job.leased_until.isoformat() if job.leased_until else None},
        )
        return job

    def heartbeat_job(
        self,
        job_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        external_run_id: str | None = None,
    ) -> Job:
        with self.connect() as conn:
            row = conn.execute(
                """
                update jobs
                set worker_id = coalesce(%s, worker_id),
                    heartbeat_at = now(),
                    leased_until = case when %s::integer is null then leased_until else now() + (%s::integer * interval '1 second') end,
                    external_run_id = coalesce(%s, external_run_id),
                    updated_at = now()
                where job_id = %s
                  and status = 'running'
                returning *
                """,
                (worker_id, lease_seconds, lease_seconds, external_run_id, job_id),
            ).fetchone()
        if not row:
            current = self.get_job(job_id)
            raise ValueError(f"Only running jobs can heartbeat, got {current.status}")
        job = self._job_from_row(row)
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

    def finish_job(self, job_id: str, result: dict[str, Any]) -> Job:
        with self.connect() as conn:
            row = conn.execute(
                """
                update jobs
                set status = 'succeeded',
                    result = %s,
                    error = null,
                    finished_at = now(),
                    leased_until = null,
                    updated_at = now()
                where job_id = %s
                returning *
                """,
                (Jsonb(to_jsonable(result)), job_id),
            ).fetchone()
        job = self._job_from_row(row)
        self.add_job_event(job.job_id, "succeeded", "Job succeeded", {"result": result})
        return job

    def fail_job(self, job_id: str, error: str, *, retryable: bool = True) -> Job:
        current = self.get_job(job_id)
        status = "queued" if retryable and current.attempts < current.max_attempts else "failed"
        delay_seconds = _retry_delay_seconds(current.payload, current.attempts) if status == "queued" else 0
        run_after = utc_now() + timedelta(seconds=delay_seconds) if status == "queued" else None
        with self.connect() as conn:
            row = conn.execute(
                """
                update jobs
                set status = %s,
                    error = %s,
                    run_after = coalesce(%s, run_after),
                    finished_at = case when %s = 'failed' then now() else finished_at end,
                    worker_id = null,
                    leased_until = null,
                    heartbeat_at = null,
                    updated_at = now()
                where job_id = %s
                returning *
                """,
                (status, error, run_after, status, job_id),
            ).fetchone()
        job = self._job_from_row(row)
        event_type = "retry_queued" if status == "queued" else "failed"
        self.add_job_event(
            job.job_id,
            event_type,
            error,
            {"run_after": job.run_after.isoformat() if job.run_after else None, "backoff_seconds": delay_seconds},
        )
        return job

    def retry_job(self, job_id: str) -> Job:
        current = self.get_job(job_id)
        if current.status not in {"failed", "canceled"}:
            raise ValueError(f"Only failed or canceled jobs can be retried, got {current.status}")
        with self.connect() as conn:
            row = conn.execute(
                """
                update jobs
                set status = 'queued',
                    error = null,
                    run_after = now(),
                    finished_at = null,
                    worker_id = null,
                    leased_until = null,
                    heartbeat_at = null,
                    external_run_id = null,
                    updated_at = now()
                where job_id = %s
                returning *
                """,
                (job_id,),
            ).fetchone()
        job = self._job_from_row(row)
        self.add_job_event(job.job_id, "retry_queued", "Job manually queued for retry")
        return job

    def cancel_job(self, job_id: str, *, reason: str = "") -> Job:
        current = self.get_job(job_id)
        if current.status in {"succeeded", "failed", "canceled"}:
            raise ValueError(f"Only queued or running jobs can be canceled, got {current.status}")
        error = reason or "Job canceled"
        with self.connect() as conn:
            row = conn.execute(
                """
                update jobs
                set status = 'canceled',
                    error = %s,
                    finished_at = now(),
                    worker_id = null,
                    leased_until = null,
                    heartbeat_at = null,
                    external_run_id = null,
                    updated_at = now()
                where job_id = %s
                returning *
                """,
                (error, job_id),
            ).fetchone()
        job = self._job_from_row(row)
        self.add_job_event(job.job_id, "canceled", error)
        return job

    def recover_stale_jobs(self, *, tenant_id: str | None = None, max_age_seconds: int) -> list[Job]:
        tenant_filter = "and tenant_id = %s" if tenant_id else ""
        params: tuple[Any, ...] = (max_age_seconds, tenant_id) if tenant_id else (max_age_seconds,)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                update jobs
                set status = case when attempts < max_attempts then 'queued' else 'failed' end,
                    error = case
                        when attempts < max_attempts then 'Recovered stale running job'
                        else 'Stale running job exceeded max attempts'
                    end,
                    run_after = case when attempts < max_attempts then now() else run_after end,
                    finished_at = case when attempts < max_attempts then null else now() end,
                    worker_id = null,
                    leased_until = null,
                    heartbeat_at = null,
                    updated_at = now()
                where status = 'running'
                  and started_at is not null
                  and started_at < now() - (%s * interval '1 second')
                  {tenant_filter}
                returning *
                """,  # noqa: S608 - tenant_filter is a fixed fragment.
                params,
            ).fetchall()
        jobs = [self._job_from_row(row) for row in rows]
        for job in jobs:
            if job.status == "queued":
                self.add_job_event(job.job_id, "stale_requeued", job.error or "Recovered stale running job")
            else:
                self.add_job_event(job.job_id, "stale_failed", job.error or "Stale running job exceeded max attempts")
        return jobs

    def count_table(self, table: str) -> int:
        if table not in {
            "source_items",
            "documents",
            "chunks",
            "passage_windows",
            "graph_nodes",
            "graph_edges",
            "users",
            "spaces",
            "entities",
            "hyperedges",
            "knowledge_claims",
            "digest_notes",
            "knowledge_claim_links",
            "digest_note_links",
            "review_items",
            "agent_memories",
            "user_profile_cards",
            "jobs",
            "job_events",
            "knowledge_sources",
            "sync_runs",
            "connector_states",
            "offline_index_states",
            "workspace_activity_events",
            "discovery_items",
        }:
            raise ValueError(f"Unsupported table: {table}")
        with self.connect() as conn:
            row = conn.execute(f"select count(*) as count from {table}").fetchone()
        return int(row["count"])

    def _source_item_from_row(self, row: dict[str, Any]) -> SourceItem:
        return SourceItem(
            source_item_id=row["source_item_id"],
            source_channel=row["source_channel"],
            record_type=row["record_type"],
            source_id=row["source_id"],
            owner_user_id=row["owner_user_id"],
            space_id=row["space_id"],
            visibility=Visibility(row["visibility"]),
            visible_team_ids=list(row["visible_team_ids"] or []),
            title=row["title"],
            url=row["url"],
            content_text=row["content_text"],
            content_hash=row["content_hash"],
            metadata=dict(row["metadata"] or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _offline_index_state_from_row(self, row: dict[str, Any]) -> OfflineIndexState:
        return OfflineIndexState(
            object_type=row["object_type"],
            object_id=row["object_id"],
            owner_user_id=row["owner_user_id"],
            source_item_id=row.get("source_item_id"),
            content_hash=row.get("content_hash"),
            mtime=row.get("mtime"),
            visibility_version=row.get("visibility_version"),
            embedding_provider=row.get("embedding_provider"),
            embedding_model=row.get("embedding_model"),
            index_version=row["index_version"],
            status=row["status"],
            dirty_reason=row.get("dirty_reason"),
            last_indexed_at=row.get("last_indexed_at"),
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _workspace_activity_event_from_row(self, row: dict[str, Any]) -> WorkspaceActivityEvent:
        return WorkspaceActivityEvent(
            workspace_activity_event_id=row["workspace_activity_event_id"],
            owner_user_id=row["owner_user_id"],
            actor_user_id=row["actor_user_id"],
            activity_type=row["activity_type"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            surface=row["surface"],
            title=row["title"],
            summary=row["summary"],
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _discovery_item_from_row(self, row: dict[str, Any]) -> DiscoveryItem:
        return DiscoveryItem(
            discovery_id=row["discovery_id"],
            owner_user_id=row["owner_user_id"],
            discovery_type=row["discovery_type"],
            title=row["title"],
            evidence=list(row.get("evidence") or []),
            confidence=float(row["confidence"]),
            producer=row["producer"],
            fingerprint=str(row.get("fingerprint") or ""),
            evidence_snapshot=list(row.get("evidence_snapshot") or []),
            discovery_score=float(row.get("discovery_score") or 0.0),
            quality_signals=dict(row.get("quality_signals") or {}),
            status=row["status"],
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _connector_state_from_row(self, row: dict[str, Any]) -> ConnectorState:
        return ConnectorState(
            connector_state_id=row["connector_state_id"],
            connector_id=row["connector_id"],
            owner_user_id=row["owner_user_id"],
            enabled=bool(row["enabled"]),
            scan_cursor=row.get("scan_cursor"),
            sync_status=row["sync_status"],
            last_success_at=row.get("last_success_at"),
            last_error_at=row.get("last_error_at"),
            last_error=row.get("last_error"),
            permission_scope=dict(row.get("permission_scope") or {}),
            config=dict(row.get("config") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _knowledge_source_from_row(self, row: dict[str, Any]) -> KnowledgeSource:
        return KnowledgeSource(
            knowledge_source_id=row["knowledge_source_id"],
            owner_user_id=row["owner_user_id"],
            name=row["name"],
            source_type=row["source_type"],
            uri=row["uri"],
            mode=row["mode"],
            status=row["status"],
            connector_id=row["connector_id"],
            space_id=row["space_id"],
            visibility=Visibility(row["visibility"]),
            visible_team_ids=list(row.get("visible_team_ids") or []),
            permission_scope=dict(row.get("permission_scope") or {}),
            config=dict(row.get("config") or {}),
            last_sync_at=row.get("last_sync_at"),
            last_error=row.get("last_error"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _sync_run_from_row(self, row: dict[str, Any]) -> SyncRun:
        return SyncRun(
            sync_run_id=row["sync_run_id"],
            knowledge_source_id=row["knowledge_source_id"],
            owner_user_id=row["owner_user_id"],
            connector_id=row["connector_id"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row.get("finished_at"),
            scanned=int(row.get("scanned") or 0),
            ingested=int(row.get("ingested") or 0),
            new_files=int(row.get("new_files") or 0),
            changed_files=int(row.get("changed_files") or 0),
            unchanged_files=int(row.get("unchanged_files") or 0),
            moved_files=int(row.get("moved_files") or 0),
            missing_files=int(row.get("missing_files") or 0),
            skipped=int(row.get("skipped") or 0),
            failed=int(row.get("failed") or 0),
            error=row.get("error"),
            report=dict(row.get("report") or {}),
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _chunk_from_row(self, row: dict[str, Any]) -> Chunk:
        return Chunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            source_item_id=row["source_item_id"],
            owner_user_id=row["owner_user_id"],
            space_id=row["space_id"],
            visibility=Visibility(row["visibility"]),
            visible_team_ids=list(row["visible_team_ids"] or []),
            text=row["text"],
            ordinal=row["ordinal"],
            embedding=None,
            metadata={
                "embedding_provider": row.get("embedding_provider"),
                "embedding_model": row.get("embedding_model"),
                "embedding_created_at": row.get("embedding_created_at"),
            },
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _document_from_row(self, row: dict[str, Any]) -> Document:
        return Document(
            document_id=row["document_id"],
            source_item_id=row["source_item_id"],
            owner_user_id=row["owner_user_id"],
            space_id=row["space_id"],
            visibility=Visibility(row["visibility"]),
            visible_team_ids=list(row["visible_team_ids"] or []),
            title=row["title"],
            body=row["body"],
            metadata=dict(row.get("metadata") or {}),
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _hyperedge_from_row(self, row: dict[str, Any]) -> Hyperedge:
        return Hyperedge(
            hyperedge_id=row["hyperedge_id"],
            relation_type=row["relation_type"],
            owner_user_id=row["owner_user_id"],
            space_id=row["space_id"],
            visibility=Visibility(row["visibility"]),
            directionality=Directionality(row["directionality"]),
            visible_team_ids=list(row["visible_team_ids"] or []),
            evidence_text=row["evidence_text"],
            source_refs=[SourceRef(**item) for item in (row.get("source_refs") or [])],
            confidence=row["confidence"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _knowledge_claim_from_row(self, row: dict[str, Any]) -> KnowledgeClaim:
        return KnowledgeClaim(
            knowledge_claim_id=row["knowledge_claim_id"],
            owner_user_id=row["owner_user_id"],
            claim_type=row["claim_type"],
            statement=row["statement"],
            subject=row.get("subject"),
            predicate=row.get("predicate"),
            object=row.get("object"),
            qualifiers=dict(row.get("qualifiers") or {}),
            evidence_text=row["evidence_text"],
            source_refs=[SourceRef(**item) for item in (row.get("source_refs") or [])],
            confidence=float(row["confidence"]),
            producer=row["producer"],
            job_id=row.get("job_id"),
            request_id=row.get("request_id"),
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _digest_note_from_row(self, row: dict[str, Any]) -> DigestNote:
        return DigestNote(
            digest_note_id=row["digest_note_id"],
            owner_user_id=row["owner_user_id"],
            title=row["title"],
            synopsis=row["synopsis"],
            key_points=list(row.get("key_points") or []),
            actions=list(row.get("actions") or []),
            open_questions=list(row.get("open_questions") or []),
            risks=list(row.get("risks") or []),
            memory_suggestions=list(row.get("memory_suggestions") or []),
            relationship_suggestions=list(row.get("relationship_suggestions") or []),
            source_refs=[SourceRef(**item) for item in (row.get("source_refs") or [])],
            confidence=float(row["confidence"]),
            producer=row["producer"],
            job_id=row.get("job_id"),
            request_id=row.get("request_id"),
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _agent_memory_from_row(self, row: dict[str, Any]) -> AgentMemory:
        return AgentMemory(
            agent_memory_id=row["agent_memory_id"],
            owner_user_id=row["owner_user_id"],
            layer=MemoryLayer(row["layer"]),
            text=row["text"],
            confidence=float(row["confidence"]),
            source_refs=[SourceRef(**item) for item in row["source_refs"]],
            decay_policy=row["decay_policy"],
            last_verified_at=row["last_verified_at"],
            created_by_user_id=row["created_by_user_id"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _profile_card_from_row(self, row: dict[str, Any]) -> UserProfileCard:
        return UserProfileCard(
            profile_card_id=row["profile_card_id"],
            owner_user_id=row["owner_user_id"],
            profile=dict(row["profile"] or {}),
            source_refs=[SourceRef(**item) for item in row["source_refs"]],
            confidence=float(row["confidence"]),
            last_verified_at=row.get("updated_at"),
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _job_from_row(self, row: dict[str, Any]) -> Job:
        return Job(
            job_id=row["job_id"],
            job_type=row["job_type"],
            payload=dict(row["payload"] or {}),
            status=row["status"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            priority=int(row.get("priority") or 0),
            run_after=row.get("run_after"),
            error=row["error"],
            result=dict(row["result"] or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            worker_id=row.get("worker_id"),
            leased_until=row.get("leased_until"),
            heartbeat_at=row.get("heartbeat_at"),
            external_run_id=row.get("external_run_id"),
            source_refs=[SourceRef(**item) for item in (row.get("source_refs") or [])],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
            owner_user_id=row.get("owner_user_id") or (dict(row["payload"] or {}).get("owner_user_id") if row.get("payload") else None) or "user_primary",
        )

    def _review_item_from_row(self, row: dict[str, Any]) -> ReviewItem:
        return ReviewItem(
            review_item_id=row["review_item_id"],
            owner_user_id=row["owner_user_id"],
            review_type=ReviewType(row["review_type"]),
            title=row["title"],
            proposal=dict(row["proposal"] or {}),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _job_event_from_row(self, row: dict[str, Any]) -> JobEvent:
        return JobEvent(
            job_event_id=row["job_event_id"],
            job_id=row["job_id"],
            event_type=row["event_type"],
            message=row["message"],
            detail=dict(row["detail"] or {}),
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )


def _vector_literal(vector: list[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"


def _visibility_version(owner_user_id: str, visibility: str, visible_team_ids: list[str]) -> str:
    return "|".join([owner_user_id, visibility, ",".join(sorted(visible_team_ids))])


def _retry_delay_seconds(payload: dict[str, Any], attempts: int) -> int:
    raw = payload.get("retry_backoff_seconds", payload.get("backoff_seconds", 60)) if isinstance(payload, dict) else 60
    try:
        base = max(0, int(raw))
    except (TypeError, ValueError):
        base = 60
    exponent = max(0, attempts - 1)
    return min(base * (2**exponent), 3600)
