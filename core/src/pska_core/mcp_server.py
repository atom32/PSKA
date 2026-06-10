from __future__ import annotations

import json
import os
import sys
from typing import Any

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
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
        "name": "pska_agentic_search",
        "description": "Run a small agentic PSKA search plan and return retrieval trace plus citations.",
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
]


def main() -> int:
    server = MCPServer(os.environ.get("PSKA_DATABASE_URL", "postgresql:///pska"))
    return server.run()


class MCPServer:
    def __init__(self, database_url: str, store: Any | None = None, llm: Any | None = None) -> None:
        self.store = store or PostgresKnowledgeStore(database_url)
        self.retrieval = RetrievalService(self.store, ACLService(self.store))
        self.agentic = AgenticSearchService(self.retrieval, llm=llm)
        self.ingest = IngestService(self.store)
        self.extraction = ExtractionService(self.store, llm=llm)

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

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
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
            return self.result(request_id, self.call_tool(params.get("name"), params.get("arguments") or {}))
        return self.error(request_id, -32601, f"Unknown method: {method}")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "pska_search":
            payload = self.pska_search(arguments)
        elif name == "pska_agentic_search":
            payload = self.pska_agentic_search(arguments)
        elif name == "pska_index_status":
            payload = self.pska_index_status()
        elif name == "pska_ingest_channel_payload":
            payload = self.pska_ingest_channel_payload(arguments)
        elif name == "pska_extract_all":
            payload = self.pska_extract_all(arguments)
        elif name == "pska_review_items":
            payload = self.pska_review_items()
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

    def pska_agentic_search(self, arguments: dict[str, Any]) -> Any:
        user = self.store.get_user(arguments.get("user_id") or "user_primary")
        return self.agentic.search(
            arguments["query"],
            user,
            represented_user_id=arguments.get("represented_user_id"),
            max_iterations=3,
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

    def result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def write(self, response: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
