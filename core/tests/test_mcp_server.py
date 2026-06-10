from __future__ import annotations

import json

from pska_core.ingest import IngestService
from pska_core.mcp_server import MCPServer
from pska_core.models import User
from pska_core.store import InMemoryKnowledgeStore
from tests.fakes import FakeLLM, extraction_response


def make_server() -> MCPServer:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "mcp searchable text"},
        }
    )
    return MCPServer("postgresql:///unused", store=store, llm=FakeLLM([extraction_response(), extraction_response()]))


def test_mcp_lists_pska_tools() -> None:
    response = make_server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "pska_search" in names
    assert "pska_agentic_search" in names
    assert "pska_index_status" in names
    assert "pska_ingest_channel_payload" in names
    assert "pska_extract_all" in names
    assert "pska_review_items" in names


def test_mcp_calls_pska_search() -> None:
    response = make_server().handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "pska_search", "arguments": {"query": "searchable", "user_id": "user_primary"}},
        }
    )

    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["citations"]
    assert payload["results"][0]["source_item_id"]


def test_mcp_ingests_and_extracts() -> None:
    server = make_server()
    ingest = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "pska_ingest_channel_payload",
                "arguments": {
                    "payload": {
                        "schema_version": "pska.channel_ingest.v1",
                        "source_channel": "manual",
                        "record_type": "note",
                        "source_id": "mcp-extract",
                        "owner_user_id": "user_primary",
                        "space_id": "private_primary",
                        "visibility": "private",
                        "content": {"text": "The policy P-204 covers the education enrollment stage for dependent K."},
                    }
                },
            },
        }
    )
    extract = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "pska_extract_all", "arguments": {"owner_user_id": "user_primary"}},
        }
    )

    assert json.loads(ingest["result"]["content"][0]["text"])["source_id"] == "mcp-extract"
    reports = json.loads(extract["result"]["content"][0]["text"])["reports"]
    assert any(report["hyperedges_created"] for report in reports)
