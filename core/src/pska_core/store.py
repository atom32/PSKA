from __future__ import annotations

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
    ReviewItem,
    SourceItem,
    TeamMembership,
    User,
    UserProfileCard,
    utc_now,
)


class KnowledgeStore(Protocol):
    def add_user(self, user: User) -> None: ...
    def get_user(self, user_id: str) -> User: ...
    def team_memberships_for_user(self, user_id: str) -> list[TeamMembership]: ...
    def upsert_source_item(self, item: SourceItem) -> SourceItem: ...
    def add_document(self, document: Document) -> None: ...
    def add_chunk(self, chunk: Chunk) -> None: ...
    def add_agent_memory(self, memory: AgentMemory) -> None: ...
    def add_profile_card(self, profile_card: UserProfileCard) -> None: ...
    def add_entity(self, entity: Entity) -> None: ...
    def add_hyperedge(self, hyperedge: Hyperedge, members: list[HyperedgeMember]) -> None: ...
    def add_review_item(self, review_item: ReviewItem) -> None: ...
    def list_review_items(self) -> list[ReviewItem]: ...
    def list_entities(self) -> list[Entity]: ...
    def list_source_items(self) -> list[SourceItem]: ...
    def list_chunks_for_sources(self, source_item_ids: set[str]) -> list[Chunk]: ...
    def list_chunks_missing_embedding(self, *, provider: str, model: str, limit: int | None = None) -> list[Chunk]: ...
    def update_chunk_embedding(self, chunk_id: str, embedding: list[float], *, provider: str, model: str) -> None: ...
    def vector_search_chunks(self, source_item_ids: set[str], query_embedding: list[float], *, top_k: int) -> list[tuple[Chunk, float]]: ...
    def list_hyperedges_for_entities(self, entity_ids: set[str]) -> list[tuple[Hyperedge, list[HyperedgeMember]]]: ...


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

    def add_profile_card(self, profile_card: UserProfileCard) -> None:
        self.profile_cards[profile_card.profile_card_id] = profile_card

    def add_entity(self, entity: Entity) -> None:
        self.entities[entity.entity_id] = entity

    def add_hyperedge(self, hyperedge: Hyperedge, members: list[HyperedgeMember]) -> None:
        self.hyperedges[hyperedge.hyperedge_id] = hyperedge
        self.hyperedge_members.extend(members)

    def add_review_item(self, review_item: ReviewItem) -> None:
        self.review_items[review_item.review_item_id] = review_item

    def list_review_items(self) -> list[ReviewItem]:
        return list(self.review_items.values())

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

    def create_job(self, job_type: str, payload: dict, *, max_attempts: int = 3) -> Job:
        job = Job(job_id=f"job_{uuid4().hex}", job_type=job_type, payload=dict(payload), max_attempts=max_attempts)
        self.jobs[job.job_id] = job
        self.add_job_event(job.job_id, "queued", f"Queued {job_type} job", {"payload": payload})
        return job

    def get_job(self, job_id: str) -> Job:
        return self.jobs[job_id]

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[Job]:
        jobs = list(self.jobs.values())
        if status:
            jobs = [job for job in jobs if job.status == status]
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

    def claim_next_job(self) -> Job | None:
        queued = sorted((job for job in self.jobs.values() if job.status == "queued"), key=lambda job: job.created_at)
        if not queued:
            return None
        job = queued[0]
        job.status = "running"
        job.attempts += 1
        job.error = None
        job.started_at = utc_now()
        job.finished_at = None
        job.updated_at = utc_now()
        self.add_job_event(job.job_id, "started", f"Started attempt {job.attempts}")
        return job

    def finish_job(self, job_id: str, result: dict) -> Job:
        job = self.jobs[job_id]
        job.status = "succeeded"
        job.result = dict(result)
        job.error = None
        job.finished_at = utc_now()
        job.updated_at = utc_now()
        self.add_job_event(job.job_id, "succeeded", "Job succeeded", {"result": result})
        return job

    def fail_job(self, job_id: str, error: str, *, retryable: bool = True) -> Job:
        job = self.jobs[job_id]
        job.status = "queued" if retryable and job.attempts < job.max_attempts else "failed"
        job.error = error
        if job.status == "failed":
            job.finished_at = utc_now()
        job.updated_at = utc_now()
        event_type = "retry_queued" if job.status == "queued" else "failed"
        self.add_job_event(job.job_id, event_type, error)
        return job

    def retry_job(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.status not in {"failed", "canceled"}:
            raise ValueError(f"Only failed or canceled jobs can be retried, got {job.status}")
        job.status = "queued"
        job.error = None
        job.finished_at = None
        job.updated_at = utc_now()
        self.add_job_event(job.job_id, "retry_queued", "Job manually queued for retry")
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
                event_type = "stale_requeued"
            else:
                job.status = "failed"
                job.error = "Stale running job exceeded max attempts"
                job.finished_at = now
                event_type = "stale_failed"
            job.updated_at = now
            recovered.append(job)
            self.add_job_event(job.job_id, event_type, job.error)
        return recovered


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
