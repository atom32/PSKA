from __future__ import annotations

import sys
import types

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


def _payload(
    source_id: str,
    text: str,
    *,
    title: str | None = None,
    url: str | None = None,
    created_at: str | None = None,
    extra: dict | None = None,
) -> dict:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": source_id,
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": title or source_id,
        "url": url,
        "content": {"text": text},
        "created_at": created_at,
        "extra": extra or {},
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


def test_retrieval_prefers_exact_url_and_title_matches() -> None:
    store = _store()
    ingest = IngestService(store)
    ingest.ingest_channel_payload(
        _payload(
            "noisy-note",
            "browser browser browser exact target phrase appears many times",
            title="Noisy note",
        )
    )
    ingest.ingest_channel_payload(
        _payload(
            "target-note",
            "short body",
            title="Exact Target",
            url="https://example.test/exact-target",
        )
    )
    user = store.get_user("user_primary")

    title_response = RetrievalService(store, ACLService(store)).search("Exact Target", user, top_k=1)
    url_response = RetrievalService(store, ACLService(store)).search("https://example.test/exact-target", user, top_k=1)

    assert title_response.results[0].title == "Exact Target"
    assert title_response.score_debug["ranker"] == "exact_source"
    assert title_response.score_debug["exact_candidates"] == 1
    assert title_response.results[0].score_debug["exact_source"] == 1.0
    assert url_response.results[0].title == "Exact Target"
    assert url_response.score_debug["ranker"] == "exact_source"


def test_retrieval_falls_back_to_term_frequency_when_bm25_is_unavailable(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "rank_bm25", None)
    store = _store()
    IngestService(store).ingest_channel_payload(_payload("fallback-note", "fallback ranking topic"))
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("ranking topic", user, top_k=1)

    assert response.score_debug["lexical_ranker"] == "term_frequency"
    assert response.results[0].title == "fallback-note"


def test_retrieval_uses_optional_rank_bm25_when_available(monkeypatch) -> None:
    class FakeBM25:
        def __init__(self, documents):
            self.documents = documents

        def get_scores(self, query_terms):
            query = set(query_terms)
            return [float(len(query.intersection(document))) for document in self.documents]

    module = types.ModuleType("rank_bm25")
    module.BM25Okapi = FakeBM25
    monkeypatch.setitem(sys.modules, "rank_bm25", module)
    store = _store()
    ingest = IngestService(store)
    ingest.ingest_channel_payload(_payload("weak-note", "ranking"))
    ingest.ingest_channel_payload(_payload("strong-note", "ranking topic"))
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("ranking topic", user, top_k=1)

    assert response.score_debug["lexical_ranker"] == "rank_bm25"
    assert response.results[0].title == "strong-note"
    assert response.results[0].score_debug["bm25"] == 2.0


def test_retrieval_uses_recency_as_tie_breaker_for_similar_matches() -> None:
    store = _store()
    ingest = IngestService(store)
    ingest.ingest_channel_payload(
        _payload(
            "old-note",
            "ranking topic",
            title="Old note",
            created_at="2026-01-01T00:00:00Z",
        )
    )
    ingest.ingest_channel_payload(
        _payload(
            "new-note",
            "ranking topic",
            title="New note",
            created_at="2026-06-01T00:00:00Z",
        )
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("ranking topic", user, top_k=2)

    assert [result.title for result in response.results] == ["New note", "Old note"]
    assert response.results[0].score_debug["recency"] == 1.0
    assert response.results[0].score_debug["quality_boost"] > response.results[1].score_debug["quality_boost"]


def test_retrieval_uses_source_authority_as_tie_breaker() -> None:
    store = _store()
    ingest = IngestService(store)
    ingest.ingest_channel_payload(
        _payload(
            "low-authority-note",
            "authority topic",
            title="Low authority",
            created_at="2026-06-01T00:00:00Z",
            extra={"source_authority": 0.1},
        )
    )
    ingest.ingest_channel_payload(
        _payload(
            "high-authority-note",
            "authority topic",
            title="High authority",
            created_at="2026-06-01T00:00:00Z",
            extra={"source_authority": 0.9},
        )
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("authority topic", user, top_k=2)

    assert [result.title for result in response.results] == ["High authority", "Low authority"]
    assert response.results[0].score_debug["source_authority"] == 0.9
    assert response.results[1].score_debug["source_authority"] == 0.1


def test_retrieval_reports_sensitive_query_terms() -> None:
    store = _store()
    IngestService(store).ingest_channel_payload(_payload("security-note", "API key rotation checklist."))
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("api key rotation", user, top_k=1)

    assert response.sensitivity == ["sensitive_query_terms:api key"]
    assert response.score_debug["diagnostics"]["sensitivity_count"] == 1


def test_retrieval_reports_sensitive_source_metadata() -> None:
    store = _store()
    IngestService(store).ingest_channel_payload(
        _payload(
            "sensitive-note",
            "private planning topic",
            title="Sensitive note",
            extra={"sensitivity": "high"},
        )
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("private planning topic", user, top_k=1)

    assert response.sensitivity == [f"sensitive_sources:{response.results[0].source_item_id}"]
    assert response.score_debug["diagnostics"]["sensitivity_count"] == 1
