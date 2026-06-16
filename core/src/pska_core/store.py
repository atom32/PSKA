from __future__ import annotations

from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from pska_core.models import (
    AgentMemory,
    Chunk,
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
from pska_core.enums import Visibility


class KnowledgeStore(Protocol):
    def add_user(self, user: User) -> None: ...
    def get_user(self, user_id: str) -> User: ...
    def team_memberships_for_user(self, user_id: str) -> list[TeamMembership]: ...
    def upsert_source_item(self, item: SourceItem) -> SourceItem: ...
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
    ) -> AgentMemory: ...
    def add_profile_card(self, profile_card: UserProfileCard) -> None: ...
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
    def list_chunks_for_sources(self, source_item_ids: set[str]) -> list[Chunk]: ...
    def list_chunks_missing_embedding(self, *, provider: str, model: str, limit: int | None = None) -> list[Chunk]: ...
    def update_chunk_embedding(self, chunk_id: str, embedding: list[float], *, provider: str, model: str) -> None: ...
    def vector_search_chunks(self, source_item_ids: set[str], query_embedding: list[float], *, top_k: int) -> list[tuple[Chunk, float]]: ...
    def list_hyperedges_for_entities(self, entity_ids: set[str]) -> list[tuple[Hyperedge, list[HyperedgeMember]]]: ...
    def count_table(self, table: str) -> int: ...
    def claim_next_job(self, *, worker_id: str | None = None, lease_seconds: int | None = None) -> Job | None: ...
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
    ) -> AgentMemory:
        memory = self.agent_memories[agent_memory_id]
        memory.confidence = confidence
        memory.decay_policy = decay_policy
        memory.last_verified_at = last_verified_at
        return memory

    def add_profile_card(self, profile_card: UserProfileCard) -> None:
        self.profile_cards[profile_card.profile_card_id] = profile_card

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

    def list_entities(self) -> list[Entity]:
        return list(self.entities.values())

    def list_source_items(self) -> list[SourceItem]:
        return list(self.source_items.values())

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

    def claim_next_job(self, *, worker_id: str | None = None, lease_seconds: int | None = None) -> Job | None:
        now = utc_now()
        queued = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status == "queued" and (job.run_after is None or job.run_after <= now)
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


def _retry_delay_seconds(payload: dict, attempts: int) -> int:
    raw = payload.get("retry_backoff_seconds", payload.get("backoff_seconds", 60)) if isinstance(payload, dict) else 60
    try:
        base = max(0, int(raw))
    except (TypeError, ValueError):
        base = 60
    exponent = max(0, attempts - 1)
    return min(base * (2**exponent), 3600)
