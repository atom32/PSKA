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
from pska_core.models import ChannelIngestPayload
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
                "user_id": {"type": "string", "default": "user_primary"},
                "represented_user_id": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pska_index_status",
        "description": "Return basic PSKA index counts.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
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
            "properties": {"owner_user_id": {"type": "string"}},
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
                "owner_user_id": {"type": "string"},
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
                "user_id": {"type": "string", "default": "user_primary"},
                "represented_user_id": {"type": "string"},
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
            return self.result(request_id, self.call_tool(params.get("name"), params.get("arguments") or {}, context=context))
        return self.error(request_id, -32601, f"Unknown method: {method}")

    def call_tool(self, name: str, arguments: dict[str, Any], *, context: RequestContext | None = None) -> dict[str, Any]:
        arguments = context.apply_to_payload(arguments) if context else arguments
        if name == "pska_search":
            payload = self.pska_search(arguments)
        elif name == "pska_index_status":
            payload = self.pska_index_status()
        elif name == "pska_ingest_channel_payload":
            payload = self.pska_ingest_channel_payload(arguments)
        elif name == "pska_extract_all":
            payload = self.pska_extract_all(arguments)
        elif name == "pska_review_items":
            payload = self.pska_review_items()
        elif name == "pska_write_candidates":
            payload = self.pska_write_candidates(arguments)
        elif name == "pska_job_context":
            payload = self.pska_job_context(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return {"content": [{"type": "text", "text": json.dumps(to_jsonable(payload), ensure_ascii=False)}]}

    def pska_search(self, arguments: dict[str, Any]) -> Any:
        user = self.store.get_user(arguments.get("user_id") or "user_primary")
        return self.retrieval.search(
            arguments["query"],
            user,
            represented_user_id=arguments.get("represented_user_id"),
            top_k=int(arguments.get("top_k") or 5),
        )

    def pska_index_status(self) -> dict[str, int]:
        return {
            "source_items": self.store.count_table("source_items"),
            "documents": self.store.count_table("documents"),
            "chunks": self.store.count_table("chunks"),
            "entities": self.store.count_table("entities"),
            "hyperedges": self.store.count_table("hyperedges"),
            "review_items": self.store.count_table("review_items"),
        }

    def pska_ingest_channel_payload(self, arguments: dict[str, Any]) -> Any:
        return self.ingest.ingest_channel_payload(ChannelIngestPayload.from_mapping(arguments["payload"]))

    def pska_extract_all(self, arguments: dict[str, Any]) -> Any:
        return {"reports": self.extraction.extract_all_visible(owner_user_id=arguments.get("owner_user_id"))}

    def pska_review_items(self) -> Any:
        return {"review_items": self.store.list_review_items()}

    def pska_write_candidates(self, arguments: dict[str, Any]) -> Any:
        return {"summary": self.candidates.write_candidates(arguments)}

    def pska_job_context(self, arguments: dict[str, Any]) -> Any:
        job = self.store.get_job(str(arguments["job_id"]))
        request_user_id = str(arguments.get("represented_user_id") or arguments.get("user_id") or "user_primary")
        source_item_ids = _job_source_item_ids(job)
        source_items = [
            item
            for item in self.store.list_source_items()
            if item.source_item_id in source_item_ids and item.owner_user_id == request_user_id
        ]
        chunks = self.store.list_chunks_for_sources({item.source_item_id for item in source_items})
        return {
            "job": job,
            "request_user_id": request_user_id,
            "source_items": source_items,
            "chunks": chunks,
        }

    def result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def write(self, response: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


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


if __name__ == "__main__":
    raise SystemExit(main())
