from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pska_core.acl import ACLService
from pska_core.retrieval import RetrievalService
from pska_core.serde import dumps
from pska_core.store_postgres import PostgresKnowledgeStore


DEFAULT_DATABASE_URL = "postgresql:///pska_mvp_plus_sample"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_current_sample_gate_report(args)
    print(dumps(report))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the current PSKA Postgres sample database without resetting it.")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--owner-user-id", default="user_primary")
    parser.add_argument("--query", default="")
    parser.add_argument("--require-graph", action="store_true", help="Require entities, hyperedges, and graph evidence citations.")
    parser.add_argument("--require-review-or-memory", action="store_true", help="Require pending review items, agent memories, or profile cards.")
    return parser


def build_current_sample_gate_report(args: argparse.Namespace) -> dict[str, Any]:
    store = PostgresKnowledgeStore(args.database_url)
    counts = _counts(store)
    user = store.get_user(args.owner_user_id)
    query = args.query or _sample_query(store)
    retrieval = RetrievalService(store, ACLService(store)).search(query, user)
    checks = {
        "database_accessible": True,
        "sources_exist": counts["source_items"] > 0,
        "chunks_exist": counts["chunks"] > 0,
        "search_returns_results": bool(retrieval.results),
        "search_has_citations": bool(retrieval.citations),
        "digest_job_exists": counts["digest_jobs"] > 0,
    }
    if args.require_graph:
        checks.update(
            {
                "entities_exist": counts["entities"] > 0,
                "hyperedges_exist": counts["hyperedges"] > 0,
                "graph_has_evidence_citations": _has_graph_evidence_citations(retrieval),
            }
        )
    if args.require_review_or_memory:
        checks["review_or_memory_exists"] = (
            counts["review_items"] > 0 or counts["agent_memories"] > 0 or counts["user_profile_cards"] > 0
        )
    return {
        "ok": all(checks.values()),
        "database_url": args.database_url,
        "owner_user_id": args.owner_user_id,
        "query": query,
        "checks": checks,
        "counts": counts,
        "sample": {
            "result_count": len(retrieval.results),
            "citation_count": len(retrieval.citations),
            "graph_context_count": len(retrieval.hypergraph_context),
            "graph_path_count": len(retrieval.graph_paths),
            "top_result": retrieval.results[0] if retrieval.results else None,
        },
        "next_actions": _next_actions(checks),
    }


def _counts(store: PostgresKnowledgeStore) -> dict[str, int]:
    return {
        "source_items": store.count_table("source_items"),
        "documents": store.count_table("documents"),
        "chunks": store.count_table("chunks"),
        "entities": store.count_table("entities"),
        "hyperedges": store.count_table("hyperedges"),
        "review_items": store.count_table("review_items"),
        "agent_memories": store.count_table("agent_memories"),
        "user_profile_cards": store.count_table("user_profile_cards"),
        "jobs": store.count_table("jobs"),
        "digest_jobs": len(store.list_jobs(job_type="digest_via_fastreact", limit=1000)),
    }


def _sample_query(store: PostgresKnowledgeStore) -> str:
    for item in store.list_source_items():
        for token in item.title.split():
            if len(token) >= 4:
                return token
        for token in item.content_text.split():
            normalized = token.strip(".,:;!?()[]{}\"'")
            if len(normalized) >= 4:
                return normalized
    return "archive"


def _has_graph_evidence_citations(retrieval: Any) -> bool:
    graph_edges = list(retrieval.hypergraph_context)
    for path in retrieval.graph_paths:
        graph_edges.extend(path.get("edges") or [])
    return any(edge.get("evidence_citations") for edge in graph_edges if isinstance(edge, dict))


def _next_actions(checks: dict[str, bool]) -> list[str]:
    actions = []
    if not checks.get("sources_exist") or not checks.get("chunks_exist"):
        actions.append("Run scripts/mvp_plus_real_sample_smoke.py --skip-llm to rebuild a small current sample database.")
    if checks.get("sources_exist") and not checks.get("entities_exist", True):
        actions.append("Run ./scripts/pska --database-url postgresql:///pska_mvp_plus_sample extract-all --owner-user-id user_primary.")
    if checks.get("digest_job_exists") and not checks.get("review_or_memory_exists", True):
        actions.append("Run the Fastreact PSKA digest worker to write review or memory candidates.")
    if not checks.get("graph_has_evidence_citations", True):
        actions.append("Re-run search after extraction and verify graph evidence citations.")
    if not actions:
        actions.append("Current sample database passes the requested MVP+ gate.")
    return actions


if __name__ == "__main__":
    raise SystemExit(main())
