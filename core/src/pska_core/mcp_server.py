from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from pska_core.acl import ACLService
from pska_core.agentic_service import AgenticServiceClient, build_agentic_service_client
from pska_core.auth import RequestContext
from pska_core.candidates import CandidateWriteService
from pska_core.config import DatabaseConfig, PSKAConfig
from pska_core.embeddings import EmbeddingProvider, build_embedding_provider
from pska_core.evidence_composition import EvidenceCompositionContext, EvidenceCompositionPipeline, evidence_set_to_dict
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.models import DEFAULT_TENANT_ID, ChannelIngestPayload, SourceRef
from pska_core.retrieval import RetrievalService, query_focused_evidence_snippet
from pska_core.serde import to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


PROTOCOL_VERSION = "2024-11-05"


ASK_READ_TOOL_NAMES = {
    "pska_search",
    "pska_index_status",
    "pska_read_evidence_context",
    "pska_graph_context",
    "pska_digest_context",
}


TOOLS = [
    {
        "name": "pska_search",
        "description": "Search the PSKA knowledge base with ACL filtering and citations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
                "max_results": {"type": "integer", "default": 5},
                "max_snippet_chars": {"type": "integer", "default": 700},
                "knowledge_base_ids": {"type": "array", "items": {"type": "string"}},
                "source_item_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "object"},
                "scope_mode": {"type": "string", "enum": ["soft", "hard"], "default": "soft"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pska_index_status",
        "description": "Return basic PSKA index counts for the current tenant/user scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "knowledge_base_ids": {"type": "array", "items": {"type": "string"}},
                "source_item_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "object"},
                "scope_mode": {"type": "string", "enum": ["soft", "hard"], "default": "soft"},
            },
            "required": [],
        },
    },
    {
        "name": "pska_read_evidence_context",
        "description": "Read fuller source, document, chunk, or citation context within the current tenant/user scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source_refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_item_id": {"type": "string"},
                            "document_id": {"type": "string"},
                            "passage_window_id": {"type": "string"},
                            "chunk_id": {"type": "string"},
                            "url": {"type": "string"},
                            "path": {"type": "string"},
                        },
                    },
                },
                "source_item_ids": {"type": "array", "items": {"type": "string"}},
                "knowledge_base_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "object"},
                "document_ids": {"type": "array", "items": {"type": "string"}},
                "chunk_ids": {"type": "array", "items": {"type": "string"}},
                "max_items": {"type": "integer", "default": 5},
                "max_source_chars": {"type": "integer", "default": 1200},
                "max_document_chars": {"type": "integer", "default": 2400},
                "max_passage_chars": {"type": "integer", "default": 1600},
                "max_chunk_chars": {"type": "integer", "default": 1200},
            },
            "required": [],
        },
    },
    {
        "name": "pska_graph_context",
        "description": "Expand graph neighbors, paths, and claim evidence for entities or source refs in the current tenant/user scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"}},
                "entity_labels": {"type": "array", "items": {"type": "string"}},
                "source_item_ids": {"type": "array", "items": {"type": "string"}},
                "knowledge_base_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "object"},
                "max_depth": {"type": "integer", "default": 2},
                "max_paths": {"type": "integer", "default": 6},
                "max_edges": {"type": "integer", "default": 8},
                "max_snippet_chars": {"type": "integer", "default": 700},
            },
            "required": [],
        },
    },
    {
        "name": "pska_digest_context",
        "description": "Read digest notes, knowledge claims, risks, and open questions in the current tenant/user scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source_item_ids": {"type": "array", "items": {"type": "string"}},
                "knowledge_base_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "object"},
                "job_id": {"type": "string"},
                "max_claims": {"type": "integer", "default": 8},
                "max_digest_notes": {"type": "integer", "default": 5},
                "max_snippet_chars": {"type": "integer", "default": 700},
            },
            "required": [],
        },
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
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "pska_review_items",
        "description": "List PSKA review items.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "pska_write_candidates",
        "description": "Write Fastreact-generated PSKA candidates with required source refs.",
        "inputSchema": {
            "type": "object",
            "properties": {
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
    def __init__(
        self,
        database_url: str,
        store: Any | None = None,
        llm: Any | None = None,
        agentic_service: AgenticServiceClient | None = None,
        config: PSKAConfig | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        if config is None:
            config = PSKAConfig(database=DatabaseConfig(url=database_url))
        self.config = config
        self.store = store or PostgresKnowledgeStore(database_url)
        if embedding_provider is None:
            embedding_provider = build_embedding_provider(config.embedding_runtime_config())
        self.retrieval = RetrievalService(self.store, ACLService(self.store), embedding_provider=embedding_provider)
        self.ingest = IngestService(self.store, embedding_provider=embedding_provider, **config.ingest_kwargs())
        self.extraction = ExtractionService(
            self.store,
            llm=llm,
            agentic_service=agentic_service or build_agentic_service_client(config.agentic_service_runtime_config()),
        )
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
            mcp_context = _context_from_mcp_params(params, include_arguments=context is None)
            _emit_mcp_identity_log(
                "pska.mcp_tools_call_received",
                tool_name=params.get("name"),
                params=params,
                authenticated_context=context,
                mcp_context=mcp_context,
                arguments=params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
            )
            if context and mcp_context and not context.service_authenticated:
                _assert_context_matches(context, mcp_context)
            return self.result(
                request_id,
                self.call_tool(params.get("name"), params.get("arguments") or {}, context=mcp_context or context),
            )
        return self.error(request_id, -32601, f"Unknown method: {method}")

    def call_tool(self, name: str, arguments: dict[str, Any], *, context: RequestContext | None = None) -> dict[str, Any]:
        raw_arguments = dict(arguments or {})
        if context and name == "pska_job_context" and not _mcp_arguments_have_tenant(raw_arguments):
            job_tenant_id = _tenant_id_for_job(self.store, raw_arguments.get("job_id"))
            if job_tenant_id:
                context = replace(context, tenant_id=job_tenant_id)
        arguments = _apply_mcp_context(raw_arguments, context) if context else raw_arguments
        _emit_mcp_identity_log(
            "pska.mcp_tool_context_applied",
            tool_name=name,
            params={},
            authenticated_context=context,
            mcp_context=context,
            arguments=arguments,
            raw_arguments=raw_arguments,
        )
        if name == "pska_search":
            payload = self.pska_search(arguments)
        elif name == "pska_index_status":
            payload = self.pska_index_status(arguments)
        elif name == "pska_read_evidence_context":
            payload = self.pska_read_evidence_context(arguments)
        elif name == "pska_graph_context":
            payload = self.pska_graph_context(arguments)
        elif name == "pska_digest_context":
            payload = self.pska_digest_context(arguments)
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
        tenant_id, user, represented_user_id = self._request_scope(arguments)
        owner_user_id = represented_user_id or user.user_id
        source_item_ids = set(_string_list(arguments.get("source_item_ids")))
        scope_mode = str(arguments.get("scope_mode") or "soft").strip().lower()
        kb_source_item_ids, knowledge_base_ids = self._knowledge_base_scope_source_ids(
            arguments,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        if knowledge_base_ids:
            source_item_ids = source_item_ids & kb_source_item_ids if source_item_ids else set(kb_source_item_ids)
            scope_mode = "hard"
        response = self.retrieval.search(
            arguments["query"],
            user,
            represented_user_id=represented_user_id,
            top_k=int(arguments.get("top_k") or 5),
            source_item_ids=source_item_ids if knowledge_base_ids else source_item_ids or None,
            scope_mode="hard" if scope_mode == "hard" else "soft",
        )
        payload = _compact_search_response(
            to_jsonable(response),
            max_results=_bounded_int(arguments.get("max_results"), default=5, minimum=1, maximum=12),
            max_snippet_chars=_bounded_int(arguments.get("max_snippet_chars"), default=700, minimum=120, maximum=1600),
        )
        if knowledge_base_ids:
            payload["scope_applied"] = {
                "mode": "hard",
                "scope_mode": "hard",
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_ids": sorted(source_item_ids),
            }
        return payload

    def pska_index_status(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = dict(arguments or {})
        tenant_id, user, represented_user_id = self._request_scope(arguments)
        owner_user_id = represented_user_id or user.user_id
        requested_source_ids = set(_source_item_ids_from_mcp_arguments(arguments))
        kb_source_item_ids, knowledge_base_ids = self._knowledge_base_scope_source_ids(
            arguments,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        visible_items = self._visible_source_items(user, represented_user_id=represented_user_id)
        if knowledge_base_ids:
            requested_source_ids = requested_source_ids & kb_source_item_ids if requested_source_ids else set(kb_source_item_ids)
        if requested_source_ids:
            visible_items = [item for item in visible_items if item.source_item_id in requested_source_ids]
        source_ids = {item.source_item_id for item in visible_items}
        source_scope_constrained = bool(knowledge_base_ids or requested_source_ids)
        if source_scope_constrained and not source_ids:
            knowledge_claim_count = 0
            digest_note_count = 0
        else:
            knowledge_claim_count = len(
                self.store.list_knowledge_claims(
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    source_item_ids=source_ids if source_scope_constrained else None,
                    limit=10_000,
                )
            )
            digest_note_count = len(
                self.store.list_digest_notes(
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    source_item_ids=source_ids if source_scope_constrained else None,
                    limit=10_000,
                )
            )
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "request_user_id": owner_user_id,
            "source_items": len(visible_items),
            "user_source_items": len([item for item in visible_items if item.owner_user_id == owner_user_id]),
            "documents": len(self.store.list_documents_for_sources(source_ids)),
            "chunks": len(self.store.list_chunks_for_sources(source_ids)),
            "entities": len(self.store.list_entities(tenant_id=tenant_id)),
            "hyperedges": len(self.store.list_hyperedges_for_entities({entity.entity_id for entity in self.store.list_entities(tenant_id=tenant_id)})),
            "knowledge_claims": knowledge_claim_count,
            "digest_notes": digest_note_count,
            "review_items": len(self.store.list_review_items(tenant_id=tenant_id)),
            "scope_applied": {
                "mode": "hard",
                "scope_mode": "hard",
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_ids": sorted(kb_source_item_ids),
            } if knowledge_base_ids else {},
        }

    def pska_read_evidence_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id, user, represented_user_id = self._request_scope(arguments)
        max_items = _bounded_int(arguments.get("max_items"), default=5, minimum=1, maximum=12)
        max_source_chars = _bounded_int(arguments.get("max_source_chars"), default=1200, minimum=240, maximum=8000)
        max_document_chars = _bounded_int(arguments.get("max_document_chars"), default=2400, minimum=500, maximum=12000)
        max_passage_chars = _bounded_int(arguments.get("max_passage_chars"), default=1600, minimum=500, maximum=8000)
        max_chunk_chars = _bounded_int(arguments.get("max_chunk_chars"), default=1200, minimum=240, maximum=6000)
        query = _clean_string(arguments.get("query"))
        refs = _source_refs_from_mcp_arguments(arguments)
        requested_source_ids = set(_string_list(arguments.get("source_item_ids")))
        requested_document_ids = set(_string_list(arguments.get("document_ids")))
        requested_chunk_ids = set(_string_list(arguments.get("chunk_ids")))
        kb_source_item_ids, knowledge_base_ids = self._knowledge_base_scope_source_ids(
            arguments,
            tenant_id=tenant_id,
            owner_user_id=represented_user_id or user.user_id,
        )
        if knowledge_base_ids:
            requested_source_ids = requested_source_ids & kb_source_item_ids if requested_source_ids else set(kb_source_item_ids)
        for ref in refs:
            if ref.source_item_id:
                requested_source_ids.add(ref.source_item_id)
            if ref.document_id:
                requested_document_ids.add(ref.document_id)
            if ref.chunk_id:
                requested_chunk_ids.add(ref.chunk_id)
        if knowledge_base_ids:
            requested_source_ids = requested_source_ids & kb_source_item_ids
        visible_items = self._visible_source_items(user, represented_user_id=represented_user_id)
        visible_by_id = {item.source_item_id: item for item in visible_items}
        if not requested_source_ids and not requested_document_ids and not requested_chunk_ids and query:
            search = self.retrieval.search(
                query,
                user,
                represented_user_id=represented_user_id,
                top_k=max_items,
                source_item_ids=kb_source_item_ids if knowledge_base_ids else None,
                scope_mode="hard" if knowledge_base_ids else "soft",
            )
            requested_source_ids.update(result.source_item_id for result in search.results[:max_items])
        if knowledge_base_ids or requested_source_ids:
            visible_items = [item for item in visible_items if item.source_item_id in requested_source_ids]
        source_ids = {item.source_item_id for item in visible_items}
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        if requested_document_ids:
            documents = [document for document in documents if document.document_id in requested_document_ids]
            document_source_ids = {document.source_item_id for document in documents}
            source_ids = source_ids.intersection(document_source_ids) if requested_source_ids else document_source_ids
            chunks = [chunk for chunk in chunks if chunk.document_id in requested_document_ids or chunk.source_item_id in document_source_ids]
        if requested_chunk_ids:
            chunks = [chunk for chunk in chunks if chunk.chunk_id in requested_chunk_ids]
            chunk_document_ids = {chunk.document_id for chunk in chunks}
            chunk_source_ids = {chunk.source_item_id for chunk in chunks}
            if not requested_document_ids:
                documents = [document for document in documents if document.document_id in chunk_document_ids or document.source_item_id in chunk_source_ids]
            source_ids = source_ids.intersection(chunk_source_ids) if source_ids else chunk_source_ids
        if query and not requested_document_ids and not requested_chunk_ids:
            source_ids = {item.source_item_id for item in _rank_items_by_query(visible_items, query)[:max_items]}
            documents = [document for document in documents if document.source_item_id in source_ids]
            chunks = [chunk for chunk in chunks if chunk.source_item_id in source_ids]
        selected_items = [visible_by_id[source_id] for source_id in sorted(source_ids) if source_id in visible_by_id][:max_items]
        selected_documents = _rank_objects_by_query(documents, query, fields=("title", "body"))[: max_items * 2] if query else documents[: max_items * 2]
        selected_chunks = _rank_objects_by_query(chunks, query, fields=("chunk_id", "text"))[: max_items * 4] if query else chunks[: max_items * 4]
        passage_windows = _passage_windows_for_documents(selected_documents, selected_chunks, target_chars=max_passage_chars)
        results = _evidence_results_from_context(selected_items, selected_documents, selected_chunks, max_snippet_chars=max_chunk_chars, query=query)
        citations = _citations_for_source_items(selected_items, chunks=selected_chunks, max_snippet_chars=max_chunk_chars, query=query)
        payload = {
            "ok": True,
            "tenant_id": tenant_id,
            "request_user_id": represented_user_id or user.user_id,
            "scope_applied": {
                "mode": "hard",
                "scope_mode": "hard",
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_ids": sorted(kb_source_item_ids),
            } if knowledge_base_ids else {},
            "query": query or None,
            "results": results[: max_items * 2],
            "citations": citations[: max_items * 2],
            "source_items": [_compact_source_item(item, max_chars=max_source_chars) for item in selected_items],
            "documents": [_compact_document(document, max_chars=max_document_chars, query=query) for document in selected_documents],
            "passage_windows": [_compact_passage_window(window, max_chars=max_passage_chars, query=query) for window in passage_windows[: max_items * 2]],
            "chunks": [_compact_chunk(chunk, max_chars=max_chunk_chars, query=query) for chunk in selected_chunks],
            "graph_paths": [],
            "diagnostics": {"gaps": [], "conflicts": [], "sensitivity": []},
            "omitted": {
                "source_items": max(0, len(visible_items) - len(selected_items)),
                "documents": max(0, len(documents) - len(selected_documents)),
                "chunks": max(0, len(chunks) - len(selected_chunks)),
            },
        }
        return _compact_evidence_context_response(
            payload,
            max_items=max_items,
            max_source_chars=max_source_chars,
            max_document_chars=max_document_chars,
            max_passage_chars=max_passage_chars,
            max_chunk_chars=max_chunk_chars,
        )

    def pska_graph_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id, user, represented_user_id = self._request_scope(arguments)
        query = _clean_string(arguments.get("query"))
        max_depth = _bounded_int(arguments.get("max_depth"), default=2, minimum=1, maximum=3)
        max_paths = _bounded_int(arguments.get("max_paths"), default=6, minimum=1, maximum=12)
        max_edges = _bounded_int(arguments.get("max_edges"), default=8, minimum=1, maximum=24)
        max_snippet_chars = _bounded_int(arguments.get("max_snippet_chars"), default=700, minimum=120, maximum=2000)
        entity_ids = set(_string_list(arguments.get("entity_ids")))
        entity_labels = [label.casefold() for label in _string_list(arguments.get("entity_labels"))]
        source_ids = set(_string_list(arguments.get("source_item_ids")))
        kb_source_item_ids, knowledge_base_ids = self._knowledge_base_scope_source_ids(
            arguments,
            tenant_id=tenant_id,
            owner_user_id=represented_user_id or user.user_id,
        )
        if knowledge_base_ids:
            source_ids = source_ids & kb_source_item_ids if source_ids else set(kb_source_item_ids)
        all_entities = self.store.list_entities(tenant_id=tenant_id)
        visible_entities = self.retrieval._visible_entities(all_entities, user=user, represented_user_id=represented_user_id)
        selected_entities = []
        if entity_ids or entity_labels:
            selected_entities = [
                entity
                for entity in visible_entities
                if entity.entity_id in entity_ids or any(label in entity.label.casefold() for label in entity_labels)
            ]
        elif query:
            search = self.retrieval.search(
                query,
                user,
                represented_user_id=represented_user_id,
                top_k=max_paths,
                source_item_ids=source_ids if knowledge_base_ids else source_ids or None,
                scope_mode="hard" if knowledge_base_ids or source_ids else "soft",
            )
            ranked = search.results
            source_ids.update(result.source_item_id for result in ranked[:max_paths])
            selected_entities = self.retrieval._matching_entities(query, ranked, tenant_id=tenant_id)
            selected_entities = self.retrieval._visible_entities(selected_entities, user=user, represented_user_id=represented_user_id)
        if source_ids and not selected_entities:
            edge_tuples = self.store.list_hyperedges_for_entities({entity.entity_id for entity in visible_entities})
            selected_entity_ids: set[str] = set()
            for edge, members in edge_tuples:
                if not self.retrieval._can_read_graph_object(user, edge.owner_user_id, edge.visibility, edge.visible_team_ids, represented_user_id):
                    continue
                if any(ref.source_item_id in source_ids for ref in edge.source_refs):
                    selected_entity_ids.update(member.entity_id for member in members)
            selected_entities = [entity for entity in visible_entities if entity.entity_id in selected_entity_ids]
        ranked_for_paths = [] if knowledge_base_ids and not source_ids else _retrieval_results_from_sources(query, self._visible_source_items(user, represented_user_id=represented_user_id), source_ids)
        graph_paths = self.retrieval._graph_paths(
            query=query or " ".join(entity.label for entity in selected_entities[:3]) or "graph context",
            ranked=ranked_for_paths,
            user=user,
            represented_user_id=represented_user_id,
            max_depth=max_depth,
            max_paths=max_paths,
        )
        entity_by_id = {entity.entity_id: entity for entity in all_entities}
        edge_contexts: list[dict[str, Any]] = []
        for edge, members in self.store.list_hyperedges_for_entities({entity.entity_id for entity in selected_entities}):
            if not self.retrieval._can_read_graph_object(user, edge.owner_user_id, edge.visibility, edge.visible_team_ids, represented_user_id):
                continue
            if knowledge_base_ids and not any(ref.source_item_id in source_ids for ref in edge.source_refs if ref.source_item_id):
                continue
            edge_contexts.append(self.retrieval._edge_context(edge, members, entity_by_id, user=user, represented_user_id=represented_user_id))
            if len(edge_contexts) >= max_edges:
                break
        source_refs = _source_refs_from_graph_context(edge_contexts, graph_paths)
        citations = self.retrieval._source_ref_citations(source_refs, user=user, represented_user_id=represented_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "request_user_id": represented_user_id or user.user_id,
            "scope_applied": {
                "mode": "hard",
                "scope_mode": "hard",
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_ids": sorted(kb_source_item_ids),
            } if knowledge_base_ids else {},
            "query": query or None,
            "entities": [_compact_entity(entity) for entity in selected_entities[:max_edges]],
            "edges": [_compact_graph_edge(edge, max_snippet_chars=max_snippet_chars) for edge in edge_contexts],
            "graph_paths": [_compact_graph_path(path, max_snippet_chars=max_snippet_chars) for path in graph_paths],
            "results": _results_from_graph_edges(edge_contexts, max_snippet_chars=max_snippet_chars),
            "citations": [_compact_citation(citation, max_snippet_chars=max_snippet_chars) for citation in citations[: max_edges * 2]],
            "diagnostics": {"gaps": [], "conflicts": [], "sensitivity": []},
            "omitted": {
                "entities": max(0, len(selected_entities) - max_edges),
                "edges": max(0, len(edge_contexts) - max_edges),
                "graph_paths": max(0, len(graph_paths) - max_paths),
            },
        }

    def pska_digest_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tenant_id, user, represented_user_id = self._request_scope(arguments)
        owner_user_id = represented_user_id or user.user_id
        query = _clean_string(arguments.get("query"))
        source_ids = set(_string_list(arguments.get("source_item_ids")))
        kb_source_item_ids, knowledge_base_ids = self._knowledge_base_scope_source_ids(
            arguments,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        if knowledge_base_ids:
            source_ids = source_ids & kb_source_item_ids if source_ids else set(kb_source_item_ids)
        max_claims = _bounded_int(arguments.get("max_claims"), default=8, minimum=1, maximum=24)
        max_digest_notes = _bounded_int(arguments.get("max_digest_notes"), default=5, minimum=1, maximum=16)
        max_snippet_chars = _bounded_int(arguments.get("max_snippet_chars"), default=700, minimum=120, maximum=2000)
        job_id = _clean_string(arguments.get("job_id")) or None
        if knowledge_base_ids and not source_ids:
            claims = []
            notes = []
        else:
            claims = self.store.list_knowledge_claims(
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                source_item_ids=source_ids if knowledge_base_ids else source_ids or None,
                job_id=job_id,
                limit=max_claims * 3,
            )
            notes = self.store.list_digest_notes(
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                source_item_ids=source_ids if knowledge_base_ids else source_ids or None,
                job_id=job_id,
                limit=max_digest_notes * 3,
            )
        if query:
            claims = _rank_objects_by_query(claims, query, fields=("statement", "evidence_text", "subject", "predicate", "object"))
            notes = _rank_objects_by_query(notes, query, fields=("title", "synopsis", "key_points", "actions", "open_questions", "risks"))
        claims = claims[:max_claims]
        notes = notes[:max_digest_notes]
        source_refs = [
            ref
            for obj in [*claims, *notes]
            for ref in getattr(obj, "source_refs", [])
            if isinstance(ref, SourceRef)
        ]
        citations = self.retrieval._source_ref_citations(source_refs, user=user, represented_user_id=represented_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "request_user_id": owner_user_id,
            "scope_applied": {
                "mode": "hard",
                "scope_mode": "hard",
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_ids": sorted(kb_source_item_ids),
            } if knowledge_base_ids else {},
            "query": query or None,
            "knowledge_claims": [_compact_knowledge_claim(claim) for claim in claims],
            "digest_notes": [_compact_digest_note(note) for note in notes],
            "results": _results_from_digest_context(claims, notes, max_snippet_chars=max_snippet_chars),
            "citations": [_compact_citation(citation, max_snippet_chars=max_snippet_chars) for citation in citations[: (max_claims + max_digest_notes)]],
            "graph_paths": [],
            "diagnostics": {"gaps": [], "conflicts": [], "sensitivity": []},
            "omitted": {"knowledge_claims": 0, "digest_notes": 0},
        }

    def _request_scope(self, arguments: dict[str, Any]) -> tuple[str, Any, str | None]:
        scope = arguments.get("scope") if isinstance(arguments.get("scope"), dict) else {}
        tenant_id = str(arguments.get("tenant_id") or arguments.get("tenant_key") or scope.get("tenant_id") or scope.get("tenant_key") or DEFAULT_TENANT_ID)
        legacy_user = arguments.get("represented_user_id") or arguments.get("owner_user_id") or scope.get("represented_user_id") or scope.get("owner_user_id")
        user_id = _pska_user_id_from_key(
            str(arguments.get("user_key") or arguments.get("user_id") or scope.get("user_key") or scope.get("user_id") or legacy_user or "user_primary")
        )
        if user_id == "agent_service" and legacy_user:
            user_id = _pska_user_id_from_key(str(legacy_user))
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        return tenant_id, user, None

    def _visible_source_items(self, user: Any, *, represented_user_id: str | None) -> list[Any]:
        return [
            item
            for item in self.store.list_source_items(tenant_id=user.tenant_id)
            if str(getattr(item, "lifecycle_status", "active") or "active") == "active"
            if self.retrieval.acl.can_read_item(user, item, represented_user_id=represented_user_id)
        ]

    def _knowledge_base_scope_source_ids(self, arguments: dict[str, Any], *, tenant_id: str, owner_user_id: str) -> tuple[set[str], list[str]]:
        knowledge_base_ids = _knowledge_base_ids_from_mcp_arguments(arguments)
        if not knowledge_base_ids:
            return set(), []
        fallback_source_item_ids: set[str] = set()
        requested_source_item_ids = set(_source_item_ids_from_mcp_arguments(arguments))
        for knowledge_base_id in knowledge_base_ids:
            try:
                self.store.get_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            except KeyError as exc:
                scoped_fallback_ids = self._hard_scoped_source_fallback_ids(
                    knowledge_base_id,
                    requested_source_item_ids=requested_source_item_ids,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                )
                if not scoped_fallback_ids:
                    raise PermissionError("knowledge base is not accessible") from exc
                fallback_source_item_ids.update(scoped_fallback_ids)
        source_item_ids = self.store.list_knowledge_base_source_item_ids(
            set(knowledge_base_ids),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        source_item_ids.update(fallback_source_item_ids)
        return source_item_ids, knowledge_base_ids

    def _hard_scoped_source_fallback_ids(
        self,
        knowledge_base_id: str,
        *,
        requested_source_item_ids: set[str],
        tenant_id: str,
        owner_user_id: str,
    ) -> set[str]:
        if not requested_source_item_ids:
            return set()
        kb_source_item_ids = self.store.list_knowledge_base_source_item_ids(
            {knowledge_base_id},
            tenant_id=tenant_id,
            owner_user_id=None,
        )
        if not kb_source_item_ids or not requested_source_item_ids.issubset(kb_source_item_ids):
            return set()
        source_items = {
            item.source_item_id: item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.source_item_id in requested_source_item_ids
        }
        if set(source_items) != requested_source_item_ids:
            return set()
        for item in source_items.values():
            if item.owner_user_id != owner_user_id:
                return set()
            if str(getattr(item, "lifecycle_status", "active") or "active") != "active":
                return set()
        return set(requested_source_item_ids)

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
            if item.source_item_id in source_item_ids
            and item.owner_user_id == request_user_id
            and str(getattr(item, "lifecycle_status", "active") or "active") == "active"
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
    budgets = [
        (max_results, max_snippet_chars),
        (min(max_results, 2), min(max_snippet_chars, 140)),
        (1, 120),
    ]
    response: dict[str, Any] = {}
    for result_limit, snippet_limit in budgets:
        candidate = _build_compact_search_response(
            payload,
            results=results,
            citations=citations,
            graph_paths=graph_paths,
            result_limit=result_limit,
            snippet_limit=snippet_limit,
        )
        candidate = _fit_mcp_payload_budget(candidate)
        response = candidate
        if len(json.dumps(candidate, ensure_ascii=False)) < 3800:
            break
    return response


def _compact_evidence_context_response(
    payload: dict[str, Any],
    *,
    max_items: int,
    max_source_chars: int,
    max_document_chars: int,
    max_passage_chars: int,
    max_chunk_chars: int,
) -> dict[str, Any]:
    budgets = [
        (
            max_items * 2,
            max_items * 2,
            max_items,
            max_items * 2,
            max_items * 4,
            max_source_chars,
            max_document_chars,
            max_passage_chars,
            max_chunk_chars,
        ),
        (
            min(max_items * 2, 4),
            min(max_items * 2, 4),
            min(max_items, 2),
            min(max_items * 2, 3),
            min(max_items * 4, 4),
            min(max_source_chars, 700),
            min(max_document_chars, 900),
            min(max_passage_chars, 900),
            min(max_chunk_chars, 700),
        ),
        (2, 2, 1, 1, 2, 360, 500, 500, 360),
        (1, 1, 1, 1, 1, 240, 300, 300, 240),
        (1, 1, 0, 0, 1, 0, 0, 0, 360),
    ]
    response: dict[str, Any] = {}
    for result_limit, citation_limit, source_limit, passage_limit, chunk_limit, source_chars, document_chars, passage_chars, chunk_chars in budgets:
        candidate = _build_compact_evidence_context_response(
            payload,
            result_limit=result_limit,
            citation_limit=citation_limit,
            source_limit=source_limit,
            passage_limit=passage_limit,
            chunk_limit=chunk_limit,
            source_chars=source_chars,
            document_chars=document_chars,
            passage_chars=passage_chars,
            chunk_chars=chunk_chars,
        )
        candidate = _fit_mcp_payload_budget(candidate)
        response = candidate
        if len(json.dumps(candidate, ensure_ascii=False)) < 3800:
            break
    return response


def _build_compact_evidence_context_response(
    payload: dict[str, Any],
    *,
    result_limit: int,
    citation_limit: int,
    source_limit: int,
    passage_limit: int,
    chunk_limit: int,
    source_chars: int,
    document_chars: int,
    passage_chars: int,
    chunk_chars: int,
) -> dict[str, Any]:
    query = _clean_string(payload.get("query"))
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    source_items = payload.get("source_items") if isinstance(payload.get("source_items"), list) else []
    documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []
    passage_windows = payload.get("passage_windows") if isinstance(payload.get("passage_windows"), list) else []
    chunks = payload.get("chunks") if isinstance(payload.get("chunks"), list) else []
    compact_results = [
        _compact_search_result(item, max_snippet_chars=chunk_chars)
        for item in results[:result_limit]
        if isinstance(item, dict)
    ]
    compact_citations = [
        _compact_citation(item, max_snippet_chars=chunk_chars)
        for item in citations[:citation_limit]
        if isinstance(item, dict)
    ]
    compact_source_items = [_compact_source_item(item, max_chars=source_chars) for item in source_items[:source_limit]]
    compact_documents = [_compact_document(item, max_chars=document_chars, query=query) for item in documents[:source_limit]]
    compact_passage_windows = [
        _compact_passage_window(item, max_chars=passage_chars, query=query)
        for item in passage_windows[:passage_limit]
        if isinstance(item, dict)
    ]
    compact_chunks = [_compact_chunk(item, max_chars=chunk_chars, query=query) for item in chunks[:chunk_limit]]
    evidence_set = _compact_mcp_evidence_set(
        query=query,
        citations=compact_citations,
        graph_paths=payload.get("graph_paths") if isinstance(payload.get("graph_paths"), list) else [],
        max_records=max(1, min(6, citation_limit or result_limit or chunk_limit or 1)),
    )
    follow_up_keys = _dedupe_follow_up_keys(
        key
        for item in [*compact_results, *compact_citations, *compact_chunks]
        for key in _extract_follow_up_keys(
            " ".join(
                str(item.get(field) or "")
                for field in ("source_item_id", "document_id", "chunk_id", "title", "snippet", "text")
            )
        )
    )
    omitted = dict(payload.get("omitted") if isinstance(payload.get("omitted"), dict) else {})
    omitted.update(
        {
            "results": max(0, len(results) - len(compact_results)),
            "citations": max(0, len(citations) - len(compact_citations)),
            "context_source_items": max(0, len(source_items) - len(compact_source_items)),
            "context_documents": max(0, len(documents) - len(compact_documents)),
            "passage_windows": max(0, len(passage_windows) - len(compact_passage_windows)),
            "context_chunks": max(0, len(chunks) - len(compact_chunks)),
            "reason": "MCP compact output keeps FastReAct tool results parser-safe.",
        }
    )
    return {
        "ok": payload.get("ok", True),
        "tenant_id": payload.get("tenant_id"),
        "request_user_id": payload.get("request_user_id"),
        "scope_applied": payload.get("scope_applied") if isinstance(payload.get("scope_applied"), dict) else {},
        "query": payload.get("query"),
        "results": compact_results,
        "citations": compact_citations,
        "source_items": compact_source_items,
        "documents": compact_documents,
        "passage_windows": compact_passage_windows,
        "chunks": compact_chunks,
        "evidence_set": evidence_set,
        "follow_up_keys": follow_up_keys[:12],
        "graph_paths": payload.get("graph_paths") if isinstance(payload.get("graph_paths"), list) else [],
        "diagnostics": _compact_diagnostics(payload.get("diagnostics")),
        "omitted": omitted,
    }


def _build_compact_search_response(
    payload: dict[str, Any],
    *,
    results: list[Any],
    citations: list[Any],
    graph_paths: list[Any],
    result_limit: int,
    snippet_limit: int,
) -> dict[str, Any]:
    compact_results = [_compact_search_result(item, max_snippet_chars=snippet_limit) for item in results[:result_limit] if isinstance(item, dict)]
    compact_citations = [_compact_citation(item, max_snippet_chars=snippet_limit) for item in citations[:result_limit] if isinstance(item, dict)]
    compact_graph_paths = [_compact_graph_path(item, max_snippet_chars=snippet_limit) for item in graph_paths[:5] if isinstance(item, dict)]
    evidence_set = _compact_mcp_evidence_set(
        query=_clean_string(payload.get("query")),
        citations=compact_citations,
        graph_paths=compact_graph_paths,
        max_records=max(1, min(6, result_limit or 1)),
    )
    follow_up_keys = _dedupe_follow_up_keys(
        key
        for item in [*compact_results, *compact_citations]
        for key in _extract_follow_up_keys(
            " ".join(
                str(item.get(field) or "")
                for field in ("source_item_id", "chunk_id", "title", "snippet")
            )
        )
    )
    return {
        "query": payload.get("query"),
        "request_user_id": payload.get("request_user_id"),
        "visible_spaces": payload.get("visible_spaces") if isinstance(payload.get("visible_spaces"), list) else [],
        "results": compact_results,
        "citations": compact_citations,
        "evidence_set": evidence_set,
        "follow_up_keys": follow_up_keys[:12],
        "graph_paths": compact_graph_paths,
        "diagnostics": _compact_diagnostics(payload.get("diagnostics")),
        "omitted": {
            "results": max(0, len(results) - len(compact_results)),
            "citations": max(0, len(citations) - len(compact_citations)),
            "graph_paths": max(0, len(graph_paths) - 5),
            "reason": "MCP compact output keeps FastReAct tool results parser-safe.",
        },
    }


def _context_from_mcp_params(params: dict[str, Any], *, include_arguments: bool = True) -> RequestContext | None:
    arguments = params.get("arguments") if include_arguments and isinstance(params.get("arguments"), dict) else {}
    scope = arguments.get("scope") if isinstance(arguments.get("scope"), dict) else {}
    legacy_user = _clean_string(
        params.get("represented_user_id")
        or params.get("owner_user_id")
        or arguments.get("represented_user_id")
        or arguments.get("owner_user_id")
        or scope.get("represented_user_id")
        or scope.get("owner_user_id")
    )
    user_key = _clean_string(
        params.get("user_key")
        or params.get("user_id")
        or arguments.get("user_key")
        or arguments.get("user_id")
        or scope.get("user_key")
        or scope.get("user_id")
        or legacy_user
    )
    tenant_key = _clean_string(params.get("tenant_key") or params.get("tenant_id") or arguments.get("tenant_key") or arguments.get("tenant_id") or scope.get("tenant_key") or scope.get("tenant_id"))
    if _pska_user_id_from_key(user_key) == "agent_service" and legacy_user:
        user_key = legacy_user
    if not user_key and not tenant_key:
        return None
    user_id = _pska_user_id_from_key(user_key or "user_primary")
    return RequestContext(
        tenant_id=tenant_key or DEFAULT_TENANT_ID,
        user_id=user_id,
        represented_user_id=None,
        caller="user",
        subject=user_key or user_id,
        auth_provider="mcp_params",
    )


def _mcp_arguments_have_tenant(arguments: dict[str, Any]) -> bool:
    if arguments.get("tenant_id") or arguments.get("tenant_key"):
        return True
    scope = arguments.get("scope") if isinstance(arguments.get("scope"), dict) else {}
    return bool(scope.get("tenant_id") or scope.get("tenant_key"))


def _assert_context_matches(authenticated: RequestContext, mcp_context: RequestContext) -> None:
    target_user_id = authenticated.represented_user_id or authenticated.user_id
    if authenticated.tenant_id != mcp_context.tenant_id or target_user_id != mcp_context.user_id:
        raise PermissionError("MCP identity params do not match authenticated context")


def _apply_mcp_context(arguments: dict[str, Any], context: RequestContext) -> dict[str, Any]:
    merged = dict(arguments)
    target_user_id = context.represented_user_id or context.user_id
    merged["tenant_id"] = context.tenant_id
    merged["user_id"] = target_user_id
    if isinstance(merged.get("scope"), dict):
        scope = dict(merged["scope"])
        scope["tenant_id"] = context.tenant_id
        scope["user_id"] = target_user_id
        scope["owner_user_id"] = target_user_id
        scope.pop("represented_user_id", None)
        merged["scope"] = scope
    if isinstance(merged.get("payload"), dict):
        payload = dict(merged["payload"])
        payload["tenant_id"] = context.tenant_id
        payload["owner_user_id"] = target_user_id
        merged["payload"] = payload
    merged["owner_user_id"] = target_user_id
    merged.pop("represented_user_id", None)
    return merged


def _emit_mcp_identity_log(
    event: str,
    *,
    tool_name: Any,
    params: dict[str, Any],
    authenticated_context: RequestContext | None,
    mcp_context: RequestContext | None,
    arguments: dict[str, Any],
    raw_arguments: dict[str, Any] | None = None,
) -> None:
    raw_scope = raw_arguments.get("scope") if isinstance(raw_arguments, dict) and isinstance(raw_arguments.get("scope"), dict) else {}
    scope = arguments.get("scope") if isinstance(arguments.get("scope"), dict) else {}
    source_item_ids = _string_list(arguments.get("source_item_ids") or scope.get("source_item_ids"))
    record = {
        "event": event,
        "tool_name": tool_name,
        "param_user_key": params.get("user_key") or params.get("user_id"),
        "param_tenant_key": params.get("tenant_key") or params.get("tenant_id"),
        "auth_tenant_id": getattr(authenticated_context, "tenant_id", None),
        "auth_user_id": getattr(authenticated_context, "user_id", None),
        "auth_caller": getattr(authenticated_context, "caller", None),
        "auth_provider": getattr(authenticated_context, "auth_provider", None),
        "mcp_tenant_id": getattr(mcp_context, "tenant_id", None),
        "mcp_user_id": getattr(mcp_context, "user_id", None),
        "arg_tenant_id": arguments.get("tenant_id"),
        "arg_user_id": arguments.get("user_id"),
        "raw_scope_tenant_id": raw_scope.get("tenant_id"),
        "raw_scope_user_id": raw_scope.get("user_id"),
        "scope_tenant_id": scope.get("tenant_id"),
        "scope_user_id": scope.get("user_id"),
        "scope_mode": arguments.get("scope_mode") or scope.get("scope_mode") or scope.get("mode"),
        "knowledge_base_ids": _knowledge_base_ids_from_mcp_arguments(arguments),
        "source_item_count": len(source_item_ids),
        "source_item_ids_preview": source_item_ids[:8],
    }
    print(json.dumps(to_jsonable(record), ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _pska_user_id_from_key(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("pska:") else value


def _clean_string(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


def _compact_search_result(item: dict[str, Any], *, max_snippet_chars: int) -> dict[str, Any]:
    snippet = _truncate(str(item.get("snippet") or ""), max_snippet_chars)
    compact = {
        "result_id": item.get("result_id"),
        "source_item_id": item.get("source_item_id"),
        "source": item.get("source"),
        "title": item.get("title"),
        "snippet": snippet,
        "score": item.get("score"),
        "citation": _compact_citation(item.get("citation"), max_snippet_chars=max_snippet_chars),
    }
    follow_up_keys = _dedupe_follow_up_keys(
        _extract_follow_up_keys(
            " ".join(
                str(value or "")
                for value in (compact.get("source_item_id"), compact.get("title"), compact.get("snippet"))
            )
        )
    )
    if follow_up_keys:
        compact["follow_up_keys"] = follow_up_keys[:8]
    return compact


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
    follow_up_keys = _dedupe_follow_up_keys(
        _extract_follow_up_keys(
            " ".join(
                str(value or "")
                for value in (compact.get("source_item_id"), compact.get("chunk_id"), compact.get("title"), compact.get("snippet"))
            )
        )
    )
    if follow_up_keys:
        compact["follow_up_keys"] = follow_up_keys[:8]
    return compact


def _compact_mcp_evidence_set(
    *,
    query: str | None,
    citations: list[dict[str, Any]],
    graph_paths: list[dict[str, Any]] | None = None,
    max_records: int = 6,
) -> dict[str, Any]:
    usable_citations = [citation for citation in citations if isinstance(citation, dict) and citation.get("source_item_id")]
    graph_context = graph_paths or []
    if not usable_citations and not graph_context:
        return {
            "schema": "pska.evidence_set.v1",
            "status": "empty",
            "records": [],
            "slots": [],
            "missing_slots": [],
            "conflicts": [],
            "audit": {"record_count": 0, "slot_count": 0, "source_type_counts": {}},
        }
    composed = EvidenceCompositionPipeline().compose(
        usable_citations,
        EvidenceCompositionContext(query=str(query or ""), max_records=max(1, min(int(max_records or 1), 8))),
        graph_paths=graph_context,
    )
    return _compact_evidence_set_payload(evidence_set_to_dict(composed.evidence_set))


def _compact_evidence_set_payload(evidence_set: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for record in evidence_set.get("records") if isinstance(evidence_set.get("records"), list) else []:
        if not isinstance(record, dict):
            continue
        citation = record.get("citation") if isinstance(record.get("citation"), dict) else {}
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        records.append(
            {
                "record_id": record.get("record_id"),
                "source_type": record.get("source_type"),
                "rank": record.get("rank"),
                "score": record.get("score"),
                "selected_span": _truncate(str(record.get("selected_span") or record.get("text") or ""), 360),
                "citation": _compact_citation(citation, max_snippet_chars=260),
                "metadata": {
                    key: metadata.get(key)
                    for key in ["title", "source_item_id", "document_id", "chunk_id", "passage_window_id"]
                    if metadata.get(key) is not None
                },
            }
        )
    audit = evidence_set.get("audit") if isinstance(evidence_set.get("audit"), dict) else {}
    return {
        "schema": evidence_set.get("schema") or "pska.evidence_set.v1",
        "evidence_set_id": evidence_set.get("evidence_set_id"),
        "status": evidence_set.get("status"),
        "records": records,
        "slots": _list_of_dicts(evidence_set.get("slots"))[:12],
        "missing_slots": list(evidence_set.get("missing_slots") or [])[:12],
        "conflicts": list(evidence_set.get("conflicts") or [])[:6],
        "audit": {
            "record_count": audit.get("record_count", len(records)),
            "slot_count": audit.get("slot_count"),
            "source_type_counts": audit.get("source_type_counts") if isinstance(audit.get("source_type_counts"), dict) else {},
            "missing_slots": audit.get("missing_slots") if isinstance(audit.get("missing_slots"), list) else [],
        },
    }


def _fit_mcp_payload_budget(payload: dict[str, Any], *, max_chars: int = 3800) -> dict[str, Any]:
    if len(json.dumps(payload, ensure_ascii=False)) < max_chars:
        return payload
    evidence_set = payload.get("evidence_set") if isinstance(payload.get("evidence_set"), dict) else {}
    if not evidence_set:
        return payload
    compact = {**payload, "evidence_set": _handle_only_evidence_set(evidence_set, include_titles=True)}
    compact = _drop_context_sections_for_budget(compact, max_chars=max_chars)
    if len(json.dumps(compact, ensure_ascii=False)) < max_chars:
        return compact
    compact = {**payload, "evidence_set": _handle_only_evidence_set(evidence_set, include_titles=False)}
    compact = _drop_context_sections_for_budget(compact, max_chars=max_chars)
    if len(json.dumps(compact, ensure_ascii=False)) < max_chars:
        return compact
    compact = dict(payload)
    compact.pop("evidence_set", None)
    return compact


def _drop_context_sections_for_budget(payload: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    if len(json.dumps(payload, ensure_ascii=False)) < max_chars:
        return payload
    compact = dict(payload)
    omitted = dict(compact.get("omitted") if isinstance(compact.get("omitted"), dict) else {})
    for section in ["source_items", "documents", "passage_windows"]:
        values = compact.get(section)
        if isinstance(values, list) and values:
            omitted[section] = omitted.get(section, 0) + len(values)
            compact[section] = []
            compact["omitted"] = omitted
            if len(json.dumps(compact, ensure_ascii=False)) < max_chars:
                return compact
    compact = _shrink_repeated_evidence_text(compact)
    if len(json.dumps(compact, ensure_ascii=False)) < max_chars:
        return compact
    values = compact.get("chunks")
    if isinstance(values, list) and values:
        omitted["chunks"] = omitted.get("chunks", 0) + len(values)
        compact["chunks"] = []
        compact["omitted"] = omitted
    return compact


def _shrink_repeated_evidence_text(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    for section in ["results", "citations", "chunks"]:
        compact[section] = [
            _truncate_evidence_item_text(item, max_chars=160)
            for item in _list_of_dicts(compact.get(section))
        ]
    evidence_set = compact.get("evidence_set") if isinstance(compact.get("evidence_set"), dict) else {}
    if evidence_set:
        records = []
        for record in _list_of_dicts(evidence_set.get("records")):
            citation = record.get("citation") if isinstance(record.get("citation"), dict) else {}
            records.append(
                {
                    **record,
                    "selected_span": _truncate(str(record.get("selected_span") or ""), 120) if record.get("selected_span") else record.get("selected_span"),
                    "citation": _truncate_evidence_item_text(citation, max_chars=120),
                }
            )
        compact["evidence_set"] = {**evidence_set, "records": records}
    return compact


def _truncate_evidence_item_text(item: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    compact = dict(item)
    for key in ["snippet", "text", "title"]:
        if compact.get(key):
            compact[key] = _truncate(str(compact.get(key) or ""), max_chars)
    citation = compact.get("citation") if isinstance(compact.get("citation"), dict) else {}
    if citation:
        compact["citation"] = _truncate_evidence_item_text(citation, max_chars=max_chars)
    return compact


def _handle_only_evidence_set(evidence_set: dict[str, Any], *, include_titles: bool) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for record in _list_of_dicts(evidence_set.get("records"))[:8]:
        citation = record.get("citation") if isinstance(record.get("citation"), dict) else {}
        compact_citation = {
            key: citation.get(key)
            for key in ["source_item_id", "document_id", "chunk_id", "passage_window_id"]
            if citation.get(key) is not None
        }
        if include_titles and citation.get("title"):
            compact_citation["title"] = _truncate(str(citation.get("title") or ""), 80)
        records.append(
            {
                "record_id": record.get("record_id"),
                "source_type": record.get("source_type"),
                "rank": record.get("rank"),
                "citation": compact_citation,
            }
        )
    audit = evidence_set.get("audit") if isinstance(evidence_set.get("audit"), dict) else {}
    return {
        "schema": evidence_set.get("schema") or "pska.evidence_set.v1",
        "evidence_set_id": evidence_set.get("evidence_set_id"),
        "status": evidence_set.get("status"),
        "records": records,
        "slots": _list_of_dicts(evidence_set.get("slots"))[:6],
        "missing_slots": list(evidence_set.get("missing_slots") or [])[:6],
        "conflicts": list(evidence_set.get("conflicts") or [])[:3],
        "audit": {
            "record_count": audit.get("record_count", len(records)),
            "slot_count": audit.get("slot_count"),
            "source_type_counts": audit.get("source_type_counts") if isinstance(audit.get("source_type_counts"), dict) else {},
        },
    }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _extract_follow_up_keys(text: str) -> list[str]:
    value = str(text or "")
    keys: list[str] = []
    keys.extend(match.strip() for match in re.findall(r"`([^`\n]{2,80})`", value))
    keys.extend(re.findall(r"\b[A-Z][A-Z0-9]{1,20}(?:-[A-Z0-9]{1,20})+\b", value))
    keys.extend(re.findall(r"\b(?:src|chk|doc|pw)_[a-zA-Z0-9_]{8,80}\b", value))
    return _dedupe_follow_up_keys(keys)


def _dedupe_follow_up_keys(keys: Any) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_key in keys or []:
        key = str(raw_key or "").strip(" \t\r\n,.;:()[]{}<>\"'")
        if len(key) < 3 or len(key) > 96:
            continue
        normalized = key.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(key)
    return deduped


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


def _source_refs_from_mcp_arguments(arguments: dict[str, Any]) -> list[SourceRef]:
    refs: list[SourceRef] = []
    for raw in arguments.get("source_refs") or []:
        if isinstance(raw, SourceRef):
            refs.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        refs.append(
            SourceRef(
                source_item_id=_clean_string(raw.get("source_item_id")) or None,
                document_id=_clean_string(raw.get("document_id")) or None,
                chunk_id=_clean_string(raw.get("chunk_id")) or None,
                passage_window_id=_clean_string(raw.get("passage_window_id")) or None,
                message_id=_clean_string(raw.get("message_id")) or None,
                path=_clean_string(raw.get("path")) or None,
                url=_clean_string(raw.get("url")) or None,
            )
        )
    return refs


def _knowledge_base_ids_from_mcp_arguments(arguments: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    ids.extend(_string_list(arguments.get("knowledge_base_id")))
    ids.extend(_string_list(arguments.get("knowledge_base_ids")))
    scope = arguments.get("scope") if isinstance(arguments.get("scope"), dict) else {}
    ids.extend(_string_list(scope.get("knowledge_base_id")))
    ids.extend(_string_list(scope.get("knowledge_base_ids")))
    return list(dict.fromkeys(item for item in ids if item))


def _source_item_ids_from_mcp_arguments(arguments: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    ids.extend(_string_list(arguments.get("source_item_id")))
    ids.extend(_string_list(arguments.get("source_item_ids")))
    scope = arguments.get("scope") if isinstance(arguments.get("scope"), dict) else {}
    ids.extend(_string_list(scope.get("source_item_id")))
    ids.extend(_string_list(scope.get("source_item_ids")))
    return list(dict.fromkeys(item for item in ids if item))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list | tuple | set):
        return [str(value)] if str(value).strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def _query_terms(query: str) -> set[str]:
    return {term.casefold() for term in re.findall(r"[\w\u4e00-\u9fff-]+", query or "") if len(term.strip()) > 1}


def _text_score(text: str, terms: set[str]) -> int:
    normalized = (text or "").casefold()
    return sum(1 for term in terms if term in normalized)


def _rank_items_by_query(items: list[Any], query: str) -> list[Any]:
    terms = _query_terms(query)
    if not terms:
        return items
    return sorted(
        items,
        key=lambda item: (
            _text_score(
                " ".join(
                    [
                        str(getattr(item, "title", "") or ""),
                        str(getattr(item, "source_id", "") or ""),
                        str(getattr(item, "content_text", "") or ""),
                    ]
                ),
                terms,
            ),
            str(getattr(item, "updated_at", "") or ""),
            str(getattr(item, "source_item_id", "") or ""),
        ),
        reverse=True,
    )


def _rank_objects_by_query(objects: list[Any], query: str, *, fields: tuple[str, ...]) -> list[Any]:
    terms = _query_terms(query)
    if not terms:
        return objects

    def object_text(obj: Any) -> str:
        values: list[str] = []
        for field in fields:
            value = getattr(obj, field, "")
            values.append(json.dumps(to_jsonable(value), ensure_ascii=False) if isinstance(value, (list, dict)) else str(value or ""))
        return " ".join(values)

    ranked = sorted(
        objects,
        key=lambda obj: (_text_score(object_text(obj), terms), str(getattr(obj, "created_at", "") or ""), str(getattr(obj, "knowledge_claim_id", "") or getattr(obj, "digest_note_id", "") or "")),
        reverse=True,
    )
    return [obj for obj in ranked if _text_score(object_text(obj), terms) > 0] or ranked


def _evidence_results_from_context(source_items: list[Any], documents: list[Any], chunks: list[Any], *, max_snippet_chars: int, query: str | None = None) -> list[dict[str, Any]]:
    item_by_id = {item.source_item_id: item for item in source_items}
    document_by_id = {document.document_id: document for document in documents}
    results: list[dict[str, Any]] = []
    for chunk in chunks:
        document = document_by_id.get(chunk.document_id)
        item = item_by_id.get(chunk.source_item_id)
        title = getattr(document, "title", "") or getattr(item, "title", "") or chunk.source_item_id
        snippet = _focused_context_text(str(getattr(chunk, "text", "") or ""), query=query, max_chars=max_snippet_chars)
        results.append(
            {
                "result_id": chunk.chunk_id,
                "source_item_id": chunk.source_item_id,
                "source": getattr(item, "source_channel", None),
                "title": title,
                "snippet": snippet,
                "score": 1.0,
                "citation": {
                    "source_item_id": chunk.source_item_id,
                    "document_id": chunk.document_id,
                    "chunk_id": chunk.chunk_id,
                    "url": getattr(item, "url", None),
                    "title": title,
                    "snippet": snippet,
                },
            }
        )
    if results:
        return results
    for document in documents:
        item = item_by_id.get(document.source_item_id)
        snippet = _focused_context_text(str(getattr(document, "body", "") or ""), query=query, max_chars=max_snippet_chars)
        results.append(
            {
                "result_id": document.document_id,
                "source_item_id": document.source_item_id,
                "source": getattr(item, "source_channel", None),
                "title": getattr(document, "title", "") or getattr(item, "title", "") or document.source_item_id,
                "snippet": snippet,
                "score": 1.0,
                "citation": {
                    "source_item_id": document.source_item_id,
                    "document_id": document.document_id,
                    "url": getattr(item, "url", None),
                    "title": getattr(document, "title", "") or getattr(item, "title", "") or document.source_item_id,
                    "snippet": snippet,
                },
            }
        )
    if results:
        return results
    for item in source_items:
        snippet = _focused_context_text(str(item.content_text or ""), query=query, max_chars=max_snippet_chars)
        results.append(
            {
                "result_id": item.source_item_id,
                "source_item_id": item.source_item_id,
                "source": item.source_channel,
                "title": item.title,
                "snippet": snippet,
                "score": 1.0,
                "citation": {
                    "source_item_id": item.source_item_id,
                    "url": item.url,
                    "title": item.title,
                    "snippet": snippet,
                },
            }
        )
    return results


def _citations_for_source_items(source_items: list[Any], *, chunks: list[Any], max_snippet_chars: int, query: str | None = None) -> list[dict[str, Any]]:
    item_by_id = {item.source_item_id: item for item in source_items}
    chunks_by_source: dict[str, list[Any]] = {}
    for chunk in chunks:
        chunks_by_source.setdefault(chunk.source_item_id, []).append(chunk)
    citations: list[dict[str, Any]] = []
    for item in source_items:
        item_chunks = chunks_by_source.get(item.source_item_id, [])
        if not item_chunks:
            citations.append(
                {
                    "source_item_id": item.source_item_id,
                    "url": item.url,
                    "title": item.title,
                    "snippet": _focused_context_text(str(item.content_text or ""), query=query, max_chars=max_snippet_chars),
                }
            )
            continue
        for chunk in item_chunks[:2]:
            snippet = _focused_context_text(str(chunk.text or ""), query=query, max_chars=max_snippet_chars)
            citations.append(
                {
                    "source_item_id": item.source_item_id,
                    "document_id": chunk.document_id,
                    "chunk_id": chunk.chunk_id,
                    "url": item_by_id[chunk.source_item_id].url,
                    "title": item_by_id[chunk.source_item_id].title,
                    "snippet": snippet,
                }
            )
    return citations


def _retrieval_results_from_sources(query: str, source_items: list[Any], source_ids: set[str]) -> list[Any]:
    candidates = [item for item in source_items if not source_ids or item.source_item_id in source_ids]
    if query:
        candidates = _rank_items_by_query(candidates, query)
    return [
        SimpleNamespace(
            result_id=item.source_item_id,
            source_item_id=item.source_item_id,
            source=item.source_channel,
            title=item.title,
            snippet=_truncate(str(item.content_text or ""), 700),
            score=1.0,
            citation={"source_item_id": item.source_item_id, "title": item.title, "url": item.url},
        )
        for item in candidates[:12]
    ]


def _source_refs_from_graph_context(edge_contexts: list[dict[str, Any]], graph_paths: list[dict[str, Any]]) -> list[SourceRef]:
    refs: list[SourceRef] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("source_item_id") or value.get("document_id") or value.get("chunk_id"):
                refs.append(
                    SourceRef(
                        source_item_id=_clean_string(value.get("source_item_id")) or None,
                        document_id=_clean_string(value.get("document_id")) or None,
                        chunk_id=_clean_string(value.get("chunk_id")) or None,
                        passage_window_id=_clean_string(value.get("passage_window_id")) or None,
                        url=_clean_string(value.get("url")) or None,
                    )
                )
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(edge_contexts)
    collect(graph_paths)
    return refs


def _results_from_graph_edges(edge_contexts: list[dict[str, Any]], *, max_snippet_chars: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for edge in edge_contexts:
        source_refs = edge.get("source_refs") if isinstance(edge.get("source_refs"), list) else []
        first_ref = next((ref for ref in source_refs if isinstance(ref, dict)), {})
        members = edge.get("members") if isinstance(edge.get("members"), list) else []
        title = " / ".join(str(member.get("label") or "") for member in members if isinstance(member, dict) and member.get("label")) or str(edge.get("relation_type") or "graph evidence")
        snippet = _truncate(str(edge.get("evidence_text") or ""), max_snippet_chars)
        results.append(
            {
                "result_id": edge.get("hyperedge_id"),
                "source_item_id": first_ref.get("source_item_id"),
                "source": "graph",
                "title": title,
                "snippet": snippet,
                "score": edge.get("confidence"),
                "citation": {
                    "source_item_id": first_ref.get("source_item_id"),
                    "document_id": first_ref.get("document_id"),
                    "chunk_id": first_ref.get("chunk_id"),
                    "title": title,
                    "snippet": snippet,
                },
            }
        )
    return results


def _results_from_digest_context(claims: list[Any], notes: list[Any], *, max_snippet_chars: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for claim in claims:
        source_ref = next((ref for ref in getattr(claim, "source_refs", []) if isinstance(ref, SourceRef)), SourceRef())
        snippet = _truncate(str(getattr(claim, "statement", "") or getattr(claim, "evidence_text", "") or ""), max_snippet_chars)
        results.append(
            {
                "result_id": getattr(claim, "knowledge_claim_id", None),
                "source_item_id": source_ref.source_item_id,
                "source": "knowledge_claim",
                "title": str(getattr(claim, "claim_type", "") or "knowledge claim"),
                "snippet": snippet,
                "score": getattr(claim, "confidence", None),
                "citation": {
                    "source_item_id": source_ref.source_item_id,
                    "document_id": source_ref.document_id,
                    "chunk_id": source_ref.chunk_id,
                    "title": str(getattr(claim, "claim_type", "") or "knowledge claim"),
                    "snippet": snippet,
                },
            }
        )
    for note in notes:
        source_ref = next((ref for ref in getattr(note, "source_refs", []) if isinstance(ref, SourceRef)), SourceRef())
        snippet = _truncate(str(getattr(note, "synopsis", "") or ""), max_snippet_chars)
        results.append(
            {
                "result_id": getattr(note, "digest_note_id", None),
                "source_item_id": source_ref.source_item_id,
                "source": "digest_note",
                "title": str(getattr(note, "title", "") or "digest note"),
                "snippet": snippet,
                "score": getattr(note, "confidence", None),
                "citation": {
                    "source_item_id": source_ref.source_item_id,
                    "document_id": source_ref.document_id,
                    "chunk_id": source_ref.chunk_id,
                    "title": str(getattr(note, "title", "") or "digest note"),
                    "snippet": snippet,
                },
            }
        )
    return results


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


def _focused_context_text(text: str, *, query: str | None, max_chars: int) -> str:
    if query:
        focused = query_focused_evidence_snippet(text, query, max_chars=max_chars)
    else:
        focused = _truncate(text, max_chars)
    return _wrap_long_lines(_truncate(focused, max_chars), max_line_chars=min(500, max(120, max_chars // 2)))


def _wrap_long_lines(text: str, *, max_line_chars: int) -> str:
    lines: list[str] = []
    for line in str(text or "").splitlines() or [""]:
        if len(line) <= max_line_chars:
            lines.append(line)
            continue
        cursor = 0
        while cursor < len(line):
            lines.append(line[cursor : cursor + max_line_chars])
            cursor += max_line_chars
    return "\n".join(lines).strip()


def _compact_chunk(chunk: Any, *, max_chars: int, query: str | None = None) -> dict[str, Any]:
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
        "text": _focused_context_text(text, query=query, max_chars=max_chars),
        "text_chars": len(text),
    }


def _compact_document(document: Any, *, max_chars: int, query: str | None = None) -> dict[str, Any]:
    payload = to_jsonable(document)
    body = str(payload.get("body") or "")
    return {
        "document_id": payload.get("document_id"),
        "source_item_id": payload.get("source_item_id"),
        "owner_user_id": payload.get("owner_user_id"),
        "space_id": payload.get("space_id"),
        "visibility": payload.get("visibility"),
        "title": payload.get("title"),
        "body": _focused_context_text(body, query=query, max_chars=max_chars),
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


def _compact_passage_window(window: dict[str, Any], *, max_chars: int, query: str | None = None) -> dict[str, Any]:
    text = str(window.get("text") or "")
    return {**window, "text": _focused_context_text(text, query=query, max_chars=max_chars), "text_chars": len(text)}


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
