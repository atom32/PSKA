from __future__ import annotations

from dataclasses import asdict

from pska_core.agentic import AgenticSearchService
from pska_core.ingest import IngestService
from pska_core.memory import MemoryService
from pska_core.models import ChannelIngestPayload, SourceRef, User
from pska_core.retrieval import RetrievalService


PSKA_TOOL_NAMES = [
    "pska_search",
    "pska_agentic_search",
    "pska_read_source",
    "pska_read_document",
    "pska_ingest_channel_payload",
    "pska_write_memory",
    "pska_update_profile_card_proposal",
    "pska_create_hyperedge",
    "pska_create_review_item",
    "pska_propose_team_visibility",
    "pska_apply_review_decision",
    "pska_index_status",
]


class PSKAToolFacade:
    """Fastreact-facing facade. Mutating methods are expected to run behind approval."""

    def __init__(
        self,
        *,
        ingest: IngestService,
        retrieval: RetrievalService,
        agentic_search: AgenticSearchService,
        memory: MemoryService,
    ) -> None:
        self.ingest = ingest
        self.retrieval = retrieval
        self.agentic_search = agentic_search
        self.memory = memory

    def pska_search(self, query: str, user: User, represented_user_id: str | None = None) -> dict:
        return asdict(self.retrieval.search(query, user, represented_user_id=represented_user_id))

    def pska_agentic_search(self, query: str, user: User, represented_user_id: str | None = None) -> dict:
        return self.agentic_search.search(query, user, represented_user_id=represented_user_id).to_dict()

    def pska_ingest_channel_payload(self, payload: dict) -> dict:
        item = self.ingest.ingest_channel_payload(ChannelIngestPayload.from_mapping(payload))
        return asdict(item)

    def pska_write_memory(self, payload: dict) -> dict:
        source_refs = [SourceRef(**ref) for ref in payload.get("source_refs", [])]
        memory = self.memory.write_agent_memory(
            owner_user_id=payload["owner_user_id"],
            layer=payload["layer"],
            text=payload["text"],
            confidence=float(payload.get("confidence", 0.0)),
            source_refs=source_refs,
            created_by_user_id=payload.get("created_by_user_id"),
            decay_policy=payload.get("decay_policy", "manual"),
        )
        return asdict(memory)

    def pska_update_profile_card_proposal(self, payload: dict) -> dict:
        source_refs = [SourceRef(**ref) for ref in payload.get("source_refs", [])]
        result = self.memory.propose_profile_update(
            owner_user_id=payload["owner_user_id"],
            profile_delta=payload["profile_delta"],
            source_refs=source_refs,
            sensitivity=payload.get("sensitivity", "normal"),
        )
        return asdict(result)
