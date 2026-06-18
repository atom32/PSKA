from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pska_core.acl import ACLService
from pska_core.embeddings import EmbeddingProvider
from pska_core.enums import Directionality, UserRole, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.models import Chunk, Document, Entity, SourceItem, SourceRef, User
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


def run_retrieval_eval(fixture_path: str | Path = DEFAULT_RETRIEVAL_EVAL_FIXTURE) -> dict[str, Any]:
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    store = build_eval_store(fixture)
    provider = FixtureEmbeddingProvider(fixture.get("query_vectors") or {})
    retrieval = RetrievalService(store, ACLService(store), embedding_provider=provider)
    user = store.get_user(str(fixture.get("user_id") or "user_primary"))
    case_reports = []
    for case in fixture.get("cases") or []:
        response = retrieval.search(str(case["query"]), user, top_k=int(case.get("top_k") or 5))
        case_reports.append(evaluate_response(case, response))
    return {
        "ok": all(report["ok"] for report in case_reports),
        "fixture": str(fixture_path),
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
    return store


def evaluate_response(case: dict[str, Any], response: RetrievalResponse) -> dict[str, Any]:
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

    checks = {
        "citations": citation_report["ok"],
        "lexical": lexical_report["ok"],
        "vector": vector_report["ok"],
        "graph_paths": graph_path_report["ok"],
        "gaps": gap_report["ok"],
        "conflicts": conflict_report["ok"],
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
        "diagnostics": {
            "score_debug": to_jsonable(response.score_debug),
            "result_count": len(response.results),
            "citation_count": len(response.citations),
            "graph_path_count": len(response.graph_paths),
            "graph_path_explanations": [path.get("explanation") for path in response.graph_paths],
        },
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
