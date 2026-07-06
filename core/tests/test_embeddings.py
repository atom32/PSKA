from __future__ import annotations

import json
import sys
import types

from pska_core.acl import ACLService
from pska_core.embeddings import APIEmbeddingProvider, EmbeddingConfig, EmbeddingService, build_embedding_provider
from pska_core.enums import UserRole, Visibility
from pska_core.ingest import IngestService
from pska_core.models import Chunk, Document, SourceItem, TeamMembership, User
from pska_core.offline_index import OfflineIndexService
from pska_core.retrieval import (
    EvidenceScoreContext,
    EvidenceScorePipeline,
    EvidenceScoreSignal,
    EvidenceScorer,
    RetrievalResult,
    RetrievalService,
)
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


class ModeEmbeddingProvider:
    provider_name = "mode-test"
    model_name = "mode-test-model"
    dimensions = 3

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            if "query_vector" in text or "semantic-target" in text:
                vectors.append([1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
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


def _add_chunked_source(store: InMemoryKnowledgeStore, source_id: str, title: str, chunks: list[str]) -> str:
    source_item_id = f"src_{source_id}"
    document_id = f"doc_{source_id}"
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="upload",
            record_type="file",
            source_id=source_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title=title,
            url=None,
            content_text="\n\n".join(chunks),
            content_hash=f"hash_{source_id}",
            metadata={"extra": {"extraction": {"extractor": "test"}}},
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title=title,
            body="\n\n".join(chunks),
        )
    )
    for ordinal, text in enumerate(chunks):
        store.add_chunk(
            Chunk(
                chunk_id=f"chk_{source_id}_{ordinal}",
                document_id=document_id,
                source_item_id=source_item_id,
                owner_user_id="user_primary",
                space_id="private_primary",
                visibility=Visibility.PRIVATE,
                visible_team_ids=[],
                text=text,
                ordinal=ordinal,
            )
        )
    return source_item_id


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


def test_api_embedding_provider_posts_to_remote_endpoint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                        {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):  # noqa: ANN001 - urllib request object.
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("pska_core.embeddings.urlopen", fake_urlopen)
    provider = APIEmbeddingProvider(
        api_key="sk-test",
        model_name="remote-embedding",
        base_url="https://embedding.test/v1/",
        dimensions=3,
        timeout_seconds=12,
    )

    vectors = provider.embed_texts(["alpha", "beta"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert captured["url"] == "https://embedding.test/v1/embeddings"
    assert captured["timeout"] == 12
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["payload"] == {"model": "remote-embedding", "input": ["alpha", "beta"], "dimensions": 3}


def test_api_embedding_provider_does_not_load_local_bge(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "embedding_key.txt"
    key_file.write_text("sk-embedding\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "FlagEmbedding", None)

    provider = build_embedding_provider(
        EmbeddingConfig(
            provider="api",
            model="remote-embedding",
            dimensions=3,
            api_key_file=key_file,
            base_url="https://embedding.test/v1",
        )
    )

    assert isinstance(provider, APIEmbeddingProvider)
    assert provider.provider_name == "api"
    assert provider.model_name == "remote-embedding"
    assert provider.dimensions == 3


def test_ingest_marks_source_and_chunks_dirty_for_offline_index() -> None:
    store = _store()

    item = IngestService(store, chunk_chars=10).ingest_channel_payload(
        _payload("offline-index-note", "alpha beta gamma delta")
    )

    states = store.list_offline_index_states(source_item_id=item.source_item_id)
    chunk_count = len(store.list_chunks_for_sources({item.source_item_id}))
    assert {state.object_type for state in states} == {"source_item", "chunk"}
    assert all(state.status == "dirty" for state in states)
    assert all(state.dirty_reason == "source_ingested" for state in states)
    assert store.offline_index_status()["dirty"] == chunk_count + 1


def test_offline_index_processes_only_dirty_chunks() -> None:
    store = _store()
    first = IngestService(store).ingest_channel_payload(_payload("dirty-one", "CodeGraph builds graph memory."))
    second = IngestService(store).ingest_channel_payload(_payload("dirty-two", "Browser captures pages."))
    first_chunk = store.list_chunks_for_sources({first.source_item_id})[0]
    second_chunk = store.list_chunks_for_sources({second.source_item_id})[0]
    provider = FakeEmbeddingProvider()

    report = OfflineIndexService(store, embedding_provider=provider).process_dirty_embeddings(limit=1)

    assert report["embedded"] == 1
    assert set(report["indexed_chunk_ids"]) <= {first_chunk.chunk_id, second_chunk.chunk_id}
    assert provider.calls[0] in [["CodeGraph builds graph memory."], ["Browser captures pages."]]
    assert len(store.list_offline_index_states(object_type="chunk", status="indexed")) == 1
    assert len(store.list_offline_index_states(object_type="chunk", status="dirty")) == 1


def test_visibility_change_invalidates_offline_index_state() -> None:
    store = _store()
    item = IngestService(store).ingest_channel_payload(_payload("visibility-dirty", "private note"))

    store.update_visibility(
        target_type="source_item",
        target_id=item.source_item_id,
        visibility=Visibility.TEAM.value,
        visible_team_ids=["team_default"],
    )

    state = store.list_offline_index_states(object_type="source_item", source_item_id=item.source_item_id)[0]
    assert state.status == "dirty"
    assert state.dirty_reason == "visibility_changed"
    assert state.visibility_version == "user_primary|team|team_default"


def test_tombstone_source_invalidates_offline_index_state() -> None:
    store = _store()
    item = IngestService(store).ingest_channel_payload(_payload("delete-dirty", "temporary note"))

    report = OfflineIndexService(store).tombstone_source(item.source_item_id, reason="source_deleted")

    assert report["tombstoned"] == 2
    assert store.offline_index_status()["tombstoned"] == 2


def test_retrieval_uses_vector_results_when_lexical_has_no_match() -> None:
    store = _store()
    IngestService(store, embedding_provider=FakeEmbeddingProvider()).ingest_channel_payload(
        _payload("graph-note", "CodeGraph builds a code knowledge graph.")
    )
    user = store.get_user("user_primary")
    retrieval_provider = FakeEmbeddingProvider()

    response = RetrievalService(
        store,
        ACLService(store),
        embedding_provider=retrieval_provider,
    ).search("图谱", user, top_k=1)

    assert response.results
    assert response.results[0].title == "graph-note"
    assert response.score_debug["ranker"] == "hybrid_rrf"
    assert response.score_debug["lexical_candidates"] == 0
    assert response.score_debug["vector_enabled"] is True
    assert response.score_debug["vector_candidates"] == 1
    assert response.score_debug["vector_error"] is None
    assert retrieval_provider.calls == [["图谱"]]
    assert response.results[0].source == "vector"
    assert response.results[0].score_debug["vector_rank"] == 1.0


def test_retrieval_mode_can_isolate_lexical_and_vector_candidates() -> None:
    store = _store()
    ingest = IngestService(store, embedding_provider=ModeEmbeddingProvider())
    ingest.ingest_channel_payload(_payload("semantic-note", "semantic-target carries the meaning only."))
    ingest.ingest_channel_payload(_payload("literal-note", "literal-token appears as a keyword only."))
    ingest.ingest_channel_payload(_payload("background-note", "background context without the keyword."))
    user = store.get_user("user_primary")
    retrieval = RetrievalService(store, ACLService(store), embedding_provider=ModeEmbeddingProvider())

    lexical = retrieval.search("query_vector literal-token", user, top_k=1, retrieval_mode="lexical")
    vector = retrieval.search("query_vector literal-token", user, top_k=2, retrieval_mode="vector")

    assert lexical.results[0].title == "literal-note"
    assert lexical.score_debug["retrieval_mode"] == "lexical"
    assert lexical.score_debug["vector_enabled"] is False
    assert vector.score_debug["retrieval_mode"] == "vector"
    assert vector.score_debug["lexical_ranker"] == "disabled"
    semantic = next(result for result in vector.results if result.title == "semantic-note")
    assert semantic.score_debug["vector_rank"] == 1.0


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


def test_retrieval_boosts_spreadsheet_sources_for_spreadsheet_queries() -> None:
    store = _store()
    ingest = IngestService(store)
    ingest.ingest_channel_payload(
        _payload(
            "decision-log",
            "Acme Example pipeline decisions mention Alice Example several times. Acme Example needs review.",
            title="decision-log-2025-q3.md",
        )
    )
    ingest.ingest_channel_payload(
        _payload(
            "portfolio-pipeline.xlsx",
            "Company Lead Status ARR Next Step Acme Example Alice Example active 1200000 Prepare partner meeting brief",
            title="portfolio-pipeline.xlsx",
            extra={"extraction": {"extractor": "xlsx-zip-xml", "sheet_count": 1}},
        )
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("What is in the Excel pipeline for Acme Example?", user, top_k=1)

    assert response.results[0].title == "portfolio-pipeline.xlsx"
    assert response.results[0].score_debug["spreadsheet_intent_match"] == 1.0


def test_retrieval_focuses_spreadsheet_snippet_on_matching_row_and_columns() -> None:
    store = _store()
    wide_row_padding = " | ".join(f"Filler{i}" for i in range(40))
    _add_chunked_source(
        store,
        "loan_book",
        "loan-book.xlsx",
        [
            "| RowNo | BorrowerId | DrawnCNYmm | LTV | InternalRating | ECLStage | Checksum | Notes |\n"
            "| 1 | BOR-OTHER | 10.00 | 0.1000 | BBB | Stage 1 | CHK-OTHER | old row |",
            "| RowNo | BorrowerId | DrawnCNYmm | LTV | InternalRating | ECLStage | Checksum | Notes |\n"
            f"| 1375 | BOR-NOISE | 11.00 | 0.1100 | BBB | Stage 1 | CHK-NOISE | {wide_row_padding} |\n"
            "| 1376 | BOR-TXC-HLD-1376 | 654.32 | 0.7261 | AA- | Stage 2 | CHK-TXC-1376-4812 | target row |",
        ],
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search(
        "RowNo 1376 Borrower BOR-TXC-HLD-1376 DrawnCNYmm LTV InternalRating ECLStage Checksum",
        user,
        top_k=1,
    )

    snippet = response.results[0].snippet
    assert "1376" in snippet
    assert "BOR-TXC-HLD-1376" in snippet
    assert "654.32" in snippet
    assert "0.7261" in snippet
    assert "AA-" in snippet
    assert "Stage 2" in snippet
    assert "CHK-TXC-1376-4812" in snippet
    assert "1375" not in snippet


def test_retrieval_carries_neighbor_table_header_for_mid_table_row_chunks() -> None:
    store = _store()
    _add_chunked_source(
        store,
        "txn_attachment",
        "transactions.xlsx",
        [
            "| TxnRow | TxnId | AmountCNYmm | RelatedFacilityId | LoanBookBorrowerId | RiskTag | RunningExposureCNYmm |\n"
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| 288 | TXN-SENTINEL-0288 | 47.89 | FAC-TXC-1376 | BOR-TXC-HLD-1376 | 疑似循环转账+同控资金归集 | 812.77 |",
            "| Check | Expected | Actual | Status |\n"
            "| Sentinel TxnId | TXN-SENTINEL-0288 | TXN-SENTINEL-0288 | OK |\n"
            "| Amount | 47.89 | 47.89 | OK |\n"
            "| RiskTag | 疑似循环转账+同控资金归集 | 疑似循环转账+同控资金归集 | OK |\n"
            "| RunningExposure | 812.77 | 812.77 | OK |",
        ],
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search(
        "TXN-SENTINEL-0288 Amount FacilityId BorrowerId RiskTag RunningExposure",
        user,
        top_k=1,
    )

    assert response.results[0].result_id == "chk_txn_attachment_1"
    assert "LoanBookBorrowerId" in response.results[0].snippet
    assert "BOR-TXC-HLD-1376" in response.results[0].snippet


def test_deterministic_evidence_scoring_scores_numeric_anchor_coverage() -> None:
    store = _store()
    _add_chunked_source(
        store,
        "numeric_report",
        "numeric-report-2025.pdf",
        [
            "2025 metric-alpha overview metric-alpha metric-alpha metric-alpha with many background mentions.",
            "单位：元\n第一季度 第二季度 第三季度 第四季度\nmetric-alpha 10.00 20.00 30.00 40.00",
        ],
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("2025 第四季度 metric-alpha 是多少", user, top_k=1)

    assert response.results[0].result_id == "chk_numeric_report_1"
    assert response.results[0].score_debug["evidence_scoring_anchor_coverage"] >= 0.75
    assert response.results[0].score_debug["evidence_scoring_positive_score"] > 0


def test_evidence_score_pipeline_accepts_registered_scorers() -> None:
    class ConstantScorer(EvidenceScorer):
        name = "custom_signal"

        def score(self, result, context, features):
            return EvidenceScoreSignal(self.name, 0.01)

    pipeline = EvidenceScorePipeline(scorers=[ConstantScorer()])
    result = RetrievalResult(
        result_id="chunk-1",
        source_item_id="source-1",
        source="manual",
        title="Generic source",
        snippet="Generic evidence",
        score=0.0,
        citation={},
    )

    outcome = pipeline.score(
        result,
        EvidenceScoreContext(query="generic question", anchors=(), asks_for_numeric_answer=False),
    )

    assert outcome.score == 0.01
    assert outcome.debug["evidence_scoring_custom_signal"] == 0.01


def test_retrieval_adds_tail_chunk_for_last_page_queries() -> None:
    store = _store()
    _add_chunked_source(
        store,
        "annual_report",
        "Annual Report.pdf",
        [
            "Annual Report overview Annual Report overview Annual Report overview.",
            "Annual Report financial discussion and operating review.",
            "Investor contacts: www.example.com, +852 2179 5122. Registered office final-page sentinel.",
        ],
    )
    user = store.get_user("user_primary")

    response = RetrievalService(store, ACLService(store)).search("Annual Report.pdf 最后一页", user, top_k=1)

    assert response.results[0].result_id == "chk_annual_report_2"
    assert "final-page sentinel" in response.results[0].snippet
    assert response.results[0].score_debug["document_position_intent"] == 1.0


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
