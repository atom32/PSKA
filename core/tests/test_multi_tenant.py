from __future__ import annotations

import json

from pska_core.acl import ACLService
from pska_core.candidates import CandidateWriteError, CandidateWriteService
from pska_core.enums import UserRole
from pska_core.ingest import IngestService
from pska_core.jobs import EXTRACT_ALL, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import User
from pska_core.retrieval import RetrievalService
from pska_core.store import InMemoryKnowledgeStore


TENANT_A = "tenant_a"
TENANT_B = "tenant_b"


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_a", "primary", UserRole.USER, tenant_id=TENANT_A))
    store.add_user(User("user_c", "secondary", UserRole.USER, tenant_id=TENANT_A))
    store.add_user(User("user_b", "primary", UserRole.USER, tenant_id=TENANT_B))
    store.add_user(User("admin_a", "admin", UserRole.ADMIN, tenant_id=TENANT_A))
    store.add_user(User("admin_b", "admin", UserRole.ADMIN, tenant_id=TENANT_B))
    return store


def _payload(*, tenant_id: str, owner_user_id: str, source_id: str = "same-source", text: str = "shared raretenant fact") -> dict:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "tenant_id": tenant_id,
        "source_channel": "manual",
        "record_type": "note",
        "source_id": source_id,
        "owner_user_id": owner_user_id,
        "space_id": "private_primary",
        "visibility": "private",
        "visible_team_ids": [],
        "title": f"Tenant note {tenant_id}",
        "content": {"text": text},
    }


def test_same_content_hash_is_deduped_only_within_tenant_and_owner() -> None:
    store = _store()
    ingest = IngestService(store)

    item_a = ingest.ingest_channel_payload(_payload(tenant_id=TENANT_A, owner_user_id="user_a"))
    item_c = ingest.ingest_channel_payload(_payload(tenant_id=TENANT_A, owner_user_id="user_c"))
    item_b = ingest.ingest_channel_payload(_payload(tenant_id=TENANT_B, owner_user_id="user_b"))
    duplicate_a = ingest.ingest_channel_payload(_payload(tenant_id=TENANT_A, owner_user_id="user_a"))

    assert item_a.content_hash == item_b.content_hash
    assert item_a.content_hash == item_c.content_hash
    assert item_a.source_item_id != item_c.source_item_id
    assert item_a.source_item_id != item_b.source_item_id
    assert duplicate_a.source_item_id == item_a.source_item_id
    assert {item.source_item_id for item in store.list_source_items(tenant_id=TENANT_A)} == {item_a.source_item_id, item_c.source_item_id}
    assert [item.source_item_id for item in store.list_source_items(tenant_id=TENANT_B)] == [item_b.source_item_id]


def test_retrieval_and_acl_are_tenant_scoped() -> None:
    store = _store()
    ingest = IngestService(store)
    item_a = ingest.ingest_channel_payload(_payload(tenant_id=TENANT_A, owner_user_id="user_a", text="alpha raretenant"))
    item_b = ingest.ingest_channel_payload(_payload(tenant_id=TENANT_B, owner_user_id="user_b", text="beta raretenant"))
    acl = ACLService(store)
    retrieval = RetrievalService(store, acl)

    response_a = retrieval.search("raretenant", store.get_user("user_a", tenant_id=TENANT_A))
    response_b = retrieval.search("raretenant", store.get_user("user_b", tenant_id=TENANT_B))

    assert [result.source_item_id for result in response_a.results] == [item_a.source_item_id]
    assert [result.source_item_id for result in response_b.results] == [item_b.source_item_id]
    assert acl.can_read_item(store.get_user("admin_a", tenant_id=TENANT_A), item_a)
    assert not acl.can_read_item(store.get_user("admin_a", tenant_id=TENANT_A), item_b)
    assert acl.can_read_item(store.get_user("admin_b", tenant_id=TENANT_B), item_b)


def test_jobs_claim_only_matching_tenant_scope() -> None:
    store = _store()
    job_a = JobService(store, tenant_id=TENANT_A).submit(EXTRACT_ALL, {"owner_user_id": "user_a"})
    job_b = JobService(store, tenant_id=TENANT_B).submit(EXTRACT_ALL, {"owner_user_id": "user_b"})

    claimed_a = store.claim_next_job(tenant_id=TENANT_A, worker_id="worker_a")
    claimed_b = store.claim_next_job(tenant_id=TENANT_B, worker_id="worker_b")

    assert claimed_a is not None and claimed_a.job_id == job_a.job_id
    assert claimed_b is not None and claimed_b.job_id == job_b.job_id
    assert store.get_job(job_a.job_id).tenant_id == TENANT_A
    assert store.get_job(job_b.job_id).tenant_id == TENANT_B


def test_candidate_write_rejects_cross_tenant_source_refs() -> None:
    store = _store()
    item_b = IngestService(store).ingest_channel_payload(_payload(tenant_id=TENANT_B, owner_user_id="user_b"))

    try:
        CandidateWriteService(store).write_candidates(
            {
                "tenant_id": TENANT_A,
                "owner_user_id": "user_b",
                "source_refs": [{"source_item_id": item_b.source_item_id}],
                "entities": [{"entity_type": "topic", "label": "leak"}],
            }
        )
    except CandidateWriteError as exc:
        assert "known source_items" in str(exc)
    else:
        raise AssertionError("expected CandidateWriteError")


def test_mcp_job_context_infers_tenant_from_job_and_blocks_mismatch() -> None:
    store = _store()
    item_a = IngestService(store).ingest_channel_payload(_payload(tenant_id=TENANT_A, owner_user_id="user_a"))
    job = JobService(store, tenant_id=TENANT_A).submit(
        "digest_via_fastreact",
        {"owner_user_id": "user_a", "source_refs": [{"source_item_id": item_a.source_item_id}]},
    )
    server = MCPServer("postgresql:///unused", store=store)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "pska_job_context", "arguments": {"job_id": job.job_id, "user_id": "user_a"}},
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])

    assert payload["tenant_id"] == TENANT_A
    assert payload["source_items"][0]["source_item_id"] == item_a.source_item_id

    try:
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "pska_job_context", "arguments": {"job_id": job.job_id, "tenant_id": TENANT_B, "user_id": "user_a"}},
            }
        )
    except PermissionError as exc:
        assert "job tenant mismatch" in str(exc)
    else:
        raise AssertionError("expected PermissionError")
