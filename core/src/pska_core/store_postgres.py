from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import re
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, UserStatus, Visibility
from pska_core.models import (
    AgentMemory,
    ArtifactSupport,
    AskConversation,
    AskMessage,
    AskRun,
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
    KnowledgeTopic,
    OfflineIndexState,
    ProcessingSpan,
    PromptProfile,
    ReviewItem,
    SourceRef,
    SourceItem,
    SyncRun,
    TeamMembership,
    TopicMention,
    User,
    UserProfileCard,
    WorkspaceActivityEvent,
    WritingBoard,
    WritingEdge,
    WritingNode,
    utc_now,
)
from pska_core.serde import to_jsonable


def _count_sql(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int((row or {}).get("count") or 0)


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

    def ensure_identity(
        self,
        *,
        tenant_id: str,
        user_id: str,
        role: UserRole = UserRole.USER,
        space_id: str | None = None,
    ) -> None:
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        user_id = user_id or "user_primary"
        handle = _identity_handle(user_id)
        space_id = space_id or f"private_{_identity_handle(user_id, max_length=84)}"
        with self.connect() as conn:
            conn.execute(
                """
                insert into tenants(tenant_id, slug, name)
                values (%s, %s, %s)
                on conflict (tenant_id) do nothing
                """,
                (tenant_id, tenant_id, tenant_id),
            )
            user_row = conn.execute(
                """
                insert into users(user_id, handle, role, status, tenant_id)
                values (%s, %s, %s, 'active', %s)
                on conflict (user_id) do update
                set status = 'active',
                    updated_at = now()
                where users.tenant_id = excluded.tenant_id
                returning tenant_id
                """,
                (user_id, handle, role.value, tenant_id),
            ).fetchone()
            if not user_row:
                existing_user = conn.execute("select tenant_id from users where user_id = %s", (user_id,)).fetchone()
                existing_tenant = existing_user.get("tenant_id") if existing_user else None
                raise ValueError(
                    f"user_id {user_id!r} already belongs to tenant {existing_tenant!r}; "
                    "use a tenant-scoped user key"
                )
            existing_space = conn.execute("select tenant_id from spaces where space_id = %s", (space_id,)).fetchone()
            if existing_space and str(existing_space.get("tenant_id") or DEFAULT_TENANT_ID) != tenant_id:
                raise ValueError(
                    f"space_id {space_id!r} already belongs to tenant {existing_space.get('tenant_id')!r}; "
                    "use a tenant-scoped space id"
                )
            conn.execute(
                """
                insert into spaces(space_id, slug, kind, owner_user_id, tenant_id)
                values (%s, %s, 'private', %s, %s)
                on conflict (space_id) do update
                set owner_user_id = excluded.owner_user_id,
                    tenant_id = excluded.tenant_id,
                    updated_at = now()
                """,
                (space_id, space_id, user_id, tenant_id),
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
                "select * from source_items where tenant_id = %s and owner_user_id = %s and content_hash = %s",
                (item.tenant_id, item.owner_user_id, item.content_hash),
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
                        lifecycle_status = %s,
                        deleted_at = %s,
                        deleted_by = %s,
                        delete_reason = %s,
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
                        item.lifecycle_status,
                        item.deleted_at,
                        item.deleted_by,
                        item.delete_reason,
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
                    content_hash, metadata, lifecycle_status, deleted_at, deleted_by, delete_reason, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    item.lifecycle_status,
                    item.deleted_at,
                    item.deleted_by,
                    item.delete_reason,
                    item.tenant_id,
                ),
            )
            return item

    def update_source_lifecycle(
        self,
        source_item_ids: list[str],
        *,
        lifecycle_status: str,
        actor_user_id: str,
        reason: str,
        tenant_id: str | None = None,
        hard_delete: bool = False,
    ) -> dict[str, int]:
        ids = list(dict.fromkeys(source_item_ids))
        if not ids:
            return {"source_items": 0, "documents": 0, "chunks": 0, "offline_index_states": 0}
        tenant = tenant_id or DEFAULT_TENANT_ID
        with self.connect() as conn:
            counts = {
                "source_items": _count_sql(conn, "select count(*) from source_items where tenant_id = %s and source_item_id = any(%s)", (tenant, ids)),
                "documents": _count_sql(conn, "select count(*) from documents where tenant_id = %s and source_item_id = any(%s)", (tenant, ids)),
                "chunks": _count_sql(conn, "select count(*) from chunks where tenant_id = %s and source_item_id = any(%s)", (tenant, ids)),
                "offline_index_states": _count_sql(
                    conn,
                    """
                    select count(*)
                    from offline_index_states
                    where tenant_id = %s
                      and (source_item_id = any(%s) or object_id = any(%s))
                    """,
                    (tenant, ids, ids),
                ),
            }
            if hard_delete:
                conn.execute(
                    """
                    delete from offline_index_states
                    where tenant_id = %s
                      and (source_item_id = any(%s) or object_id = any(%s))
                    """,
                    (tenant, ids, ids),
                )
                conn.execute("delete from chunks where tenant_id = %s and source_item_id = any(%s)", (tenant, ids))
                conn.execute("delete from documents where tenant_id = %s and source_item_id = any(%s)", (tenant, ids))
                conn.execute("delete from source_items where tenant_id = %s and source_item_id = any(%s)", (tenant, ids))
                return counts

            deleted_at = None if lifecycle_status == "active" else utc_now()
            deleted_by = None if lifecycle_status == "active" else actor_user_id
            delete_reason = None if lifecycle_status == "active" else reason
            for table in ("source_items", "documents", "chunks"):
                conn.execute(
                    f"""
                    update {table}
                    set lifecycle_status = %s,
                        deleted_at = %s,
                        deleted_by = %s,
                        delete_reason = %s,
                        updated_at = now()
                    where tenant_id = %s and source_item_id = any(%s)
                    """,  # noqa: S608 - fixed table allowlist.
                    (lifecycle_status, deleted_at, deleted_by, delete_reason, tenant, ids),
                )
            if lifecycle_status != "active":
                conn.execute(
                    """
                    update offline_index_states
                    set status = 'tombstoned',
                        dirty_reason = %s,
                        updated_at = now()
                    where tenant_id = %s
                      and (source_item_id = any(%s) or object_id = any(%s))
                    """,
                    (reason, tenant, ids, ids),
                )
        return counts

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

    def add_processing_span(self, span: ProcessingSpan) -> ProcessingSpan:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into processing_spans(
                    processing_span_id, knowledge_source_id, owner_user_id, stage, status,
                    started_at, finished_at, sync_run_id, source_item_id, duration_ms,
                    input_payload, output_payload, metadata, error, tenant_id
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (processing_span_id) do update
                set status = excluded.status,
                    finished_at = excluded.finished_at,
                    duration_ms = excluded.duration_ms,
                    input_payload = excluded.input_payload,
                    output_payload = excluded.output_payload,
                    metadata = excluded.metadata,
                    error = excluded.error
                returning *
                """,
                (
                    span.processing_span_id,
                    span.knowledge_source_id,
                    span.owner_user_id,
                    span.stage,
                    span.status,
                    span.started_at,
                    span.finished_at,
                    span.sync_run_id,
                    span.source_item_id,
                    span.duration_ms,
                    Jsonb(to_jsonable(span.input)),
                    Jsonb(to_jsonable(span.output)),
                    Jsonb(to_jsonable(span.metadata)),
                    span.error,
                    span.tenant_id,
                ),
            ).fetchone()
        return self._processing_span_from_row(row)

    def list_processing_spans(
        self,
        *,
        tenant_id: str | None = None,
        knowledge_source_id: str | None = None,
        sync_run_id: str | None = None,
        source_item_id: str | None = None,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[ProcessingSpan]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if knowledge_source_id:
            clauses.append("knowledge_source_id = %s")
            params.append(knowledge_source_id)
        if sync_run_id:
            clauses.append("sync_run_id = %s")
            params.append(sync_run_id)
        if source_item_id:
            clauses.append("source_item_id = %s")
            params.append(source_item_id)
        if stage:
            clauses.append("stage = %s")
            params.append(stage)
        where = " where " + " and ".join(clauses) if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"select * from processing_spans{where} order by started_at desc limit %s",  # noqa: S608 - fixed clauses only.
                params,
            ).fetchall()
        return [self._processing_span_from_row(row) for row in rows]

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

    def create_ask_conversation(self, conversation: AskConversation) -> AskConversation:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into ask_conversations(
                    conversation_id, tenant_id, owner_user_id, title, status, summary, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (conversation_id) do update
                set title = excluded.title,
                    status = excluded.status,
                    summary = excluded.summary,
                    metadata = excluded.metadata,
                    updated_at = now()
                returning *
                """,
                (
                    conversation.conversation_id,
                    conversation.tenant_id,
                    conversation.owner_user_id,
                    conversation.title,
                    conversation.status,
                    conversation.summary,
                    Jsonb(to_jsonable(conversation.metadata)),
                ),
            ).fetchone()
        return self._ask_conversation_from_row(row)

    def list_ask_conversations(self, *, tenant_id: str, owner_user_id: str, limit: int = 50) -> list[AskConversation]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from ask_conversations
                where tenant_id = %s and owner_user_id = %s and status <> 'archived'
                order by updated_at desc, created_at desc, conversation_id
                limit %s
                """,
                (tenant_id, owner_user_id, max(1, min(limit, 200))),
            ).fetchall()
        return [self._ask_conversation_from_row(row) for row in rows]

    def get_ask_conversation(self, conversation_id: str, *, tenant_id: str, owner_user_id: str) -> AskConversation:
        with self.connect() as conn:
            row = conn.execute(
                """
                select *
                from ask_conversations
                where conversation_id = %s and tenant_id = %s and owner_user_id = %s
                """,
                (conversation_id, tenant_id, owner_user_id),
            ).fetchone()
        if not row:
            raise KeyError(conversation_id)
        return self._ask_conversation_from_row(row)

    def archive_ask_conversation(self, conversation_id: str, *, tenant_id: str, owner_user_id: str) -> AskConversation:
        with self.connect() as conn:
            row = conn.execute(
                """
                update ask_conversations
                set status = 'archived',
                    updated_at = now()
                where conversation_id = %s and tenant_id = %s and owner_user_id = %s
                returning *
                """,
                (conversation_id, tenant_id, owner_user_id),
            ).fetchone()
        if not row:
            raise KeyError(conversation_id)
        return self._ask_conversation_from_row(row)

    def add_ask_message(self, message: AskMessage) -> AskMessage:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into ask_messages(
                    message_id, conversation_id, tenant_id, owner_user_id, role, content,
                    run_id, citations, source_refs, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (message_id) do update
                set content = excluded.content,
                    run_id = excluded.run_id,
                    citations = excluded.citations,
                    source_refs = excluded.source_refs,
                    metadata = excluded.metadata
                returning *
                """,
                (
                    message.message_id,
                    message.conversation_id,
                    message.tenant_id,
                    message.owner_user_id,
                    message.role,
                    message.content,
                    message.run_id,
                    Jsonb(to_jsonable(message.citations)),
                    Jsonb(to_jsonable(message.source_refs)),
                    Jsonb(to_jsonable(message.metadata)),
                ),
            ).fetchone()
            conn.execute(
                "update ask_conversations set updated_at = now() where conversation_id = %s",
                (message.conversation_id,),
            )
        return self._ask_message_from_row(row)

    def list_ask_messages(self, conversation_id: str, *, tenant_id: str, owner_user_id: str, limit: int = 100) -> list[AskMessage]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from ask_messages
                where conversation_id = %s and tenant_id = %s and owner_user_id = %s
                order by created_at, message_id
                limit %s
                """,
                (conversation_id, tenant_id, owner_user_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._ask_message_from_row(row) for row in rows]

    def add_ask_run(self, run: AskRun) -> AskRun:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into ask_runs(
                    run_id, conversation_id, tenant_id, owner_user_id, query, status,
                    result, route, evidence_check, prompt_profile_id, prompt_profile_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id) do update
                set status = excluded.status,
                    result = excluded.result,
                    route = excluded.route,
                    evidence_check = excluded.evidence_check,
                    prompt_profile_id = excluded.prompt_profile_id,
                    prompt_profile_version = excluded.prompt_profile_version
                returning *
                """,
                (
                    run.run_id,
                    run.conversation_id,
                    run.tenant_id,
                    run.owner_user_id,
                    run.query,
                    run.status,
                    Jsonb(to_jsonable(run.result)),
                    Jsonb(to_jsonable(run.route)),
                    Jsonb(to_jsonable(run.evidence_check)),
                    run.prompt_profile_id,
                    run.prompt_profile_version,
                ),
            ).fetchone()
        return self._ask_run_from_row(row)

    def finish_ask_run(self, run_id: str, *, status: str, result: dict[str, Any]) -> AskRun:
        with self.connect() as conn:
            row = conn.execute(
                """
                update ask_runs
                set status = %s,
                    result = %s,
                    route = %s,
                    evidence_check = %s,
                    finished_at = now()
                where run_id = %s
                returning *
                """,
                (
                    status,
                    Jsonb(to_jsonable(result)),
                    Jsonb(to_jsonable(result.get("route") or {})),
                    Jsonb(to_jsonable(result.get("evidence_check") or result.get("quality_signals", {}).get("evidence_check") or {})),
                    run_id,
                ),
            ).fetchone()
            if not row:
                raise KeyError(run_id)
            conn.execute(
                "update ask_conversations set updated_at = now() where conversation_id = %s",
                (row["conversation_id"],),
            )
        return self._ask_run_from_row(row)

    def list_ask_runs(self, conversation_id: str, *, tenant_id: str, owner_user_id: str, limit: int = 50) -> list[AskRun]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from ask_runs
                where conversation_id = %s and tenant_id = %s and owner_user_id = %s
                order by started_at desc, run_id
                limit %s
                """,
                (conversation_id, tenant_id, owner_user_id, max(1, min(limit, 200))),
            ).fetchall()
        return [self._ask_run_from_row(row) for row in rows]

    def upsert_prompt_profile(self, profile: PromptProfile) -> PromptProfile:
        with self.connect() as conn:
            existing = conn.execute(
                """
                select *
                from prompt_profiles
                where tenant_id = %s and scope = %s and coalesce(owner_user_id, '') = coalesce(%s, '') and profile_type = %s
                """,
                (profile.tenant_id, profile.scope, profile.owner_user_id, profile.profile_type),
            ).fetchone()
            next_version = int(existing["current_version"] or 0) + 1 if existing else max(int(profile.current_version or 1), 1)
            row = conn.execute(
                """
                insert into prompt_profiles(
                    prompt_profile_id, tenant_id, owner_user_id, profile_type, scope,
                    name, status, current_version, config
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (prompt_profile_id) do update
                set name = excluded.name,
                    status = excluded.status,
                    current_version = excluded.current_version,
                    config = excluded.config,
                    updated_at = now()
                returning *
                """,
                (
                    existing["prompt_profile_id"] if existing else profile.prompt_profile_id,
                    profile.tenant_id,
                    profile.owner_user_id,
                    profile.profile_type,
                    profile.scope,
                    profile.name,
                    profile.status,
                    next_version,
                    Jsonb(to_jsonable(profile.config)),
                ),
            ).fetchone()
            version_hash = hashlib.sha256(f"{row['prompt_profile_id']}:{next_version}".encode("utf-8")).hexdigest()[:32]
            conn.execute(
                """
                insert into prompt_profile_versions(
                    prompt_profile_version_id, prompt_profile_id, tenant_id, profile_type,
                    scope, owner_user_id, version, config
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (prompt_profile_id, version) do nothing
                """,
                (
                    f"ppv_{version_hash}",
                    row["prompt_profile_id"],
                    profile.tenant_id,
                    profile.profile_type,
                    profile.scope,
                    profile.owner_user_id,
                    next_version,
                    Jsonb(to_jsonable(profile.config)),
                ),
            )
        return self._prompt_profile_from_row(row)

    def list_prompt_profiles(self, *, tenant_id: str, owner_user_id: str | None = None, profile_type: str | None = None) -> list[PromptProfile]:
        clauses = ["tenant_id = %s", "status = 'active'"]
        params: list[Any] = [tenant_id]
        if owner_user_id is not None:
            clauses.append("(owner_user_id is null or owner_user_id = %s)")
            params.append(owner_user_id)
        if profile_type:
            clauses.append("profile_type = %s")
            params.append(profile_type)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from prompt_profiles
                where {' and '.join(clauses)}
                order by profile_type,
                         case scope when 'tenant' then 0 when 'user' then 1 else 9 end,
                         owner_user_id nulls first,
                         updated_at
                """,  # noqa: S608 - fixed clauses only.
                tuple(params),
            ).fetchall()
        return [self._prompt_profile_from_row(row) for row in rows]

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

    def create_writing_board(self, board: WritingBoard) -> WritingBoard:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into writing_boards(board_id, tenant_id, owner_user_id, title, goal, metadata, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    board.board_id,
                    board.tenant_id,
                    board.owner_user_id,
                    board.title,
                    board.goal,
                    Jsonb(to_jsonable(board.metadata)),
                    board.created_at,
                    board.updated_at,
                ),
            ).fetchone()
        return self._writing_board_from_row(row)

    def update_writing_board(
        self,
        board_id: str,
        *,
        tenant_id: str,
        owner_user_id: str,
        title: str | None = None,
        goal: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WritingBoard:
        current = self.get_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                update writing_boards
                set title = %s,
                    goal = %s,
                    metadata = %s,
                    updated_at = now()
                where tenant_id = %s and owner_user_id = %s and board_id = %s
                returning *
                """,
                (
                    current.title if title is None else title,
                    current.goal if goal is None else goal,
                    Jsonb(to_jsonable(current.metadata if metadata is None else metadata)),
                    tenant_id,
                    owner_user_id,
                    board_id,
                ),
            ).fetchone()
        if not row:
            raise KeyError(board_id)
        return self._writing_board_from_row(row)

    def get_writing_board(self, board_id: str, *, tenant_id: str, owner_user_id: str) -> WritingBoard:
        with self.connect() as conn:
            row = conn.execute(
                """
                select *
                from writing_boards
                where tenant_id = %s and owner_user_id = %s and board_id = %s
                """,
                (tenant_id, owner_user_id, board_id),
            ).fetchone()
        if not row:
            raise KeyError(board_id)
        return self._writing_board_from_row(row)

    def list_writing_boards(self, *, tenant_id: str, owner_user_id: str, limit: int = 50) -> list[WritingBoard]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from writing_boards
                where tenant_id = %s and owner_user_id = %s
                order by updated_at desc, board_id desc
                limit %s
                """,
                (tenant_id, owner_user_id, max(0, limit)),
            ).fetchall()
        return [self._writing_board_from_row(row) for row in rows]

    def delete_writing_board(self, board_id: str, *, tenant_id: str, owner_user_id: str) -> None:
        with self.connect() as conn:
            result = conn.execute(
                """
                delete from writing_boards
                where tenant_id = %s and owner_user_id = %s and board_id = %s
                """,
                (tenant_id, owner_user_id, board_id),
            )
        if result.rowcount == 0:
            raise KeyError(board_id)

    def upsert_writing_node(self, node: WritingNode) -> WritingNode:
        self.get_writing_board(node.board_id, tenant_id=node.tenant_id, owner_user_id=node.owner_user_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into writing_nodes(
                    node_id, board_id, tenant_id, owner_user_id, node_type, title, body_markdown,
                    position, size, status, source_refs, citations, quality_signals, metadata,
                    created_at, updated_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (node_id) do update
                set title = excluded.title,
                    body_markdown = excluded.body_markdown,
                    position = excluded.position,
                    size = excluded.size,
                    status = excluded.status,
                    source_refs = excluded.source_refs,
                    citations = excluded.citations,
                    quality_signals = excluded.quality_signals,
                    metadata = excluded.metadata,
                    updated_at = now()
                where writing_nodes.tenant_id = excluded.tenant_id
                  and writing_nodes.owner_user_id = excluded.owner_user_id
                returning *
                """,
                (
                    node.node_id,
                    node.board_id,
                    node.tenant_id,
                    node.owner_user_id,
                    node.node_type,
                    node.title,
                    node.body_markdown,
                    Jsonb(to_jsonable(node.position)),
                    Jsonb(to_jsonable(node.size)),
                    node.status,
                    Jsonb(to_jsonable(node.source_refs)),
                    Jsonb(to_jsonable(node.citations)),
                    Jsonb(to_jsonable(node.quality_signals)),
                    Jsonb(to_jsonable(node.metadata)),
                    node.created_at,
                    node.updated_at,
                ),
            ).fetchone()
        if not row:
            raise KeyError(node.node_id)
        return self._writing_node_from_row(row)

    def update_writing_node(
        self,
        node_id: str,
        *,
        tenant_id: str,
        owner_user_id: str,
        title: str | None = None,
        body_markdown: str | None = None,
        position: dict[str, Any] | None = None,
        size: dict[str, Any] | None = None,
        status: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        quality_signals: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WritingNode:
        current = self._get_writing_node(node_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                update writing_nodes
                set title = %s,
                    body_markdown = %s,
                    position = %s,
                    size = %s,
                    status = %s,
                    source_refs = %s,
                    citations = %s,
                    quality_signals = %s,
                    metadata = %s,
                    updated_at = now()
                where tenant_id = %s and owner_user_id = %s and node_id = %s
                returning *
                """,
                (
                    current.title if title is None else title,
                    current.body_markdown if body_markdown is None else body_markdown,
                    Jsonb(to_jsonable(current.position if position is None else position)),
                    Jsonb(to_jsonable(current.size if size is None else size)),
                    current.status if status is None else status,
                    Jsonb(to_jsonable(current.source_refs if source_refs is None else source_refs)),
                    Jsonb(to_jsonable(current.citations if citations is None else citations)),
                    Jsonb(to_jsonable(current.quality_signals if quality_signals is None else quality_signals)),
                    Jsonb(to_jsonable(current.metadata if metadata is None else metadata)),
                    tenant_id,
                    owner_user_id,
                    node_id,
                ),
            ).fetchone()
        if not row:
            raise KeyError(node_id)
        return self._writing_node_from_row(row)

    def delete_writing_node(self, node_id: str, *, tenant_id: str, owner_user_id: str) -> None:
        with self.connect() as conn:
            result = conn.execute(
                "delete from writing_nodes where tenant_id = %s and owner_user_id = %s and node_id = %s",
                (tenant_id, owner_user_id, node_id),
            )
        if result.rowcount == 0:
            raise KeyError(node_id)

    def list_writing_nodes(self, board_id: str, *, tenant_id: str, owner_user_id: str) -> list[WritingNode]:
        self.get_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from writing_nodes
                where tenant_id = %s and owner_user_id = %s and board_id = %s
                order by created_at, node_id
                """,
                (tenant_id, owner_user_id, board_id),
            ).fetchall()
        return [self._writing_node_from_row(row) for row in rows]

    def upsert_writing_edge(self, edge: WritingEdge) -> WritingEdge:
        self.get_writing_board(edge.board_id, tenant_id=edge.tenant_id, owner_user_id=edge.owner_user_id)
        self._get_writing_node(edge.source_node_id, tenant_id=edge.tenant_id, owner_user_id=edge.owner_user_id)
        self._get_writing_node(edge.target_node_id, tenant_id=edge.tenant_id, owner_user_id=edge.owner_user_id)
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into writing_edges(
                    edge_id, board_id, tenant_id, owner_user_id, source_node_id, target_node_id,
                    edge_type, label, metadata, created_at, updated_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (edge_id) do update
                set source_node_id = excluded.source_node_id,
                    target_node_id = excluded.target_node_id,
                    edge_type = excluded.edge_type,
                    label = excluded.label,
                    metadata = excluded.metadata,
                    updated_at = now()
                where writing_edges.tenant_id = excluded.tenant_id
                  and writing_edges.owner_user_id = excluded.owner_user_id
                returning *
                """,
                (
                    edge.edge_id,
                    edge.board_id,
                    edge.tenant_id,
                    edge.owner_user_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.edge_type,
                    edge.label,
                    Jsonb(to_jsonable(edge.metadata)),
                    edge.created_at,
                    edge.updated_at,
                ),
            ).fetchone()
        if not row:
            raise KeyError(edge.edge_id)
        return self._writing_edge_from_row(row)

    def delete_writing_edge(self, edge_id: str, *, tenant_id: str, owner_user_id: str) -> None:
        with self.connect() as conn:
            result = conn.execute(
                "delete from writing_edges where tenant_id = %s and owner_user_id = %s and edge_id = %s",
                (tenant_id, owner_user_id, edge_id),
            )
        if result.rowcount == 0:
            raise KeyError(edge_id)

    def list_writing_edges(self, board_id: str, *, tenant_id: str, owner_user_id: str) -> list[WritingEdge]:
        self.get_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                select *
                from writing_edges
                where tenant_id = %s and owner_user_id = %s and board_id = %s
                order by created_at, edge_id
                """,
                (tenant_id, owner_user_id, board_id),
            ).fetchall()
        return [self._writing_edge_from_row(row) for row in rows]

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

    def upsert_knowledge_topic(self, topic: KnowledgeTopic) -> KnowledgeTopic:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into knowledge_topics(
                    topic_id, tenant_id, owner_user_id, label, normalized_label,
                    topic_type, description, confidence, producer, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (topic_id) do update
                set label = excluded.label,
                    normalized_label = excluded.normalized_label,
                    topic_type = excluded.topic_type,
                    description = excluded.description,
                    confidence = greatest(knowledge_topics.confidence, excluded.confidence),
                    producer = excluded.producer,
                    metadata = knowledge_topics.metadata || excluded.metadata,
                    updated_at = now()
                returning *
                """,
                (
                    topic.topic_id,
                    topic.tenant_id,
                    topic.owner_user_id,
                    topic.label,
                    topic.normalized_label,
                    topic.topic_type,
                    topic.description,
                    topic.confidence,
                    topic.producer,
                    Jsonb(to_jsonable(topic.metadata)),
                ),
            ).fetchone()
        return self._knowledge_topic_from_row(row)

    def list_knowledge_topics(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        query: str | None = None,
        limit: int = 100,
    ) -> list[KnowledgeTopic]:
        clauses = ["tenant_id = %s", "owner_user_id = %s"]
        params: list[Any] = [tenant_id, owner_user_id]
        if query:
            clauses.append("(label ilike %s or normalized_label ilike %s or description ilike %s)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern, pattern])
        params.append(max(1, min(limit, 1000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from knowledge_topics
                where {' and '.join(clauses)}
                order by confidence desc, updated_at desc, topic_id
                limit %s
                """,  # noqa: S608 - fixed clauses only.
                tuple(params),
            ).fetchall()
        return [self._knowledge_topic_from_row(row) for row in rows]

    def upsert_topic_mention(self, mention: TopicMention) -> TopicMention:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into topic_mentions(
                    topic_mention_id, tenant_id, owner_user_id, topic_id, source_item_id,
                    document_id, chunk_id, artifact_type, artifact_id, mention_text,
                    confidence, producer, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (topic_mention_id) do update
                set mention_text = excluded.mention_text,
                    confidence = greatest(topic_mentions.confidence, excluded.confidence),
                    producer = excluded.producer,
                    metadata = topic_mentions.metadata || excluded.metadata
                returning *
                """,
                (
                    mention.topic_mention_id,
                    mention.tenant_id,
                    mention.owner_user_id,
                    mention.topic_id,
                    mention.source_item_id,
                    mention.document_id,
                    mention.chunk_id,
                    mention.artifact_type,
                    mention.artifact_id,
                    mention.mention_text,
                    mention.confidence,
                    mention.producer,
                    Jsonb(to_jsonable(mention.metadata)),
                ),
            ).fetchone()
        return self._topic_mention_from_row(row)

    def list_topic_mentions(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        topic_ids: set[str] | None = None,
        source_item_ids: set[str] | None = None,
        limit: int = 500,
    ) -> list[TopicMention]:
        clauses = ["tenant_id = %s", "owner_user_id = %s"]
        params: list[Any] = [tenant_id, owner_user_id]
        if topic_ids:
            clauses.append("topic_id = any(%s)")
            params.append(list(topic_ids))
        if source_item_ids:
            clauses.append("source_item_id = any(%s)")
            params.append(list(source_item_ids))
        params.append(max(1, min(limit, 5000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from topic_mentions
                where {' and '.join(clauses)}
                order by created_at desc, topic_mention_id
                limit %s
                """,  # noqa: S608 - fixed clauses only.
                tuple(params),
            ).fetchall()
        return [self._topic_mention_from_row(row) for row in rows]

    def upsert_artifact_support(self, support: ArtifactSupport) -> ArtifactSupport:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into artifact_supports(
                    artifact_support_id, tenant_id, owner_user_id, artifact_type, artifact_id,
                    support_type, source_item_id, document_id, chunk_id, topic_id,
                    status, confidence, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (artifact_support_id) do update
                set status = excluded.status,
                    confidence = greatest(artifact_supports.confidence, excluded.confidence),
                    metadata = artifact_supports.metadata || excluded.metadata,
                    updated_at = now()
                returning *
                """,
                (
                    support.artifact_support_id,
                    support.tenant_id,
                    support.owner_user_id,
                    support.artifact_type,
                    support.artifact_id,
                    support.support_type,
                    support.source_item_id,
                    support.document_id,
                    support.chunk_id,
                    support.topic_id,
                    support.status,
                    support.confidence,
                    Jsonb(to_jsonable(support.metadata)),
                ),
            ).fetchone()
        return self._artifact_support_from_row(row)

    def list_artifact_supports(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        artifact_type: str | None = None,
        artifact_ids: set[str] | None = None,
        source_item_ids: set[str] | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> list[ArtifactSupport]:
        clauses = ["tenant_id = %s", "owner_user_id = %s"]
        params: list[Any] = [tenant_id, owner_user_id]
        if artifact_type:
            clauses.append("artifact_type = %s")
            params.append(artifact_type)
        if artifact_ids:
            clauses.append("artifact_id = any(%s)")
            params.append(list(artifact_ids))
        if source_item_ids:
            clauses.append("source_item_id = any(%s)")
            params.append(list(source_item_ids))
        if status:
            clauses.append("status = %s")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select *
                from artifact_supports
                where {' and '.join(clauses)}
                order by updated_at desc, artifact_support_id
                limit %s
                """,  # noqa: S608 - fixed clauses only.
                tuple(params),
            ).fetchall()
        return [self._artifact_support_from_row(row) for row in rows]

    def update_artifact_support_status_for_sources(
        self,
        source_item_ids: set[str],
        *,
        tenant_id: str,
        status: str,
    ) -> int:
        if not source_item_ids:
            return 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                update artifact_supports
                set status = %s,
                    updated_at = now()
                where tenant_id = %s and source_item_id = any(%s)
                returning artifact_support_id
                """,
                (status, tenant_id, list(source_item_ids)),
            ).fetchall()
        return len(rows)

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

    def count_table(self, table: str, *, tenant_id: str | None = None) -> int:
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
            "processing_spans",
            "connector_states",
            "offline_index_states",
            "workspace_activity_events",
            "writing_boards",
            "writing_nodes",
            "writing_edges",
            "discovery_items",
        }:
            raise ValueError(f"Unsupported table: {table}")
        with self.connect() as conn:
            if tenant_id:
                row = conn.execute(f"select count(*) as count from {table} where tenant_id = %s", (tenant_id,)).fetchone()
            else:
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
            lifecycle_status=str(row.get("lifecycle_status") or "active"),
            deleted_at=row.get("deleted_at"),
            deleted_by=row.get("deleted_by"),
            delete_reason=row.get("delete_reason"),
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

    def _writing_board_from_row(self, row: dict[str, Any]) -> WritingBoard:
        return WritingBoard(
            board_id=row["board_id"],
            owner_user_id=row["owner_user_id"],
            title=row["title"],
            goal=row["goal"],
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _writing_node_from_row(self, row: dict[str, Any]) -> WritingNode:
        return WritingNode(
            node_id=row["node_id"],
            board_id=row["board_id"],
            owner_user_id=row["owner_user_id"],
            node_type=row["node_type"],
            title=row["title"],
            body_markdown=row["body_markdown"],
            position=dict(row.get("position") or {}),
            size=dict(row.get("size") or {}),
            status=row["status"],
            source_refs=list(row.get("source_refs") or []),
            citations=list(row.get("citations") or []),
            quality_signals=dict(row.get("quality_signals") or {}),
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _writing_edge_from_row(self, row: dict[str, Any]) -> WritingEdge:
        return WritingEdge(
            edge_id=row["edge_id"],
            board_id=row["board_id"],
            owner_user_id=row["owner_user_id"],
            source_node_id=row["source_node_id"],
            target_node_id=row["target_node_id"],
            edge_type=row["edge_type"],
            label=row["label"],
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _get_writing_node(self, node_id: str, *, tenant_id: str, owner_user_id: str) -> WritingNode:
        with self.connect() as conn:
            row = conn.execute(
                """
                select *
                from writing_nodes
                where tenant_id = %s and owner_user_id = %s and node_id = %s
                """,
                (tenant_id, owner_user_id, node_id),
            ).fetchone()
        if not row:
            raise KeyError(node_id)
        return self._writing_node_from_row(row)

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

    def _processing_span_from_row(self, row: dict[str, Any]) -> ProcessingSpan:
        return ProcessingSpan(
            processing_span_id=row["processing_span_id"],
            knowledge_source_id=row["knowledge_source_id"],
            owner_user_id=row["owner_user_id"],
            stage=row["stage"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            sync_run_id=row["sync_run_id"],
            source_item_id=row["source_item_id"],
            duration_ms=row["duration_ms"],
            input=dict(row["input_payload"] or {}),
            output=dict(row["output_payload"] or {}),
            metadata=dict(row["metadata"] or {}),
            error=row["error"],
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
            lifecycle_status=str(row.get("lifecycle_status") or "active"),
            deleted_at=row.get("deleted_at"),
            deleted_by=row.get("deleted_by"),
            delete_reason=row.get("delete_reason"),
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
            lifecycle_status=str(row.get("lifecycle_status") or "active"),
            deleted_at=row.get("deleted_at"),
            deleted_by=row.get("deleted_by"),
            delete_reason=row.get("delete_reason"),
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _ask_conversation_from_row(self, row: dict[str, Any]) -> AskConversation:
        return AskConversation(
            conversation_id=row["conversation_id"],
            owner_user_id=row["owner_user_id"],
            title=row["title"],
            status=row["status"],
            summary=row.get("summary") or "",
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _ask_message_from_row(self, row: dict[str, Any]) -> AskMessage:
        return AskMessage(
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            owner_user_id=row["owner_user_id"],
            role=row["role"],
            content=row["content"],
            run_id=row.get("run_id"),
            citations=list(row.get("citations") or []),
            source_refs=list(row.get("source_refs") or []),
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _ask_run_from_row(self, row: dict[str, Any]) -> AskRun:
        return AskRun(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            owner_user_id=row["owner_user_id"],
            query=row["query"],
            status=row["status"],
            result=dict(row.get("result") or {}),
            route=dict(row.get("route") or {}),
            evidence_check=dict(row.get("evidence_check") or {}),
            prompt_profile_id=row.get("prompt_profile_id"),
            prompt_profile_version=row.get("prompt_profile_version"),
            started_at=row["started_at"],
            finished_at=row.get("finished_at"),
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _prompt_profile_from_row(self, row: dict[str, Any]) -> PromptProfile:
        return PromptProfile(
            prompt_profile_id=row["prompt_profile_id"],
            profile_type=row["profile_type"],
            scope=row["scope"],
            name=row["name"],
            config=dict(row.get("config") or {}),
            owner_user_id=row.get("owner_user_id"),
            status=row["status"],
            current_version=int(row.get("current_version") or 1),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _knowledge_topic_from_row(self, row: dict[str, Any]) -> KnowledgeTopic:
        return KnowledgeTopic(
            topic_id=row["topic_id"],
            owner_user_id=row["owner_user_id"],
            label=row["label"],
            normalized_label=row["normalized_label"],
            topic_type=row.get("topic_type") or "topic",
            description=row.get("description") or "",
            confidence=float(row.get("confidence") or 0.0),
            producer=row.get("producer") or "pska.topic_linker",
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _topic_mention_from_row(self, row: dict[str, Any]) -> TopicMention:
        return TopicMention(
            topic_mention_id=row["topic_mention_id"],
            topic_id=row["topic_id"],
            owner_user_id=row["owner_user_id"],
            source_item_id=row["source_item_id"],
            document_id=row.get("document_id"),
            chunk_id=row.get("chunk_id"),
            artifact_type=row.get("artifact_type") or "chunk",
            artifact_id=row.get("artifact_id") or "",
            mention_text=row.get("mention_text") or "",
            confidence=float(row.get("confidence") or 0.0),
            producer=row.get("producer") or "pska.topic_linker",
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            tenant_id=row.get("tenant_id") or DEFAULT_TENANT_ID,
        )

    def _artifact_support_from_row(self, row: dict[str, Any]) -> ArtifactSupport:
        return ArtifactSupport(
            artifact_support_id=row["artifact_support_id"],
            owner_user_id=row["owner_user_id"],
            artifact_type=row["artifact_type"],
            artifact_id=row["artifact_id"],
            support_type=row["support_type"],
            source_item_id=row["source_item_id"],
            document_id=row.get("document_id"),
            chunk_id=row.get("chunk_id"),
            topic_id=row.get("topic_id"),
            status=row.get("status") or "active",
            confidence=float(row.get("confidence") or 0.0),
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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


def _identity_handle(value: str, *, max_length: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._@-]+", "_", value).strip("._-") or "user"
    if len(cleaned) <= max_length:
        return cleaned
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[: max(1, max_length - 11)]}_{digest}"


def _retry_delay_seconds(payload: dict[str, Any], attempts: int) -> int:
    raw = payload.get("retry_backoff_seconds", payload.get("backoff_seconds", 60)) if isinstance(payload, dict) else 60
    try:
        base = max(0, int(raw))
    except (TypeError, ValueError):
        base = 60
    exponent = max(0, attempts - 1)
    return min(base * (2**exponent), 3600)
