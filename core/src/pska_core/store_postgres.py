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
    Document,
    Entity,
    Hyperedge,
    HyperedgeMember,
    Job,
    JobEvent,
    AuditEvent,
    ReviewItem,
    SourceRef,
    SourceItem,
    TeamMembership,
    User,
    UserProfileCard,
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
                insert into users(user_id, handle, role, status)
                values (%s, %s, %s, %s)
                on conflict (user_id) do update
                set handle = excluded.handle, role = excluded.role, status = excluded.status, updated_at = now()
                """,
                (user.user_id, user.handle, user.role.value, user.status.value),
            )

    def get_user(self, user_id: str) -> User:
        with self.connect() as conn:
            row = conn.execute("select * from users where user_id = %s", (user_id,)).fetchone()
        if not row:
            raise KeyError(user_id)
        return User(
            user_id=row["user_id"],
            handle=row["handle"],
            role=UserRole(row["role"]),
            status=UserStatus(row["status"]),
        )

    def team_memberships_for_user(self, user_id: str) -> list[TeamMembership]:
        with self.connect() as conn:
            rows = conn.execute("select * from team_memberships where user_id = %s", (user_id,)).fetchall()
        return [TeamMembership(user_id=row["user_id"], team_id=row["team_id"], role=row["role"]) for row in rows]

    def upsert_source_item(self, item: SourceItem) -> SourceItem:
        with self.connect() as conn:
            existing = conn.execute(
                "select * from source_items where content_hash = %s",
                (item.content_hash,),
            ).fetchone()
            if existing:
                return self._source_item_from_row(existing)
            conn.execute(
                """
                insert into source_items(
                    source_item_id, source_channel, record_type, source_id, owner_user_id,
                    space_id, visibility, visible_team_ids, title, url, content_text,
                    content_hash, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            return item

    def upsert_connector_state(self, state: ConnectorState) -> ConnectorState:
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into connector_states(
                    connector_state_id, connector_id, owner_user_id, enabled, scan_cursor,
                    sync_status, last_success_at, last_error_at, last_error, permission_scope, config
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (connector_state_id) do update
                set connector_id = excluded.connector_id,
                    owner_user_id = excluded.owner_user_id,
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
                ),
            ).fetchone()
        return self._connector_state_from_row(row)

    def get_connector_state(self, connector_state_id: str) -> ConnectorState:
        with self.connect() as conn:
            row = conn.execute("select * from connector_states where connector_state_id = %s", (connector_state_id,)).fetchone()
        if not row:
            raise KeyError(connector_state_id)
        return self._connector_state_from_row(row)

    def list_connector_states(self, *, owner_user_id: str | None = None, connector_id: str | None = None) -> list[ConnectorState]:
        clauses: list[str] = []
        params: list[str] = []
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
                    visibility, visible_team_ids, title, body, metadata
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )

    def add_chunk(self, chunk: Chunk) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into chunks(
                    chunk_id, document_id, source_item_id, owner_user_id, space_id,
                    visibility, visible_team_ids, ordinal, text, embedding,
                    embedding_provider, embedding_model
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
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
                ),
            )

    def add_agent_memory(self, memory: AgentMemory) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into agent_memories(
                    agent_memory_id, owner_user_id, created_by_user_id, layer, text,
                    confidence, source_refs, decay_policy, last_verified_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )

    def get_agent_memory(self, agent_memory_id: str) -> AgentMemory:
        with self.connect() as conn:
            row = conn.execute("select * from agent_memories where agent_memory_id = %s", (agent_memory_id,)).fetchone()
        if not row:
            raise KeyError(agent_memory_id)
        return self._agent_memory_from_row(row)

    def list_agent_memories(self, *, owner_user_id: str) -> list[AgentMemory]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from agent_memories
                where owner_user_id = %s
                order by confidence desc, updated_at desc, agent_memory_id
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._agent_memory_from_row(row) for row in rows]

    def update_agent_memory_lifecycle(
        self,
        agent_memory_id: str,
        *,
        confidence: float,
        decay_policy: str,
        last_verified_at,
    ) -> AgentMemory:
        with self.connect() as conn:
            row = conn.execute(
                """
                update agent_memories
                set confidence = %s, decay_policy = %s, last_verified_at = %s, updated_at = now()
                where agent_memory_id = %s
                returning *
                """,
                (confidence, decay_policy, last_verified_at, agent_memory_id),
            ).fetchone()
        if not row:
            raise KeyError(agent_memory_id)
        return self._agent_memory_from_row(row)

    def add_profile_card(self, profile_card: UserProfileCard) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into user_profile_cards(profile_card_id, owner_user_id, profile, confidence, source_refs)
                values (%s, %s, %s, %s, %s)
                on conflict (profile_card_id) do nothing
                """,
                (
                    profile_card.profile_card_id,
                    profile_card.owner_user_id,
                    Jsonb(to_jsonable(profile_card.profile)),
                    profile_card.confidence,
                    Jsonb(to_jsonable(profile_card.source_refs)),
                ),
            )

    def list_profile_cards(self, *, owner_user_id: str) -> list[UserProfileCard]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from user_profile_cards
                where owner_user_id = %s
                order by confidence desc, updated_at desc, profile_card_id
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._profile_card_from_row(row) for row in rows]

    def add_entity(self, entity: Entity) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into entities(entity_id, entity_type, label, owner_user_id, space_id, visibility, visible_team_ids, metadata)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )

    def add_hyperedge(self, hyperedge: Hyperedge, members: list[HyperedgeMember]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into hyperedges(
                    hyperedge_id, relation_type, owner_user_id, space_id, visibility,
                    visible_team_ids, directionality, evidence_text, source_refs, confidence
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    def add_review_item(self, review_item: ReviewItem) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into review_items(review_item_id, owner_user_id, review_type, title, proposal, status)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (review_item_id) do nothing
                """,
                (
                    review_item.review_item_id,
                    review_item.owner_user_id,
                    review_item.review_type.value if isinstance(review_item.review_type, ReviewType) else review_item.review_type,
                    review_item.title,
                    Jsonb(to_jsonable(review_item.proposal)),
                    review_item.status,
                ),
            )

    def get_review_item(self, review_item_id: str) -> ReviewItem:
        with self.connect() as conn:
            row = conn.execute("select * from review_items where review_item_id = %s", (review_item_id,)).fetchone()
        if not row:
            raise KeyError(review_item_id)
        return self._review_item_from_row(row)

    def list_review_items(self) -> list[ReviewItem]:
        with self.connect() as conn:
            rows = conn.execute("select * from review_items order by created_at, review_item_id").fetchall()
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

    def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self.connect() as conn:
            conn.execute(
                """
                insert into audit_events(audit_event_id, actor_user_id, action, target_type, target_id, decision, metadata)
                values (%s, %s, %s, %s, %s, %s, %s)
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
                returning {id_column}
                """,
                (visibility, visible_team_ids, target_id),
            ).fetchone()
        if not row:
            raise KeyError(target_id)

    def list_source_items(self) -> list[SourceItem]:
        with self.connect() as conn:
            rows = conn.execute("select * from source_items order by created_at, source_item_id").fetchall()
        return [self._source_item_from_row(row) for row in rows]

    def list_entities(self) -> list[Entity]:
        with self.connect() as conn:
            rows = conn.execute("select * from entities order by created_at, entity_id").fetchall()
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

    def create_job(self, job_type: str, payload: dict[str, Any], *, max_attempts: int = 3, priority: int = 0) -> Job:
        source_refs = payload.get("source_refs") if isinstance(payload.get("source_refs"), list) else []
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into jobs(job_type, payload, max_attempts, source_refs, priority, run_after)
                values (%s, %s, %s, %s, %s, now())
                returning *
                """,
                (job_type, Jsonb(to_jsonable(payload)), max_attempts, Jsonb(to_jsonable(source_refs)), priority),
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

    def list_jobs(self, *, status: str | None = None, job_type: str | None = None, limit: int = 50) -> list[Job]:
        conditions: list[str] = []
        params: list[Any] = []
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
        with self.connect() as conn:
            row = conn.execute(
                """
                insert into job_events(job_id, event_type, message, detail)
                values (%s, %s, %s, %s)
                returning *
                """,
                (job_id, event_type, message, Jsonb(to_jsonable(detail or {}))),
            ).fetchone()
        return self._job_event_from_row(row)

    def claim_next_job(self, *, worker_id: str | None = None, lease_seconds: int | None = None) -> Job | None:
        with self.connect() as conn:
            row = conn.execute(
                """
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
                    order by priority desc, run_after, created_at, job_id
                    for update skip locked
                    limit 1
                )
                returning *
                """,
                (worker_id, lease_seconds, lease_seconds),
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

    def recover_stale_jobs(self, *, max_age_seconds: int) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute(
                """
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
                returning *
                """,
                (max_age_seconds,),
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
            "users",
            "spaces",
            "entities",
            "hyperedges",
            "review_items",
            "agent_memories",
            "user_profile_cards",
            "jobs",
            "job_events",
            "connector_states",
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
        )

    def _profile_card_from_row(self, row: dict[str, Any]) -> UserProfileCard:
        return UserProfileCard(
            profile_card_id=row["profile_card_id"],
            owner_user_id=row["owner_user_id"],
            profile=dict(row["profile"] or {}),
            source_refs=[SourceRef(**item) for item in row["source_refs"]],
            confidence=float(row["confidence"]),
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
        )

    def _job_event_from_row(self, row: dict[str, Any]) -> JobEvent:
        return JobEvent(
            job_event_id=row["job_event_id"],
            job_id=row["job_id"],
            event_type=row["event_type"],
            message=row["message"],
            detail=dict(row["detail"] or {}),
            created_at=row["created_at"],
        )


def _vector_literal(vector: list[float] | None) -> str | None:
    if vector is None:
        return None
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"


def _retry_delay_seconds(payload: dict[str, Any], attempts: int) -> int:
    raw = payload.get("retry_backoff_seconds", payload.get("backoff_seconds", 60)) if isinstance(payload, dict) else 60
    try:
        base = max(0, int(raw))
    except (TypeError, ValueError):
        base = 60
    exponent = max(0, attempts - 1)
    return min(base * (2**exponent), 3600)
