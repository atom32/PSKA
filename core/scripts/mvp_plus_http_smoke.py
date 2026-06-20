from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pska_core.acl import ACLService
from pska_core.api import PSKAApi, PSKARequestHandler
from pska_core.candidates import CandidateWriteService
from pska_core.enums import UserRole
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.jobs import JobService
from pska_core.mcp_server import MCPServer
from pska_core.memory import MemoryService
from pska_core.models import User
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.serde import dumps, to_jsonable
from pska_core.store import InMemoryKnowledgeStore
from scripts.mvp_plus_smoke import FakeLLM, _extraction_response


def build_http_smoke_report() -> dict[str, Any]:
    api = _api()
    server = _SmokeServer(api)
    with server:
        base_url = f"127.0.0.1:{server.port}"
        health = _http_json(base_url, "GET", "/health")
        planning_source = _http_json(base_url, "POST", "/ingest/channel-payload", _planning_payload())
        extract = _http_json(base_url, "POST", "/extract/all", {"owner_user_id": "user_primary"})
        digest_source = _http_json(base_url, "POST", "/ingest/channel-payload", _digest_payload())
        conflict_source = _http_json(base_url, "POST", "/ingest/channel-payload", _conflict_payload())
        candidate_write = _http_json(
            base_url,
            "POST",
            "/candidates",
            _candidate_payload(digest_source["source_item_id"]),
        )
        conflict_write = _http_json(
            base_url,
            "POST",
            "/candidates",
            _conflict_candidate_payload(conflict_source["source_item_id"]),
        )
        digest = _http_json(
            base_url,
            "POST",
            "/digest/schedule",
            {"owner_user_id": "user_primary", "source_item_ids": [digest_source["source_item_id"]], "limit": 1},
        )
        job_id = digest["job"]["job_id"]
        job_context = _http_json(base_url, "GET", f"/digest/batches/{job_id}?limit=1")
        search = _http_json(base_url, "POST", "/search", {"query": "PSKA Digest FastReAct relation path", "user_id": "user_primary"})
        memory = _http_json(base_url, "POST", "/search", {"query": "concise PSKA preference", "user_id": "user_primary"})
        profile = _http_json(base_url, "POST", "/search", {"query": "profile communication style", "user_id": "user_primary"})
        conflict = _http_json(base_url, "POST", "/search", {"query": "Claim A Claim B", "user_id": "user_primary"})
        sensitive = _http_json(base_url, "POST", "/search", {"query": "api key rotation", "user_id": "user_primary"})
        agentic = _http_json(
            base_url,
            "POST",
            "/agentic-search",
            {"query": "What covers dependent K during education enrollment?", "user_id": "user_primary"},
        )
        mcp = _http_json(
            base_url,
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": "mvp-plus-http-mcp",
                "method": "tools/call",
                "params": {
                    "name": "pska_search",
                    "arguments": {"query": "PSKA Digest FastReAct", "user_id": "user_primary"},
                },
            },
        )
        ready = _http_json(base_url, "GET", "/ready")

    checks = {
        "health_ok": health["ok"] is True,
        "limited_sources_ingested": bool(
            planning_source["source_item_id"]
            and digest_source["source_item_id"]
            and conflict_source["source_item_id"]
        ),
        "extract_all_created_graph": bool(extract["reports"][0]["entities_created"] and extract["reports"][0]["hyperedges_created"]),
        "candidate_write_created_memory_profile_graph": bool(
            candidate_write["summary"]["hyperedges"]
            and candidate_write["summary"]["agent_memories"]
            and candidate_write["summary"]["profile_cards"]
        ),
        "digest_job_context_available": bool(job_context["source_items"] and job_context["chunks"]),
        "graphrag_has_grounded_path": bool(search["graph_paths"] and search["graph_paths"][0]["edges"][0]["evidence_citations"]),
        "memory_context_has_citation": bool(memory["memory_context"] and memory["memory_context"][0]["citations"]),
        "profile_context_has_citation": bool(profile["profile_context"] and profile["profile_context"][0]["citations"]),
        "conflict_diagnostic_present": bool(conflict["conflicts"]),
        "sensitivity_flag_present": bool(sensitive["sensitivity"]),
        "agentic_qa_has_answer_and_citations": bool(agentic["answer"] and agentic["retrieval"]["citations"]),
        "http_mcp_search_returns_content": bool(mcp.get("result", {}).get("content")),
        "ready_contract_ok": ready["ok"] is True and ready["checks"]["mcp"]["ok"] is True,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "sample": {
            "candidate_summary": candidate_write["summary"],
            "conflict_summary": conflict_write["summary"],
            "digest_job_id": job_id,
            "agentic_answer": agentic["answer"],
            "graph_path": search["graph_paths"][0] if search["graph_paths"] else None,
            "memory_context": memory["memory_context"],
            "profile_context": profile["profile_context"],
            "conflicts": conflict["conflicts"],
            "sensitivity": sensitive["sensitivity"],
            "ready_agentic_service_ok": ready["checks"]["agentic_service"]["ok"],
        },
    }


def main() -> int:
    report = build_http_smoke_report()
    print(dumps(report))
    return 0 if report["ok"] else 1


class _SmokeServer:
    def __init__(self, api: PSKAApi) -> None:
        class Handler(PSKARequestHandler):
            pass

        Handler.api = api
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_SmokeServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _api() -> PSKAApi:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    llm = FakeLLM([_extraction_response()])
    api = object.__new__(PSKAApi)
    api.store = store
    api.retrieval = RetrievalService(store, ACLService(store))
    api.agentic_service = _FakeAgenticService(api.retrieval)
    api.ingest = IngestService(store)
    api.extraction = ExtractionService(store, llm=llm)
    api.jobs = JobService(store)
    api.reviews = ReviewService(store)
    api.memory = MemoryService(store)
    api.candidates = CandidateWriteService(store)
    api.mcp = MCPServer("postgresql:///unused", store=store)
    return api


class _FakeAgenticService:
    def __init__(self, retrieval: RetrievalService) -> None:
        self.retrieval = retrieval

    def ready(self) -> dict[str, Any]:
        return {"ok": True, "provider": "test", "adapter": "fake"}

    def search(self, query: str, user: User, *, represented_user_id: str | None = None, max_iterations: int = 3) -> dict[str, Any]:
        retrieval = self.retrieval.search(query, user, represented_user_id=represented_user_id)
        return {
            "answer": "Policy P-204 covers dependent K during education enrollment.",
            "retrieval": to_jsonable(retrieval),
            "trace": {"retrieval_plan": ["external_agentic_service", "pska_search"]},
            "agentic_service": {"provider": "test", "adapter": "fake"},
        }


def _http_json(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload or {}).encode("utf-8") if method != "GET" else None
    conn = HTTPConnection(base_url, timeout=10)
    headers = {"content-type": "application/json"}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    conn.close()
    parsed = json.loads(data) if data else {}
    if response.status >= 400:
        raise AssertionError(f"{method} {path} failed HTTP {response.status}: {parsed}")
    return parsed


def _planning_payload() -> dict[str, Any]:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "planning_note",
        "source_id": "mvp-plus-http-planning",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": "MVP+ HTTP Planning Note",
        "content": {
            "text": (
                "Project Atlas is the shared knowledge-base initiative. "
                "The policy P-204 covers the education enrollment stage for dependent K. "
                "The Review Agent must confirm team-visible sharing."
            )
        },
    }


def _digest_payload() -> dict[str, Any]:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "digest_note",
        "source_id": "mvp-plus-http-digest",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": "MVP+ HTTP Digest Note",
        "content": {
            "text": (
                "PSKA delegates complex agentic work to FastReAct. "
                "FastReAct executes digest loops for PSKA. "
                "The user prefers concise PSKA answers."
            )
        },
    }


def _conflict_payload() -> dict[str, Any]:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "claim_note",
        "source_id": "mvp-plus-http-conflict",
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": "MVP+ HTTP Conflict Note",
        "content": {"text": "Claim A contradicts Claim B."},
    }


def _candidate_payload(source_item_id: str) -> dict[str, Any]:
    return {
        "schema_version": "pska.candidates.v1",
        "owner_user_id": "user_primary",
        "producer": "mvp_plus_http_smoke",
        "request_id": "mvp-plus-http-smoke",
        "source_refs": [{"source_item_id": source_item_id}],
        "entities": [
            {"entity_type": "project", "label": "PSKA"},
            {"entity_type": "service", "label": "FastReAct", "metadata": {"aliases": ["FR", "FastReact"]}},
            {"entity_type": "workflow", "label": "Digest"},
        ],
        "hyperedges": [
            {
                "relation_type": "delegates_to",
                "directionality": "directed",
                "evidence_text": "PSKA delegates complex agentic work to FastReAct.",
                "confidence": 0.9,
                "members": [
                    {"entity_type": "project", "label": "PSKA", "role": "caller"},
                    {"entity_type": "service", "label": "FastReAct", "role": "executor"},
                ],
            },
            {
                "relation_type": "executes",
                "directionality": "directed",
                "evidence_text": "FastReAct executes digest loops for PSKA.",
                "confidence": 0.86,
                "members": [
                    {"entity_type": "service", "label": "FastReAct", "role": "executor"},
                    {"entity_type": "workflow", "label": "Digest", "role": "workflow"},
                ],
            },
        ],
        "memory_candidates": [
            {"kind": "agent_memory", "layer": "semantic", "text": "User prefers concise PSKA answers.", "confidence": 0.9},
            {"kind": "profile", "profile_delta": {"communication": {"style": "concise"}}, "confidence": 0.8},
        ],
    }


def _conflict_candidate_payload(source_item_id: str) -> dict[str, Any]:
    return {
        "schema_version": "pska.candidates.v1",
        "owner_user_id": "user_primary",
        "producer": "mvp_plus_http_smoke",
        "request_id": "mvp-plus-http-conflict",
        "source_refs": [{"source_item_id": source_item_id}],
        "entities": [
            {"entity_type": "claim", "label": "Claim A"},
            {"entity_type": "claim", "label": "Claim B"},
        ],
        "hyperedges": [
            {
                "relation_type": "contradicts",
                "evidence_text": "Claim A contradicts Claim B.",
                "confidence": 0.9,
                "members": [
                    {"entity_type": "claim", "label": "Claim A", "role": "left"},
                    {"entity_type": "claim", "label": "Claim B", "role": "right"},
                ],
            }
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
