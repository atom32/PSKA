from __future__ import annotations

import json
import sys
from typing import Any

from pska_core.acl import ACLService
from pska_core.auth import RequestContext
from pska_core.candidates import CandidateWriteService
from pska_core.config import DatabaseConfig, PSKAConfig
from pska_core.embeddings import build_embedding_provider
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.models import DEFAULT_TENANT_ID, ChannelIngestPayload
from pska_core.retrieval import RetrievalService
from pska_core.serde import to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


PROTOCOL_VERSION = "2024-11-05"


TOOLS = [
    {
        "name": "pska_search",
        "description": "Search the PSKA knowledge base with ACL filtering and citations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tenant_id": {"type": "string", "default": DEFAULT_TENANT_ID},
                "user_id": {"type": "string", "default": "user_primary"},
                "represented_user_id": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
                "max_results": {"type": "integer", "default": 3},
                "max_snippet_chars": {"type": "integer", "default": 700},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pska_index_status",
        "description": "Return basic PSKA index counts.",
        "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string", "default": DEFAULT_TENANT_ID}}, "required": []},
    },
    {
        "name": "pska_ingest_channel_payload",
        "description": "Ingest a PSKA channel payload into the knowledge base.",
        "inputSchema": {"type": "object", "properties": {"payload": {"type": "object"}}, "required": ["payload"]},
    },
    {
        "name": "pska_extract_all",
        "description": "Extract MVP entities, hyperedges, and review items from source items.",
        "inputSchema": {
            "type": "object",
            "properties": {"tenant_id": {"type": "string", "default": DEFAULT_TENANT_ID}, "owner_user_id": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "pska_review_items",
        "description": "List PSKA review items.",
        "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string", "default": DEFAULT_TENANT_ID}}, "required": []},
    },
    {
        "name": "pska_write_candidates",
        "description": "Write Fastreact-generated PSKA candidates with required source refs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner_user_id": {"type": "string"},
                "tenant_id": {"type": "string", "default": DEFAULT_TENANT_ID},
                "schema_version": {"type": "string", "default": "pska.candidates.v1"},
                "job_id": {"type": "string"},
                "request_id": {"type": "string"},
                "source_refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_item_id": {"type": "string"},
                            "document_id": {"type": "string"},
                            "passage_window_id": {"type": "string"},
                            "chunk_id": {"type": "string"},
                            "message_id": {"type": "string"},
                            "path": {"type": "string"},
                            "url": {"type": "string"},
                        },
                    },
                },
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_type": {"type": "string"},
                            "label": {"type": "string"},
                            "confidence": {"type": "number"},
                            "source_refs": {"type": "array", "items": {"type": "object"}},
                            "metadata": {"type": "object"},
                        },
                        "required": ["entity_type", "label"],
                    },
                },
                "hyperedges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "relation_type": {"type": "string"},
                            "members": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "entity_type": {"type": "string"},
                                        "label": {"type": "string"},
                                        "role": {"type": "string"},
                                    },
                                    "required": ["entity_type", "label", "role"],
                                },
                            },
                            "evidence_text": {"type": "string"},
                            "confidence": {"type": "number"},
                            "source_refs": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["relation_type", "members", "evidence_text"],
                    },
                },
                "knowledge_claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_type": {"type": "string"},
                            "dedupe_key": {"type": "string"},
                            "statement": {"type": "string"},
                            "subject": {"type": "string"},
                            "predicate": {"type": "string"},
                            "object": {"type": "string"},
                            "qualifiers": {"type": "object"},
                            "evidence_text": {"type": "string"},
                            "confidence": {"type": "number"},
                            "source_refs": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["claim_type", "statement", "evidence_text", "source_refs"],
                    },
                },
                "digest_notes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "dedupe_key": {"type": "string"},
                            "synopsis": {"type": "string"},
                            "key_points": {"type": "array", "items": {"type": "object"}},
                            "actions": {"type": "array", "items": {"type": "object"}},
                            "open_questions": {"type": "array", "items": {"type": "object"}},
                            "risks": {"type": "array", "items": {"type": "object"}},
                            "memory_suggestions": {"type": "array", "items": {"type": "object"}},
                            "relationship_suggestions": {"type": "array", "items": {"type": "object"}},
                            "confidence": {"type": "number"},
                            "source_refs": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["title", "synopsis", "source_refs"],
                    },
                },
                "review_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "review_type": {"type": "string"},
                            "title": {"type": "string"},
                            "proposal": {"type": "object"},
                            "source_refs": {"type": "array", "items": {"type": "object"}},
                        },
                        "required": ["review_type", "title", "proposal"],
                    },
                },
                "memory_candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "layer": {"type": "string"},
                            "text": {"type": "string"},
                            "confidence": {"type": "number"},
                            "sensitivity": {"type": "string"},
                            "profile_delta": {"type": "object"},
                            "source_refs": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                },
            },
            "required": ["source_refs"],
        },
    },
    {
        "name": "pska_job_context",
        "description": "Return source/chunk context for a PSKA job within the represented user's scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tenant_id": {"type": "string", "default": DEFAULT_TENANT_ID},
                "user_id": {"type": "string", "default": "user_primary"},
                "represented_user_id": {"type": "string"},
                "cursor": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 1},
                "max_source_chars": {"type": "integer", "default": 300},
                "max_document_chars": {"type": "integer", "default": 500},
                "max_passage_chars": {"type": "integer", "default": 500},
                "max_chunk_chars": {"type": "integer", "default": 240},
                "max_chunks": {"type": "integer", "default": 1},
            },
            "required": ["job_id"],
        },
    },
]


def main() -> int:
    config = PSKAConfig.load()
    server = MCPServer(config.database.url, config=config)
    return server.run()


class MCPServer:
    def __init__(self, database_url: str, store: Any | None = None, llm: Any | None = None, config: PSKAConfig | None = None) -> None:
        if config is None:
            config = PSKAConfig(database=DatabaseConfig(url=database_url))
        self.config = config
        self.store = store or PostgresKnowledgeStore(database_url)
        embedding_provider = build_embedding_provider(config.embedding_runtime_config())
        self.retrieval = RetrievalService(self.store, ACLService(self.store), embedding_provider=embedding_provider)
        self.ingest = IngestService(self.store, embedding_provider=embedding_provider, **config.ingest_kwargs())
        self.extraction = ExtractionService(self.store, llm=llm, llm_config=config.llm)
        self.candidates = CandidateWriteService(self.store)

    def run(self) -> int:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle(request)
                if response is not None:
                    self.write(response)
            except Exception as exc:  # noqa: BLE001 - MCP server must report errors as JSON-RPC.
                request_id = None
                try:
                    request_id = json.loads(line).get("id")
                except Exception:
                    pass
                self.write(self.error(request_id, -32000, f"{type(exc).__name__}: {exc}"))
        return 0

    def handle(self, request: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            return self.result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": "pska-core", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self.result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params") or {}
            mcp_context = _context_from_mcp_params(params)
            if context and mcp_context and not context.service_authenticated:
                _assert_context_matches(context, mcp_context)
            return self.result(
                request_id,
                self.call_tool(params.get("name"), params.get("arguments") or {}, context=mcp_context or context),
            )
        return self.error(request_id, -32601, f"Unknown method: {method}")

    def call_tool(self, name: str, arguments: dict[str, Any], *, context: RequestContext | None = None) -> dict[str, Any]:
        arguments = _apply_mcp_context(arguments, context) if context else arguments
        if name == "pska_search":
            payload = self.pska_search(arguments)
        elif name == "pska_index_status":
            payload = self.pska_index_status(arguments)
        elif name == "pska_ingest_channel_payload":
            payload = self.pska_ingest_channel_payload(arguments)
        elif name == "pska_extract_all":
            payload = self.pska_extract_all(arguments)
        elif name == "pska_review_items":
            payload = self.pska_review_items(arguments)
        elif name == "pska_write_candidates":
            payload = self.pska_write_candidates(arguments)
        elif name == "pska_job_context":
            payload = self.pska_job_context(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return {"content": [{"type": "text", "text": json.dumps(to_jsonable(payload), ensure_ascii=False)}]}

    def pska_search(self, arguments: dict[str, Any]) -> Any:
        tenant_id = str(arguments.get("tenant_id") or DEFAULT_TENANT_ID)
        user = self.store.get_user(arguments.get("user_id") or "user_primary", tenant_id=tenant_id)
        response = self.retrieval.search(
            arguments["query"],
            user,
            represented_user_id=arguments.get("represented_user_id"),
            top_k=int(arguments.get("top_k") or 5),
        )
        return _compact_search_response(
            to_jsonable(response),
            max_results=_bounded_int(arguments.get("max_results"), default=3, minimum=1, maximum=5),
            max_snippet_chars=_bounded_int(arguments.get("max_snippet_chars"), default=700, minimum=120, maximum=1600),
        )

    def pska_index_status(self, arguments: dict[str, Any] | None = None) -> dict[str, int | bool]:
        tenant_id = str((arguments or {}).get("tenant_id") or DEFAULT_TENANT_ID)
        source_items = self.store.list_source_items(tenant_id=tenant_id)
        source_ids = {item.source_item_id for item in source_items}
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "source_items": len(source_items),
            "documents": len(self.store.list_documents_for_sources(source_ids)),
            "chunks": len(self.store.list_chunks_for_sources(source_ids)),
            "entities": len(self.store.list_entities(tenant_id=tenant_id)),
            "hyperedges": len(self.store.list_hyperedges_for_entities({entity.entity_id for entity in self.store.list_entities(tenant_id=tenant_id)})),
            "knowledge_claims": len(self.store.list_knowledge_claims(owner_user_id="user_primary", tenant_id=tenant_id, limit=10_000)),
            "digest_notes": len(self.store.list_digest_notes(owner_user_id="user_primary", tenant_id=tenant_id, limit=10_000)),
            "review_items": len(self.store.list_review_items(tenant_id=tenant_id)),
        }

    def pska_ingest_channel_payload(self, arguments: dict[str, Any]) -> Any:
        payload = dict(arguments["payload"])
        payload.setdefault("tenant_id", arguments.get("tenant_id") or DEFAULT_TENANT_ID)
        return self.ingest.ingest_channel_payload(ChannelIngestPayload.from_mapping(payload))

    def pska_extract_all(self, arguments: dict[str, Any]) -> Any:
        tenant_id = str(arguments.get("tenant_id") or DEFAULT_TENANT_ID)
        return {"reports": self.extraction.extract_all_visible(owner_user_id=arguments.get("owner_user_id"), tenant_id=tenant_id)}

    def pska_review_items(self, arguments: dict[str, Any] | None = None) -> Any:
        tenant_id = str((arguments or {}).get("tenant_id") or DEFAULT_TENANT_ID)
        return {"review_items": self.store.list_review_items(tenant_id=tenant_id)}

    def pska_write_candidates(self, arguments: dict[str, Any]) -> Any:
        arguments.setdefault("tenant_id", _tenant_id_for_job(self.store, arguments.get("job_id")) or DEFAULT_TENANT_ID)
        return {"summary": self.candidates.write_candidates(arguments)}

    def pska_job_context(self, arguments: dict[str, Any]) -> Any:
        job = self.store.get_job(str(arguments["job_id"]))
        tenant_id = str(arguments.get("tenant_id") or job.tenant_id or DEFAULT_TENANT_ID)
        if tenant_id != job.tenant_id:
            raise PermissionError("job tenant mismatch")
        request_user_id = str(arguments.get("represented_user_id") or arguments.get("user_id") or "user_primary")
        source_item_ids = _job_source_item_ids(job)
        candidate_items = [
            item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.source_item_id in source_item_ids and item.owner_user_id == request_user_id
        ]
        candidate_items = sorted(candidate_items, key=lambda item: (item.created_at, item.source_item_id))
        offset = _cursor_offset(arguments.get("cursor"))
        limit = _bounded_int(arguments.get("limit"), default=1, minimum=1, maximum=10)
        source_items = candidate_items[offset : offset + limit]
        next_offset = offset + len(source_items)
        has_more = next_offset < len(candidate_items)
        source_ids = {item.source_item_id for item in source_items}
        max_chunks = _bounded_int(arguments.get("max_chunks"), default=1, minimum=0, maximum=50)
        max_source_chars = _bounded_int(arguments.get("max_source_chars"), default=300, minimum=120, maximum=8000)
        max_document_chars = _bounded_int(arguments.get("max_document_chars"), default=500, minimum=500, maximum=6000)
        max_passage_chars = _bounded_int(arguments.get("max_passage_chars"), default=500, minimum=500, maximum=6000)
        max_passage_windows = _bounded_int(arguments.get("max_passage_windows"), default=3, minimum=0, maximum=20)
        max_chunk_chars = _bounded_int(arguments.get("max_chunk_chars"), default=240, minimum=120, maximum=4000)
        max_existing_claims = _bounded_int(arguments.get("max_existing_claims"), default=5, minimum=0, maximum=20)
        max_existing_digest_notes = _bounded_int(arguments.get("max_existing_digest_notes"), default=2, minimum=0, maximum=10)
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        passage_windows = _passage_windows_for_documents(documents, chunks, target_chars=max_passage_chars)
        return {
            "job": _compact_job(job),
            "tenant_id": tenant_id,
            "request_user_id": request_user_id,
            "source_items": [_compact_source_item(item, max_chars=max_source_chars) for item in source_items],
            "documents": [_compact_document(document, max_chars=max_document_chars) for document in documents],
            "passage_windows": [_compact_passage_window(window, max_chars=max_passage_chars) for window in passage_windows[:max_passage_windows]],
            "chunks": [_compact_chunk(chunk, max_chars=max_chunk_chars) for chunk in chunks[:max_chunks]],
            "knowledge_claims": [
                _compact_knowledge_claim(claim)
                for claim in self.store.list_knowledge_claims(owner_user_id=request_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=max_existing_claims)
            ],
            "digest_notes": [
                _compact_digest_note(note)
                for note in self.store.list_digest_notes(owner_user_id=request_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=max_existing_digest_notes)
            ],
            "agent_memories": [
                _compact_memory(memory)
                for memory in self.store.list_agent_memories(owner_user_id=request_user_id, tenant_id=tenant_id)[:1]
            ],
            "entities": [
                _compact_entity(entity)
                for entity in self.store.list_entities(tenant_id=tenant_id)
                if entity.owner_user_id == request_user_id
            ][:3],
            "cursor": str(offset),
            "next_cursor": str(next_offset) if has_more else None,
            "has_more": has_more,
            "total_source_items": len(candidate_items),
            "limits": {
                "source_items": limit,
                "documents": len(documents),
                "passage_windows": min(len(passage_windows), max_passage_windows),
                "total_passage_windows": len(passage_windows),
                "chunks": max_chunks,
                "existing_claims": max_existing_claims,
                "existing_digest_notes": max_existing_digest_notes,
                "source_chars": max_source_chars,
                "document_chars": max_document_chars,
                "passage_chars": max_passage_chars,
                "chunk_chars": max_chunk_chars,
            },
            "context_policy": {
                "input_strategy": "document_first",
                "passage_window_policy": "full_document_until_budget_then_paragraph_window",
                "chunks_role": "retrieval_slices_compatibility",
                "candidate_write_guidance": (
                    "Inspect existing knowledge_claims and digest_notes before writing. "
                    "Use stable dedupe_key values for the same semantic claim or digest across retries; "
                    "write low-confidence or conflict-prone items as review_items."
                ),
                "target_window_chars": max_passage_chars,
                "token_estimate": sum(window["token_estimate"] for window in passage_windows),
            },
        }

    def result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def write(self, response: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _compact_search_response(payload: dict[str, Any], *, max_results: int, max_snippet_chars: int) -> dict[str, Any]:
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    graph_paths = payload.get("graph_paths") if isinstance(payload.get("graph_paths"), list) else []
    return {
        "query": payload.get("query"),
        "request_user_id": payload.get("request_user_id"),
        "visible_spaces": payload.get("visible_spaces") if isinstance(payload.get("visible_spaces"), list) else [],
        "results": [_compact_search_result(item, max_snippet_chars=max_snippet_chars) for item in results[:max_results] if isinstance(item, dict)],
        "citations": [_compact_citation(item, max_snippet_chars=max_snippet_chars) for item in citations[:max_results] if isinstance(item, dict)],
        "graph_paths": [_compact_graph_path(item, max_snippet_chars=max_snippet_chars) for item in graph_paths[:5] if isinstance(item, dict)],
        "diagnostics": _compact_diagnostics(payload.get("diagnostics")),
        "omitted": {
            "results": max(0, len(results) - max_results),
            "citations": max(0, len(citations) - max_results),
            "graph_paths": max(0, len(graph_paths) - 5),
            "reason": "MCP compact output keeps FastReAct tool results parser-safe.",
        },
    }


def _context_from_mcp_params(params: dict[str, Any]) -> RequestContext | None:
    user_key = _clean_string(params.get("user_key") or params.get("user_id"))
    tenant_key = _clean_string(params.get("tenant_key") or params.get("tenant_id"))
    represented_user_id = _clean_string(params.get("represented_user_id"))
    if not user_key and not tenant_key and not represented_user_id:
        return None
    user_id = _pska_user_id_from_key(user_key or "user_primary")
    return RequestContext(
        tenant_id=tenant_key or DEFAULT_TENANT_ID,
        user_id=user_id,
        represented_user_id=_pska_user_id_from_key(represented_user_id) if represented_user_id else None,
        caller="user",
        subject=user_key or user_id,
        auth_provider="mcp_params",
    )


def _assert_context_matches(authenticated: RequestContext, mcp_context: RequestContext) -> None:
    if authenticated.tenant_id != mcp_context.tenant_id or authenticated.user_id != mcp_context.user_id:
        raise PermissionError("MCP identity params do not match authenticated context")


def _apply_mcp_context(arguments: dict[str, Any], context: RequestContext) -> dict[str, Any]:
    merged = dict(arguments)
    merged["tenant_id"] = context.tenant_id
    merged["user_id"] = context.user_id
    if context.represented_user_id:
        merged["represented_user_id"] = context.represented_user_id
    if isinstance(merged.get("payload"), dict):
        payload = dict(merged["payload"])
        payload["tenant_id"] = context.tenant_id
        payload["owner_user_id"] = context.represented_user_id or context.user_id
        merged["payload"] = payload
    if "owner_user_id" in merged or context.user_id:
        merged["owner_user_id"] = context.represented_user_id or context.user_id
    return merged


def _pska_user_id_from_key(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("pska:") else value


def _clean_string(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


def _compact_search_result(item: dict[str, Any], *, max_snippet_chars: int) -> dict[str, Any]:
    return {
        "result_id": item.get("result_id"),
        "source_item_id": item.get("source_item_id"),
        "source": item.get("source"),
        "title": item.get("title"),
        "snippet": _truncate(str(item.get("snippet") or ""), max_snippet_chars),
        "score": item.get("score"),
        "citation": _compact_citation(item.get("citation"), max_snippet_chars=max_snippet_chars),
    }


def _compact_citation(item: Any, *, max_snippet_chars: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    compact = {
        key: item.get(key)
        for key in ["source_item_id", "document_id", "chunk_id", "passage_window_id", "url", "title"]
        if item.get(key) is not None
    }
    if item.get("snippet"):
        compact["snippet"] = _truncate(str(item.get("snippet") or ""), max_snippet_chars)
    return compact


def _compact_graph_path(item: dict[str, Any], *, max_snippet_chars: int) -> dict[str, Any]:
    edges = item.get("edges") if isinstance(item.get("edges"), list) else []
    entities = item.get("entities") if isinstance(item.get("entities"), list) else []
    return {
        "path_id": item.get("path_id"),
        "depth": item.get("depth"),
        "seed": item.get("seed"),
        "entities": entities[:6],
        "edges": [_compact_graph_edge(edge, max_snippet_chars=max_snippet_chars) for edge in edges[:4] if isinstance(edge, dict)],
    }


def _compact_graph_edge(edge: dict[str, Any], *, max_snippet_chars: int) -> dict[str, Any]:
    return {
        "hyperedge_id": edge.get("hyperedge_id"),
        "relation_type": edge.get("relation_type"),
        "confidence": edge.get("confidence"),
        "evidence_text": _truncate(str(edge.get("evidence_text") or ""), max_snippet_chars),
        "source_refs": edge.get("source_refs") if isinstance(edge.get("source_refs"), list) else [],
    }


def _compact_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "gaps": value.get("gaps") if isinstance(value.get("gaps"), list) else [],
        "conflicts": value.get("conflicts") if isinstance(value.get("conflicts"), list) else [],
        "sensitivity": value.get("sensitivity") if isinstance(value.get("sensitivity"), list) else [],
    }


def _job_source_item_ids(job) -> set[str]:
    ids = {ref.source_item_id for ref in job.source_refs if ref.source_item_id}
    payload = job.payload if isinstance(job.payload, dict) else {}
    raw_ids = payload.get("source_item_ids")
    if isinstance(raw_ids, list):
        ids.update(str(item) for item in raw_ids if item)
    scope = payload.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("source_item_ids"), list):
        ids.update(str(item) for item in scope["source_item_ids"] if item)
    raw_refs = payload.get("source_refs")
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            if isinstance(ref, dict) and ref.get("source_item_id"):
                ids.add(str(ref["source_item_id"]))
    return ids


def _tenant_id_for_job(store: Any, job_id: Any) -> str | None:
    if not job_id:
        return None
    try:
        return str(store.get_job(str(job_id)).tenant_id)
    except Exception:
        return None


def _cursor_offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _compact_source_item(item: Any, *, max_chars: int) -> dict[str, Any]:
    payload = to_jsonable(item)
    text = str(payload.get("content_text") or "")
    raw_paths = payload.get("metadata", {}).get("raw_paths", {}) if isinstance(payload.get("metadata"), dict) else {}
    return {
        "source_item_id": payload.get("source_item_id"),
        "source_channel": payload.get("source_channel"),
        "record_type": payload.get("record_type"),
        "source_id": payload.get("source_id"),
        "owner_user_id": payload.get("owner_user_id"),
        "space_id": payload.get("space_id"),
        "visibility": payload.get("visibility"),
        "title": payload.get("title"),
        "url": payload.get("url"),
        "path": raw_paths.get("markdown") or raw_paths.get("original"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "content_text": _truncate(text, max_chars),
        "content_chars": len(text),
    }


def _compact_chunk(chunk: Any, *, max_chars: int) -> dict[str, Any]:
    payload = to_jsonable(chunk)
    text = str(payload.get("text") or "")
    return {
        "chunk_id": payload.get("chunk_id"),
        "document_id": payload.get("document_id"),
        "source_item_id": payload.get("source_item_id"),
        "owner_user_id": payload.get("owner_user_id"),
        "space_id": payload.get("space_id"),
        "visibility": payload.get("visibility"),
        "ordinal": payload.get("ordinal"),
        "text": _truncate(text, max_chars),
        "text_chars": len(text),
    }


def _compact_document(document: Any, *, max_chars: int) -> dict[str, Any]:
    payload = to_jsonable(document)
    body = str(payload.get("body") or "")
    return {
        "document_id": payload.get("document_id"),
        "source_item_id": payload.get("source_item_id"),
        "owner_user_id": payload.get("owner_user_id"),
        "space_id": payload.get("space_id"),
        "visibility": payload.get("visibility"),
        "title": payload.get("title"),
        "body": _truncate(body, max_chars),
        "body_chars": len(body),
        "token_estimate": _estimate_tokens(body),
        "metadata": payload.get("metadata") or {},
    }


def _passage_windows_for_documents(documents: list[Any], chunks: list[Any], *, target_chars: int = 6000) -> list[dict[str, Any]]:
    chunks_by_document: dict[str, list[Any]] = {}
    for chunk in chunks:
        chunks_by_document.setdefault(str(getattr(chunk, "document_id", "") or ""), []).append(chunk)
    windows: list[dict[str, Any]] = []
    max_chars = max(500, target_chars)
    for document in documents:
        document_id = str(getattr(document, "document_id", "") or "")
        body = str(getattr(document, "body", "") or "")
        if not body:
            body = "\n\n".join(str(getattr(chunk, "text", "") or "") for chunk in chunks_by_document.get(document_id, []))
        for ordinal, (start, end) in enumerate(_passage_spans(body, max_chars=max_chars)):
            text = body[start:end]
            windows.append(
                {
                    "passage_window_id": f"pw_{document_id}_{ordinal}",
                    "source_item_id": str(getattr(document, "source_item_id", "") or ""),
                    "document_id": document_id,
                    "owner_user_id": str(getattr(document, "owner_user_id", "") or ""),
                    "ordinal": ordinal,
                    "title": str(getattr(document, "title", "") or document_id),
                    "text": text,
                    "start_char": start,
                    "end_char": end,
                    "token_estimate": _estimate_tokens(text),
                    "metadata": {
                        "windowing_policy": "document_full" if len(body) <= max_chars else "paragraph_window",
                        "document_title": getattr(document, "title", "") or document_id,
                    },
                }
            )
    return windows


def _compact_passage_window(window: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    text = str(window.get("text") or "")
    return {**window, "text": _truncate(text, max_chars), "text_chars": len(text)}


def _passage_spans(text: str, *, max_chars: int) -> list[tuple[int, int]]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [(0, len(text))]
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        split_at = text.rfind("\n\n", start, end)
        if split_at <= start + max_chars // 2:
            split_at = end
        spans.append((start, split_at))
        start = split_at
        while start < len(text) and text[start].isspace():
            start += 1
    return spans


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _compact_job(job: Any) -> dict[str, Any]:
    payload = to_jsonable(job)
    return {
        "job_id": payload.get("job_id"),
        "job_type": payload.get("job_type"),
        "status": payload.get("status"),
        "attempts": payload.get("attempts"),
        "max_attempts": payload.get("max_attempts"),
        "priority": payload.get("priority"),
        "run_after": payload.get("run_after"),
        "tenant_id": payload.get("tenant_id") or (payload.get("payload") or {}).get("tenant_id"),
        "owner_user_id": (payload.get("payload") or {}).get("owner_user_id"),
        "reason": (payload.get("payload") or {}).get("reason"),
        "source_item_ids": sorted(_job_source_item_ids(job)),
    }


def _compact_entity(entity: Any) -> dict[str, Any]:
    payload = to_jsonable(entity)
    return {
        "entity_id": payload.get("entity_id"),
        "entity_type": payload.get("entity_type"),
        "label": payload.get("label"),
        "visibility": payload.get("visibility"),
    }


def _compact_memory(memory: Any) -> dict[str, Any]:
    payload = to_jsonable(memory)
    text = str(payload.get("text") or "")
    return {
        "agent_memory_id": payload.get("agent_memory_id"),
        "layer": payload.get("layer"),
        "text": _truncate(text, 240),
        "confidence": payload.get("confidence"),
    }


def _compact_knowledge_claim(claim: Any) -> dict[str, Any]:
    payload = to_jsonable(claim)
    return {
        "knowledge_claim_id": payload.get("knowledge_claim_id"),
        "claim_type": payload.get("claim_type"),
        "statement": _truncate(str(payload.get("statement") or ""), 360),
        "subject": payload.get("subject"),
        "predicate": payload.get("predicate"),
        "object": payload.get("object"),
        "evidence_text": _truncate(str(payload.get("evidence_text") or ""), 300),
        "source_refs": payload.get("source_refs") or [],
        "confidence": payload.get("confidence"),
        "producer": payload.get("producer"),
        "job_id": payload.get("job_id"),
    }


def _compact_digest_note(note: Any) -> dict[str, Any]:
    payload = to_jsonable(note)
    return {
        "digest_note_id": payload.get("digest_note_id"),
        "title": _truncate(str(payload.get("title") or ""), 160),
        "synopsis": _truncate(str(payload.get("synopsis") or ""), 700),
        "key_points": _compact_digest_list(payload.get("key_points"), max_items=5),
        "actions": _compact_digest_list(payload.get("actions"), max_items=4),
        "open_questions": _compact_digest_list(payload.get("open_questions"), max_items=3),
        "risks": _compact_digest_list(payload.get("risks"), max_items=3),
        "source_refs": payload.get("source_refs") or [],
        "confidence": payload.get("confidence"),
        "producer": payload.get("producer"),
        "job_id": payload.get("job_id"),
    }


def _compact_digest_list(value: Any, *, max_items: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compacted: list[dict[str, Any]] = []
    for item in value[:max_items]:
        if not isinstance(item, dict):
            compacted.append({"summary": _truncate(str(item), 220)})
            continue
        summary = item.get("summary") or item.get("point") or item.get("action") or item.get("question") or item.get("risk") or item.get("text")
        compacted.append(
            {
                "summary": _truncate(str(summary or ""), 220),
                "source_refs": item.get("source_refs") or [],
            }
        )
    return compacted


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 24)].rstrip() + "\n...[truncated]"


if __name__ == "__main__":
    raise SystemExit(main())
