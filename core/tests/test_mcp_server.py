from __future__ import annotations

import json

from pska_core.ingest import IngestService
from pska_core.jobs import JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import DigestNote, KnowledgeBase, KnowledgeBaseSourceItem, KnowledgeClaim, SourceRef, User
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


def make_scoped_kb_server() -> tuple[MCPServer, str, str, str, str]:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-alpha-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "mcp scoped sharedtoken alpha-only evidence"},
        }
    )
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-beta-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "mcp scoped sharedtoken beta-only evidence"},
        }
    )
    alpha_source_id = next(item.source_item_id for item in store.list_source_items() if item.source_id == "mcp-alpha-note")
    beta_source_id = next(item.source_item_id for item in store.list_source_items() if item.source_id == "mcp-beta-note")
    alpha_kb = KnowledgeBase("kb_mcp_alpha", "user_primary", "MCP Alpha KB")
    beta_kb = KnowledgeBase("kb_mcp_beta", "user_primary", "MCP Beta KB")
    store.upsert_knowledge_base(alpha_kb)
    store.upsert_knowledge_base(beta_kb)
    store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=alpha_kb.knowledge_base_id,
            source_item_id=alpha_source_id,
            owner_user_id="user_primary",
            added_by_user_id="user_primary",
        )
    )
    store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=beta_kb.knowledge_base_id,
            source_item_id=beta_source_id,
            owner_user_id="user_primary",
            added_by_user_id="user_primary",
        )
    )
    return MCPServer("postgresql:///unused", store=store), alpha_kb.knowledge_base_id, beta_kb.knowledge_base_id, alpha_source_id, beta_source_id


def test_mcp_lists_pska_tools() -> None:
    response = make_server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "pska_search" in names
    assert "pska_agentic_search" not in names
    assert "pska_index_status" in names
    assert "pska_read_evidence_context" in names
    assert "pska_graph_context" in names
    assert "pska_digest_context" in names
    assert "pska_ingest_channel_payload" in names
    assert "pska_extract_all" in names
    assert "pska_review_items" in names
    tools_by_name = {tool["name"]: tool for tool in response["result"]["tools"]}
    search_properties = tools_by_name["pska_search"]["inputSchema"]["properties"]
    assert "tenant_id" not in search_properties
    assert "user_id" not in search_properties
    assert "knowledge_base_ids" in search_properties
    assert tools_by_name["pska_index_status"]["inputSchema"]["properties"] == {}


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
    assert payload["evidence_set"]["schema"] == "pska.evidence_set.v1"
    assert payload["evidence_set"]["records"]
    assert payload["evidence_set"]["records"][0]["citation"]["source_item_id"] == payload["citations"][0]["source_item_id"]
    assert "score_debug" not in payload["results"][0]


def test_mcp_search_filters_by_knowledge_base_ids() -> None:
    server, alpha_kb_id, beta_kb_id, alpha_source_id, beta_source_id = make_scoped_kb_server()

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {
                "name": "pska_search",
                "arguments": {
                    "query": "sharedtoken evidence",
                    "user_id": "user_primary",
                    "knowledge_base_ids": [alpha_kb_id],
                    "top_k": 5,
                },
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    result_source_ids = {result["source_item_id"] for result in payload["results"]}
    citation_source_ids = {citation["source_item_id"] for citation in payload["citations"]}

    assert payload["scope_applied"]["knowledge_base_ids"] == [alpha_kb_id]
    assert payload["scope_applied"]["source_item_ids"] == [alpha_source_id]
    assert result_source_ids == {alpha_source_id}
    assert citation_source_ids == {alpha_source_id}
    assert payload["evidence_set"]["status"] in {"composed", "incomplete", "needs_review"}
    assert {record["citation"]["source_item_id"] for record in payload["evidence_set"]["records"]} == {alpha_source_id}
    assert beta_kb_id not in payload["scope_applied"]["knowledge_base_ids"]
    assert beta_source_id not in result_source_ids | citation_source_ids


def test_mcp_read_evidence_context_filters_by_knowledge_base_ids() -> None:
    server, alpha_kb_id, _, alpha_source_id, beta_source_id = make_scoped_kb_server()

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "pska_read_evidence_context",
                "arguments": {
                    "query": "sharedtoken evidence",
                    "user_id": "user_primary",
                    "knowledge_base_ids": [alpha_kb_id],
                    "max_items": 4,
                },
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    result_source_ids = {result["source_item_id"] for result in payload["results"]}
    citation_source_ids = {citation["source_item_id"] for citation in payload["citations"]}

    assert payload["scope_applied"]["knowledge_base_ids"] == [alpha_kb_id]
    assert payload["scope_applied"]["source_item_ids"] == [alpha_source_id]
    assert result_source_ids == {alpha_source_id}
    assert citation_source_ids == {alpha_source_id}
    assert payload["evidence_set"]["records"]
    assert {record["citation"]["source_item_id"] for record in payload["evidence_set"]["records"]} == {alpha_source_id}
    assert beta_source_id not in result_source_ids | citation_source_ids


def test_mcp_params_tenant_identity_overrides_tool_arguments() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", tenant_id="tenant_default"))
    store.add_user(User("alice", "alice", tenant_id="tenant_acme"))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-tenant-default-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "tenant default only marker orchard lantern"},
        }
    )
    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "pska_search",
                "user_key": "pska:alice",
                "tenant_key": "tenant_acme",
                "arguments": {
                    "query": "orchard lantern",
                    "user_id": "user_primary",
                    "tenant_id": "tenant_default",
                    "top_k": 5,
                },
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])

    assert payload["request_user_id"] == "alice"
    assert payload["results"] == []
    assert payload["citations"] == []


def test_mcp_read_evidence_context_uses_request_context_scope() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", tenant_id="tenant_default"))
    store.add_user(User("alice", "alice", tenant_id="tenant_acme"))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "default-evidence",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "default tenant marker should stay hidden"},
        }
    )
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "alice-evidence",
            "owner_user_id": "alice",
            "tenant_id": "tenant_acme",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "tenant acme evidence marker is visible"},
        }
    )

    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 25,
            "method": "tools/call",
            "params": {
                "name": "pska_read_evidence_context",
                "user_key": "pska:alice",
                "tenant_key": "tenant_acme",
                "arguments": {
                    "query": "evidence marker",
                    "tenant_id": "tenant_default",
                    "user_id": "user_primary",
                },
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    snippets = " ".join(str(result.get("snippet") or "") for result in payload["results"])
    assert payload["tenant_id"] == "tenant_acme"
    assert payload["request_user_id"] == "alice"
    assert "tenant acme evidence marker" in snippets
    assert "default tenant marker" not in snippets


def test_mcp_index_status_and_digest_context_use_represented_user_scope() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", tenant_id="tenant_default"))
    store.add_user(User("alice", "alice", tenant_id="tenant_acme"))
    ingested = IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "alice-digest-evidence",
            "owner_user_id": "alice",
            "tenant_id": "tenant_acme",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "digest scoped fact"},
        }
    )
    store.add_knowledge_claim(
        KnowledgeClaim(
            "claim_alice",
            "alice",
            "fact",
            "Alice-scoped claim",
            [SourceRef(source_item_id=ingested.source_item_id)],
            "digest scoped fact",
            tenant_id="tenant_acme",
        )
    )
    store.add_digest_note(
        DigestNote(
            "note_alice",
            "alice",
            "Alice digest",
            "Only alice tenant digest",
            [SourceRef(source_item_id=ingested.source_item_id)],
            tenant_id="tenant_acme",
        )
    )
    server = MCPServer("postgresql:///unused", store=store)

    index_response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 26,
            "method": "tools/call",
            "params": {
                "name": "pska_index_status",
                "user_key": "pska:alice",
                "tenant_key": "tenant_acme",
                "arguments": {"tenant_id": "tenant_default", "user_id": "user_primary"},
            },
        }
    )
    digest_response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 27,
            "method": "tools/call",
            "params": {
                "name": "pska_digest_context",
                "user_key": "pska:alice",
                "tenant_key": "tenant_acme",
                "arguments": {"query": "alice digest", "tenant_id": "tenant_default", "user_id": "user_primary"},
            },
        }
    )

    index_payload = json.loads(index_response["result"]["content"][0]["text"])
    digest_payload = json.loads(digest_response["result"]["content"][0]["text"])
    assert index_payload["tenant_id"] == "tenant_acme"
    assert index_payload["request_user_id"] == "alice"
    assert index_payload["knowledge_claims"] == 1
    assert index_payload["digest_notes"] == 1
    assert digest_payload["request_user_id"] == "alice"
    assert digest_payload["knowledge_claims"][0]["knowledge_claim_id"] == "claim_alice"
    assert digest_payload["digest_notes"][0]["digest_note_id"] == "note_alice"


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


def test_mcp_read_evidence_context_compacts_long_results_for_fastreact() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    long_unbroken_cell = "A" * 12000
    long_separated_body = "\n\n".join(f"generic evidence row {idx} value {idx}" for idx in range(1200))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-long-evidence-context-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {
                "text": f"parser-safe-evidence {long_unbroken_cell}\n\n{long_separated_body}",
            },
        }
    )
    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "pska_read_evidence_context",
                "arguments": {
                    "query": "parser-safe-evidence",
                    "user_id": "user_primary",
                    "max_items": 12,
                    "max_source_chars": 8000,
                    "max_document_chars": 12000,
                    "max_passage_chars": 8000,
                    "max_chunk_chars": 6000,
                },
            },
        }
    )

    text = response["result"]["content"][0]["text"]
    payload = json.loads(text)

    assert len(text) < 4000
    assert payload["results"]
    assert payload["citations"]
    assert payload["chunks"]
    assert payload["omitted"]["reason"] == "MCP compact output keeps FastReAct tool results parser-safe."


def test_mcp_search_surfaces_follow_up_keys_for_agentic_research() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary"))
    IngestService(store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-follow-up-key-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Research intake",
            "content": {
                "text": (
                    "Research intake for a generic question. Evidence keys: `RENEWAL-482`, "
                    "`INCIDENT-17B`, `CASHRUN-93`, and `MARGIN-ALPHA`."
                )
            },
        }
    )

    response = MCPServer("postgresql:///unused", store=store).handle(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "pska_search",
                "arguments": {"query": "generic research intake", "user_id": "user_primary", "max_snippet_chars": 260},
            },
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])

    assert {"RENEWAL-482", "INCIDENT-17B", "CASHRUN-93", "MARGIN-ALPHA"} <= set(payload["follow_up_keys"])
    assert {"RENEWAL-482", "INCIDENT-17B"} <= set(payload["results"][0]["follow_up_keys"])


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
