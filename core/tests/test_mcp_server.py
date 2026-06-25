from __future__ import annotations

import json

from pska_core.ingest import IngestService
from pska_core.jobs import JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import DigestNote, SourceRef, User
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
    assert "pska_agentic_search" not in names
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
    assert "score_debug" not in payload["results"][0]


def test_mcp_search_compacts_long_results_for_fastreact() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-long-search-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "searchable " + ("very long evidence " * 300)},
        }
    )
    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "pska_search",
                "arguments": {"query": "searchable", "user_id": "user_primary", "max_snippet_chars": 180},
            },
        }
    )

    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)

    assert len(text) < 4000
    assert payload["results"][0]["snippet"].endswith("...[truncated]")
    assert payload["omitted"]["reason"] == "MCP compact output keeps FastReAct tool results parser-safe."


def test_mcp_job_context_defaults_to_compact_output() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    ingested = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-long-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "PSKA compact context. " * 500},
        }
    )
    job = JobService(store).submit(
        "digest_via_fastreact",
        {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": ingested.source_item_id}]},
    )
    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {"name": "pska_job_context", "arguments": {"job_id": job.job_id}},
        }
    )

    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert len(text) < 8000
    assert len(payload["source_items"]) == 1
    assert len(payload["chunks"]) <= 1
    assert payload["source_items"][0]["content_text"].endswith("[truncated]")
    assert payload["has_more"] is False


def test_mcp_job_context_compacts_existing_digest_notes() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    ingested = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-digest-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "source text"},
        }
    )
    store.add_digest_note(
        DigestNote(
            "dig_existing",
            "user_primary",
            "Existing digest",
            "synopsis " * 200,
            [SourceRef(source_item_id=ingested.source_item_id)],
            key_points=[{"point": f"point {idx} " * 80, "source_refs": [{"source_item_id": ingested.source_item_id}]} for idx in range(8)],
            actions=[{"action": f"action {idx} " * 80, "source_refs": [{"source_item_id": ingested.source_item_id}]} for idx in range(8)],
            risks=[{"risk": f"risk {idx} " * 80, "source_refs": [{"source_item_id": ingested.source_item_id}]} for idx in range(8)],
        )
    )
    job = JobService(store).submit(
        "digest_via_fastreact",
        {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": ingested.source_item_id}]},
    )

    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {"name": "pska_job_context", "arguments": {"job_id": job.job_id}},
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    note = payload["digest_notes"][0]
    assert len(note["synopsis"]) < 750
    assert len(note["key_points"]) == 5
    assert len(note["actions"]) == 4
    assert len(note["risks"]) == 3
    assert "memory_suggestions" not in note
    assert "relationship_suggestions" not in note


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
