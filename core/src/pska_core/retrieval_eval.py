from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pska_core.acl import ACLService
from pska_core.agentic_service import build_agentic_service_client
from pska_core.embeddings import EmbeddingConfig, EmbeddingProvider, EmbeddingService, build_embedding_provider
from pska_core.enums import Directionality, MemoryLayer, UserRole, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.models import AgentMemory, Chunk, Document, Entity, SourceItem, SourceRef, User, UserProfileCard
from pska_core.retrieval import RetrievalResponse, RetrievalService
from pska_core.serde import to_jsonable
from pska_core.store import InMemoryKnowledgeStore


DEFAULT_RETRIEVAL_EVAL_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "retrieval_eval_cases.json"


class FixtureEmbeddingProvider:
    provider_name = "fixture-embeddings"
    model_name = "retrieval-eval-fixture"
    dimensions = 3

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        if text in self.vectors:
            return list(self.vectors[text])
        lower = text.lower()
        return [
            1.0 if "graph" in lower or "图谱" in text else 0.0,
            1.0 if "browser" in lower or "浏览器" in text else 0.0,
            1.0,
        ]


def run_retrieval_eval(
    fixture_path: str | Path = DEFAULT_RETRIEVAL_EVAL_FIXTURE,
    *,
    real: bool = False,
    embedding_config: EmbeddingConfig | None = None,
    require_llm: bool | None = None,
) -> dict[str, Any]:
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    store = build_eval_store(fixture)
    if real:
        provider = build_embedding_provider(embedding_config or EmbeddingConfig.from_env(default_provider="bge-m3"))
        if provider is None:
            raise RuntimeError("real retrieval eval requires a non-disabled embedding provider")
        chunks = store.list_chunks_for_sources({item.source_item_id for item in store.list_source_items()})
        embedding_report = EmbeddingService(store, provider).embed_chunks(chunks)
        if embedding_report.failed:
            raise RuntimeError(f"real retrieval eval embedding failed: {embedding_report.errors}")
    else:
        provider = FixtureEmbeddingProvider(fixture.get("query_vectors") or {})
        embedding_report = None
    retrieval = RetrievalService(store, ACLService(store), embedding_provider=provider)
    agentic = build_agentic_service_client() if real or require_llm else None
    user = store.get_user(str(fixture.get("user_id") or "user_primary"))
    case_reports = []
    for case in fixture.get("cases") or []:
        response = retrieval.search(str(case["query"]), user, top_k=int(case.get("top_k") or 5))
        agentic_response = None
        if agentic and case.get("agentic", True):
            agentic_response = agentic.search(str(case["query"]), user, max_iterations=int(case.get("agentic_max_iterations") or 2))
        case_reports.append(evaluate_response(case, response, agentic_response=agentic_response))
    return {
        "ok": all(report["ok"] for report in case_reports),
        "fixture": str(fixture_path),
        "real": real,
        "embedding": {
            "provider": provider.provider_name,
            "model": provider.model_name,
            "dimensions": provider.dimensions,
            "backfill": to_jsonable(embedding_report) if embedding_report else None,
        },
        "llm": {
            "required": False,
            "delegated_to_agentic_service": bool(real or require_llm),
            "provider": getattr(getattr(agentic, "config", None), "provider", None),
            "url": getattr(getattr(agentic, "config", None), "url", None),
            "agentic_cases": len([case for case in case_reports if case.get("agentic", {}).get("ran")]),
        },
        "case_count": len(case_reports),
        "cases": case_reports,
    }


def build_eval_store(fixture: dict[str, Any]) -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    for user in fixture.get("users") or [{"user_id": "user_primary", "handle": "primary", "role": "admin"}]:
        store.add_user(User(str(user["user_id"]), str(user.get("handle") or user["user_id"]), UserRole(str(user.get("role") or "user"))))
    for source in fixture.get("sources") or []:
        visibility = Visibility(str(source.get("visibility") or Visibility.PRIVATE.value))
        source_item = SourceItem(
            source_item_id=str(source["source_item_id"]),
            source_channel=str(source.get("source_channel") or "fixture"),
            record_type=str(source.get("record_type") or "note"),
            source_id=str(source.get("source_id") or source["source_item_id"]),
            owner_user_id=str(source.get("owner_user_id") or fixture.get("user_id") or "user_primary"),
            space_id=str(source.get("space_id") or "private_primary"),
            visibility=visibility,
            visible_team_ids=list(source.get("visible_team_ids") or []),
            title=str(source.get("title") or source["source_item_id"]),
            url=source.get("url"),
            content_text=str(source.get("text") or ""),
            content_hash=str(source.get("content_hash") or source["source_item_id"]),
            metadata=dict(source.get("metadata") or {}),
        )
        store.upsert_source_item(source_item)
        document_id = str(source.get("document_id") or f"doc_{source_item.source_item_id}")
        store.add_document(
            Document(
                document_id=document_id,
                source_item_id=source_item.source_item_id,
                owner_user_id=source_item.owner_user_id,
                space_id=source_item.space_id,
                visibility=source_item.visibility,
                visible_team_ids=source_item.visible_team_ids,
                title=source_item.title,
                body=source_item.content_text,
                metadata={},
            )
        )
        chunks = source.get("chunks") or [{"chunk_id": f"chk_{source_item.source_item_id}_0", "text": source_item.content_text}]
        for ordinal, chunk in enumerate(chunks):
            store.add_chunk(
                Chunk(
                    chunk_id=str(chunk["chunk_id"]),
                    document_id=document_id,
                    source_item_id=source_item.source_item_id,
                    owner_user_id=source_item.owner_user_id,
                    space_id=source_item.space_id,
                    visibility=source_item.visibility,
                    visible_team_ids=source_item.visible_team_ids,
                    text=str(chunk.get("text") or source_item.content_text),
                    ordinal=int(chunk.get("ordinal") if chunk.get("ordinal") is not None else ordinal),
                    embedding=list(chunk.get("embedding") or []) or None,
                    metadata={"embedding_provider": "fixture-embeddings", "embedding_model": "retrieval-eval-fixture"},
                )
            )
    graph = HypergraphService(store)
    for entity in fixture.get("entities") or []:
        graph.create_entity(
            Entity(
                entity_id=str(entity["entity_id"]),
                entity_type=str(entity.get("entity_type") or "concept"),
                label=str(entity["label"]),
                owner_user_id=str(entity.get("owner_user_id") or fixture.get("user_id") or "user_primary"),
                space_id=str(entity.get("space_id") or "private_primary"),
                visibility=Visibility(str(entity.get("visibility") or Visibility.PRIVATE.value)),
                metadata=dict(entity.get("metadata") or {}),
            )
        )
    for edge in fixture.get("hyperedges") or []:
        graph.create_hyperedge(
            relation_type=str(edge["relation_type"]),
            owner_user_id=str(edge.get("owner_user_id") or fixture.get("user_id") or "user_primary"),
            space_id=str(edge.get("space_id") or "private_primary"),
            visibility=Visibility(str(edge.get("visibility") or Visibility.PRIVATE.value)),
            directionality=Directionality(str(edge.get("directionality") or Directionality.AMBIGUOUS.value)),
            members=[(str(member["entity_id"]), str(member.get("role") or "related")) for member in edge.get("members") or []],
            evidence_text=str(edge.get("evidence_text") or ""),
            source_refs=[_source_ref(ref) for ref in edge.get("source_refs") or []],
            confidence=float(edge.get("confidence") or 0.0),
        )
    for memory in fixture.get("agent_memories") or []:
        store.add_agent_memory(
            AgentMemory(
                agent_memory_id=str(memory["agent_memory_id"]),
                owner_user_id=str(memory.get("owner_user_id") or fixture.get("user_id") or "user_primary"),
                layer=MemoryLayer(str(memory.get("layer") or MemoryLayer.SEMANTIC.value)),
                text=str(memory["text"]),
                confidence=float(memory.get("confidence") or 0.0),
                source_refs=[_source_ref(ref) for ref in memory.get("source_refs") or []],
                decay_policy=str(memory.get("decay_policy") or "manual"),
                created_by_user_id=memory.get("created_by_user_id"),
            )
        )
    for card in fixture.get("profile_cards") or []:
        store.add_profile_card(
            UserProfileCard(
                profile_card_id=str(card["profile_card_id"]),
                owner_user_id=str(card.get("owner_user_id") or fixture.get("user_id") or "user_primary"),
                profile=dict(card.get("profile") or {}),
                source_refs=[_source_ref(ref) for ref in card.get("source_refs") or []],
                confidence=float(card.get("confidence") or 0.0),
            )
        )
    return store


def evaluate_response(case: dict[str, Any], response: RetrievalResponse, *, agentic_response: Any | None = None) -> dict[str, Any]:
    expected_citations = list(case.get("expected_citations") or [])
    actual_citations = [_citation_key(item) for item in response.citations]
    citation_report = _expected_report(expected_citations, actual_citations)

    expected_lexical = list(case.get("expected_lexical_source_item_ids") or [])
    actual_lexical = [
        result.source_item_id
        for result in response.results
        if result.score_debug.get("lexical", 0.0) > 0 or result.score_debug.get("lexical_rank")
    ]
    lexical_report = _expected_report(expected_lexical, actual_lexical)

    expected_vector = list(case.get("expected_vector_source_item_ids") or [])
    actual_vector = [
        result.source_item_id
        for result in response.results
        if result.score_debug.get("vector", 0.0) > 0 or result.score_debug.get("vector_rank")
    ]
    vector_report = _expected_report(expected_vector, actual_vector)

    expected_paths = list(case.get("expected_graph_paths") or [])
    actual_paths = [_graph_path_signature(path) for path in response.graph_paths]
    graph_path_report = _graph_path_report(expected_paths, response.graph_paths, actual_paths)

    expected_gaps = list(case.get("expected_gaps") or [])
    expected_conflicts = list(case.get("expected_conflicts") or [])
    gap_report = _expected_report(expected_gaps, response.gaps)
    conflict_report = _expected_report(expected_conflicts, response.conflicts)
    memory_report = _expected_report(list(case.get("expected_memory_ids") or []), [item.get("agent_memory_id") for item in response.memory_context])
    profile_report = _expected_report(list(case.get("expected_profile_card_ids") or []), [item.get("profile_card_id") for item in response.profile_context])
    agentic_report = _agentic_report(case, agentic_response)

    checks = {
        "citations": citation_report["ok"],
        "lexical": lexical_report["ok"],
        "vector": vector_report["ok"],
        "graph_paths": graph_path_report["ok"],
        "gaps": gap_report["ok"],
        "conflicts": conflict_report["ok"],
        "memory": memory_report["ok"],
        "profile": profile_report["ok"],
        "agentic": agentic_report["ok"],
    }
    return {
        "case_id": case.get("id"),
        "query": case.get("query"),
        "ok": all(checks.values()),
        "checks": checks,
        "citations": citation_report,
        "lexical": lexical_report,
        "vector": vector_report,
        "graph_paths": graph_path_report,
        "gaps": gap_report,
        "conflicts": conflict_report,
        "memory": memory_report,
        "profile": profile_report,
        "agentic": agentic_report,
        "diagnostics": {
            "score_debug": to_jsonable(response.score_debug),
            "result_count": len(response.results),
            "citation_count": len(response.citations),
            "graph_path_count": len(response.graph_paths),
            "graph_path_explanations": [path.get("explanation") for path in response.graph_paths],
            "memory_context_count": len(response.memory_context),
            "profile_context_count": len(response.profile_context),
        },
    }


def _agentic_report(case: dict[str, Any], agentic_response: Any | None) -> dict[str, Any]:
    if agentic_response is None:
        return {"ok": True, "ran": False}
    if isinstance(agentic_response, dict):
        answer = str(agentic_response.get("answer") or "")
        retrieval = agentic_response.get("retrieval") if isinstance(agentic_response.get("retrieval"), dict) else {}
        trace = agentic_response.get("trace") if isinstance(agentic_response.get("trace"), dict) else {}
        raw_citations = retrieval.get("citations") or agentic_response.get("source_refs") or []
    else:
        answer = getattr(agentic_response, "answer", "")
        retrieval = getattr(agentic_response, "retrieval", None)
        trace = getattr(agentic_response, "trace", None)
        raw_citations = getattr(retrieval, "citations", [])
    citations = [_citation_key(item) for item in raw_citations if isinstance(item, dict)]
    expected = list(case.get("expected_agentic_citations") or case.get("expected_citations") or [])
    citation_report = _expected_report(expected, citations)
    return {
        "ok": bool(answer.strip()) and citation_report["ok"],
        "ran": True,
        "answer": answer,
        "trace": to_jsonable(trace),
        "citations": citation_report,
    }


def _expected_report(expected: list[str], actual: list[str]) -> dict[str, Any]:
    missing = [item for item in expected if item not in actual]
    return {
        "ok": not missing,
        "expected": expected,
        "actual": actual,
        "missing": missing,
    }


def _graph_path_report(expected_paths: list[dict[str, Any]], response_paths: list[dict[str, Any]], actual_signatures: list[str]) -> dict[str, Any]:
    expected_signatures = [_graph_path_expected_signature(path) for path in expected_paths]
    missing = [signature for signature in expected_signatures if signature not in actual_signatures]
    path_details = []
    for path in response_paths:
        path_details.append(
            {
                "signature": _graph_path_signature(path),
                "explanation": path.get("explanation"),
                "score": path.get("score"),
                "score_debug": path.get("score_debug"),
                "evidence_source_item_ids": [
                    citation.get("source_item_id")
                    for edge in path.get("edges", [])
                    for citation in edge.get("evidence_citations", [])
                ],
            }
        )
    return {
        "ok": not missing,
        "expected": expected_signatures,
        "actual": actual_signatures,
        "missing": missing,
        "details": path_details,
    }


def _citation_key(citation: dict[str, Any]) -> str:
    return f"{citation.get('source_item_id')}#{citation.get('chunk_id')}"


def _graph_path_signature(path: dict[str, Any]) -> str:
    labels = [entity.get("label") for entity in path.get("entities", [])]
    relations = [edge.get("relation_type") for edge in path.get("edges", [])]
    evidence_sources = [
        citation.get("source_item_id")
        for edge in path.get("edges", [])
        for citation in edge.get("evidence_citations", [])
    ]
    return "|".join([" > ".join(labels), " / ".join(relations), ",".join(evidence_sources)])


def _graph_path_expected_signature(path: dict[str, Any]) -> str:
    return "|".join(
        [
            " > ".join(path.get("entity_labels") or []),
            " / ".join(path.get("relation_types") or []),
            ",".join(path.get("evidence_source_item_ids") or []),
        ]
    )


def _source_ref(ref: dict[str, Any]) -> SourceRef:
    allowed = set(SourceRef.__dataclass_fields__)
    return SourceRef(**{key: value for key, value in ref.items() if key in allowed})
