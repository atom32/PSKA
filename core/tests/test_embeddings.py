from __future__ import annotations

from pska_core.acl import ACLService
from pska_core.embeddings import EmbeddingService
from pska_core.enums import UserRole
from pska_core.ingest import IngestService
from pska_core.models import TeamMembership, User
from pska_core.retrieval import RetrievalService
from pska_core.store import InMemoryKnowledgeStore


class FakeEmbeddingProvider:
    provider_name = "fake-bge"
    model_name = "fake-model"
    dimensions = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        vectors: list[list[float]] = []
        for text in texts:
            lower = text.lower()
            vectors.append(
                [
                    1.0 if "graph" in lower or "图谱" in text else 0.0,
                    1.0 if "browser" in lower or "浏览器" in text else 0.0,
                    1.0,
                ]
            )
        return vectors


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_team_membership(TeamMembership("user_primary", "team_default"))
    return store


def _payload(source_id: str, text: str) -> dict:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": source_id,
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": source_id,
        "content": {"text": text},
    }


def test_ingest_writes_embeddings_when_provider_is_enabled() -> None:
    store = _store()
    provider = FakeEmbeddingProvider()

    item = IngestService(store, embedding_provider=provider).ingest_channel_payload(
        _payload("graph-note", "CodeGraph builds a code knowledge graph.")
    )

    chunks = store.list_chunks_for_sources({item.source_item_id})
    assert len(chunks) == 1
    assert chunks[0].embedding == [1.0, 0.0, 1.0]
    assert chunks[0].metadata["embedding_provider"] == "fake-bge"
    assert chunks[0].metadata["embedding_model"] == "fake-model"
    assert provider.calls == [["CodeGraph builds a code knowledge graph."]]


def test_backfill_skips_chunks_that_already_match_provider_and_model() -> None:
    store = _store()
    IngestService(store).ingest_channel_payload(_payload("browser-note", "A headless browser for agents."))
    provider = FakeEmbeddingProvider()

    first = EmbeddingService(store, provider).backfill_missing()
    second = EmbeddingService(store, provider).backfill_missing()

    assert first.embedded == 1
    assert first.failed == 0
    assert second.embedded == 0
    assert second.failed == 0
    assert len(provider.calls) == 1


def test_retrieval_uses_vector_results_when_lexical_has_no_match() -> None:
    store = _store()
    IngestService(store, embedding_provider=FakeEmbeddingProvider()).ingest_channel_payload(
        _payload("graph-note", "CodeGraph builds a code knowledge graph.")
    )
    user = store.get_user("user_primary")

    response = RetrievalService(
        store,
        ACLService(store),
        embedding_provider=FakeEmbeddingProvider(),
    ).search("图谱", user, top_k=1)

    assert response.results
    assert response.results[0].title == "graph-note"
    assert response.score_debug["ranker"] == "hybrid_rrf"
    assert response.score_debug["lexical_candidates"] == 0
    assert response.score_debug["vector_enabled"] is True
    assert response.score_debug["vector_candidates"] == 1
    assert response.results[0].score_debug["vector_rank"] == 1.0
