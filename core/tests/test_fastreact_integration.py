from __future__ import annotations

import base64
from datetime import timedelta
import hashlib
import hmac
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import threading

from http.server import ThreadingHTTPServer

from pska_core.acl import ACLService
from pska_core.agentic_service import AgenticServiceError, _agentic_messages
from pska_core.api import PSKAApi, PSKARequestHandler
from pska_core.auth import context_from_headers
from pska_core.candidates import CandidateWriteService
from pska_core.cli import service_check
from pska_core.config import AuthConfig, FilesConfig, PSKAConfig, ServiceConfig, WorkspaceConfig
from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, Visibility
from pska_core.fastreact_client import FastreactError, HttpFastreactClient, FastreactConfig
import pska_core.fastreact_client as fastreact_module
from pska_core.graph_store import PostgresGraphStore
from pska_core.hypergraph import HypergraphService
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, EXTRACT_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import AgentMemory, ConnectorState, DigestNote, DiscoveryItem, Entity, KnowledgeClaim, ReviewItem, SourceRef, User, UserProfileCard, utc_now
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.serde import to_jsonable
from pska_core.store import InMemoryKnowledgeStore


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_fastreact_client_builds_pska_metadata(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"type": "chat.completion", "run_id": "run_123", "content": "ok"})

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(FastreactConfig(url="http://fastreact.test", service_token="token", timeout_seconds=7))

    response = client.chat_completion(
        messages=[{"role": "user", "content": "hello"}],
        user_id="user_primary",
        purpose="extract",
        job_id="job_123",
        scope={"source_item_ids": ["src_1"]},
        model="deepseek-v4-flash",
        temperature=0.3,
        top_p=0.9,
        max_tokens=4096,
    )

    assert response["run_id"] == "run_123"
    assert captured["url"] == "http://fastreact.test/v1/chat/completions"
    assert captured["timeout"] == 7
    assert captured["headers"]["X-fastreact-service-token"] == "token"
    assert captured["payload"]["user_key"] == "pska:user_primary"
    assert captured["payload"]["model"] == "deepseek-v4-flash"
    assert captured["payload"]["temperature"] == 0.3
    assert captured["payload"]["top_p"] == 0.9
    assert captured["payload"]["max_tokens"] == 4096
    assert captured["payload"]["metadata"] == {
        "caller": "pska",
        "purpose": "extract",
        "pska_user_id": "user_primary",
        "pska_job_id": "job_123",
        "scope": {"source_item_ids": ["src_1"]},
    }
    assert "max_tokens" not in captured["payload"]["metadata"]
    assert "temperature" not in captured["payload"]["metadata"]
    assert "top_p" not in captured["payload"]["metadata"]


def test_fastreact_client_forwards_pska_tenant_identity(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"run_id": "run_456"})

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(FastreactConfig(url="http://fastreact.test", service_token="token"))

    client.create_run(
        messages=[{"role": "user", "content": "hello"}],
        user_id="user_primary",
        tenant_id="tenant_acme",
        purpose="agentic_search",
    )

    assert captured["payload"]["user_key"] == "pska:user_primary"
    assert captured["payload"]["metadata"]["tenant_key"] == "tenant_acme"
    assert captured["payload"]["metadata"]["pska_tenant_id"] == "tenant_acme"
    assert captured["headers"]["X-fastreact-user-key"] == "pska:user_primary"
    assert captured["headers"]["X-fastreact-tenant-key"] == "tenant_acme"
    assert captured["headers"]["X-fastreact-auth-provider"] == "pska"


def test_fastreact_client_uses_authnode_jwt_for_tenant_identity(monkeypatch) -> None:
    calls = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        calls.append({"url": request.full_url, "headers": dict(request.header_items()), "payload": body, "timeout": timeout})
        if request.full_url == "http://authnode.test/v1/token":
            return FakeResponse({"access_token": "jwt-fastreact", "expires_at": "2030-01-01T00:00:00+00:00"})
        if request.full_url == "http://fastreact.test/v1/runs":
            return FakeResponse({"run_id": "run_authnode"})
        if request.full_url == "http://fastreact.test/v1/runs/run_authnode":
            return FakeResponse({"status": "completed"})
        if request.full_url == "http://fastreact.test/v1/runs/run_authnode/events?limit=500":
            return FakeResponse({"events": []})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(
        FastreactConfig(
            url="http://fastreact.test",
            service_token="service-token",
            timeout_seconds=9,
            authnode_url="http://authnode.test",
            authnode_admin_token="admin-token",
            authnode_audience="fastreact",
            authnode_token_ttl_seconds=600,
        )
    )

    created = client.create_run(
        messages=[{"role": "user", "content": "hello"}],
        user_id="user_primary",
        tenant_id="tenant_acme",
        purpose="agentic_search",
    )
    snapshot = client.wait_for_run(str(created["run_id"]))
    events = client.run_events(str(created["run_id"]))

    assert snapshot["status"] == "completed"
    assert events["events"] == []
    token_call = calls[0]
    assert token_call["url"] == "http://authnode.test/v1/token"
    assert token_call["headers"]["X-authnode-admin-token"] == "admin-token"
    assert token_call["payload"] == {
        "user_key": "pska:user_primary",
        "audience": "fastreact",
        "tenant_id": "tenant_acme",
        "ttl_seconds": 600,
    }
    fastreact_calls = calls[1:]
    assert [call["url"] for call in fastreact_calls] == [
        "http://fastreact.test/v1/runs",
        "http://fastreact.test/v1/runs/run_authnode",
        "http://fastreact.test/v1/runs/run_authnode/events?limit=500",
    ]
    for call in fastreact_calls:
        assert call["headers"]["Authorization"] == "Bearer jwt-fastreact"
        assert call["headers"]["X-fastreact-service-token"] == "service-token"


def test_fastreact_client_applies_config_generation_options_to_runs(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"run_id": "run_456"})

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(
        FastreactConfig(
            url="http://fastreact.test",
            model="deepseek-v4-flash",
            temperature=0.2,
            top_p=0.8,
            max_tokens=2048,
        )
    )

    response = client.create_run(
        messages=[{"role": "user", "content": "hello"}],
        user_id="user_primary",
        purpose="agentic_search",
        skills=["pska_answer_with_citations"],
    )

    assert response["run_id"] == "run_456"
    assert captured["url"] == "http://fastreact.test/v1/runs"
    assert captured["payload"]["model"] == "deepseek-v4-flash"
    assert captured["payload"]["temperature"] == 0.2
    assert captured["payload"]["top_p"] == 0.8
    assert captured["payload"]["max_tokens"] == 2048
    assert captured["payload"]["skills"] == ["pska_answer_with_citations"]
    assert captured["payload"]["metadata"]["purpose"] == "agentic_search"


def test_fastreact_client_sends_empty_skills_to_disable_autoselection(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"run_id": "run_no_skills"})

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(FastreactConfig(url="http://fastreact.test"))

    response = client.create_run(
        messages=[{"role": "user", "content": "hello"}],
        user_id="user_primary",
        purpose="agentic_search",
        skills=[],
    )

    assert response["run_id"] == "run_no_skills"
    assert captured["payload"]["skills"] == []


def test_agentic_search_prompt_routes_pska_queries_to_pska_skill_tools() -> None:
    messages = _agentic_messages("徐大为的简历都说了什么？")
    joined = "\n".join(message["content"] for message in messages)

    assert "use only PSKA MCP tools" in joined
    assert "do not call read_file" in joined
    assert "exec" in joined
    assert "retrieve source evidence through PSKA tools" in joined
    assert "4-8 concrete bullets" in joined


def test_agentic_search_prompt_includes_pska_tenant_identity() -> None:
    messages = _agentic_messages("What is known?", tenant_id="tenant_acme", user_id="alice")
    system = messages[0]["content"]

    assert "tenant_id='tenant_acme'" in system
    assert "user_id='alice'" in system
    assert "Every PSKA MCP tool call must include exactly these tenant_id and user_id" in system


def test_fastreact_ready_reports_missing_pska_tools(monkeypatch) -> None:
    def fake_urlopen(request, timeout=None):
        if request.full_url.endswith("/health"):
            return FakeResponse({"ok": True})
        if request.full_url.endswith("/ready"):
            return FakeResponse({"ok": True})
        if request.full_url.endswith("/v1/tools"):
            return FakeResponse({"tools": [{"name": "pska_search"}, {"name": "other_tool"}]})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(FastreactConfig(url="http://fastreact.test"))

    ready = client.ready()

    assert ready["ok"] is True
    assert ready["tool_names"] == ["other_tool", "pska_search"]
    assert ready["pska_tools_loaded"] is False
    assert ready["missing_pska_tools"] == ["pska_index_status", "pska_job_context", "pska_write_candidates"]


def test_fastreact_ready_accepts_namespaced_pska_tools(monkeypatch) -> None:
    namespaced_tools = [
        "pska_pska_search",
        "pska_pska_index_status",
        "pska_pska_job_context",
        "pska_pska_write_candidates",
    ]

    def fake_urlopen(request, timeout=None):
        if request.full_url.endswith("/health"):
            return FakeResponse({"ok": True})
        if request.full_url.endswith("/ready"):
            return FakeResponse({"ok": True})
        if request.full_url.endswith("/v1/tools"):
            return FakeResponse({"tools": [{"name": name} for name in namespaced_tools]})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(fastreact_module, "urlopen", fake_urlopen)
    client = HttpFastreactClient(FastreactConfig(url="http://fastreact.test"))

    ready = client.ready()

    assert ready["pska_tools_loaded"] is True
    assert ready["missing_pska_tools"] == []
    assert set(ready["normalized_pska_tool_names"]) == {
        "pska_index_status",
        "pska_job_context",
        "pska_search",
        "pska_write_candidates",
        "pska_pska_index_status",
        "pska_pska_job_context",
        "pska_pska_search",
        "pska_pska_write_candidates",
    }


def test_fastreact_pska_service_config_keeps_builtin_tools_under_fastreact_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(root / "scripts" / "fastreact-pska-service-config"), "--mcp-transport", "http", "--print"],
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(result.stdout)
    policy = config["policy"]
    pska_tools = policy["tenant_rules"]["pska"]["tools"]

    assert "default_action" not in policy
    assert pska_tools["exec"] == "deny"
    assert pska_tools["read_file"] == "deny"
    assert pska_tools["write_file"] == "require_approval"
    assert pska_tools["edit_file"] == "require_approval"
    assert pska_tools["pska_pska_search"] == "allow"


def test_fastreact_job_records_run_id_and_event() -> None:
    store = _store()
    store.upsert_source_item(_source_item())
    fastreact = FakeFastreact({"run_id": "run_extract", "content": "done"})
    service = JobService(store, fastreact=fastreact)
    job = service.submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary", "tenant_id": "tenant_acme"}, max_attempts=1)

    completed = service.run_next()

    assert completed is not None
    assert completed.status == "succeeded"
    assert fastreact.calls[0]["tenant_id"] == "tenant_acme"
    assert completed.result["fastreact"]["run_id"] == "run_extract"
    events = store.list_job_events(job.job_id)
    assert [event.event_type for event in events] == ["queued", "started", "execute", "fastreact_submitted", "heartbeat", "succeeded"]
    assert events[-3].detail["run_id"] == "run_extract"
    assert events[-2].detail["external_run_id"] == "run_extract"
    assert completed.external_run_id == "run_extract"


def test_fastreact_unavailable_marks_job_failed_and_retryable() -> None:
    store = _store()
    service = JobService(store, fastreact=FailingFastreact())
    job = service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)

    failed = service.run_next()

    assert failed is not None
    assert failed.status == "failed"
    assert "Fastreact down" in (failed.error or "")
    retried = store.retry_job(job.job_id)
    assert retried.status == "queued"


def test_api_ready_reports_fastreact_degraded(monkeypatch) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = object.__new__(PSKAApi)
    api.store = _store()
    api.mcp = MCPServer("postgresql:///unused", store=api.store)
    api.agentic_service = DownAgenticService()

    ready = api.ready()

    assert ready["ok"] is True
    assert ready["checks"]["database"]["ok"] is True
    assert ready["checks"]["schema"]["ok"] is True
    assert ready["checks"]["mcp"]["ok"] is True
    assert "pska_search" in ready["checks"]["mcp"]["tools"]
    assert ready["checks"]["agentic_service"]["ok"] is False


def test_api_ready_reports_job_worker_observability(monkeypatch) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = _api()
    api.agentic_service = DownAgenticService()
    service = JobService(api.store)
    stale_job = service.submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary"})
    failed_job = service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)
    running = api.store.claim_next_job(worker_id="worker_obs", lease_seconds=30)
    assert running.job_id == stale_job.job_id
    running.leased_until = utc_now() - timedelta(seconds=5)
    api.store.claim_next_job(worker_id="worker_obs", lease_seconds=30)
    api.store.fail_job(failed_job.job_id, "boom", retryable=False)

    ready = api.ready()
    jobs = ready["checks"]["jobs"]

    assert jobs["ok"] is True
    assert jobs["by_status"]["running"] == 1
    assert jobs["by_status"]["failed"] == 1
    assert jobs["by_type"]["extract_via_fastreact"] == 1
    assert jobs["active_worker_ids"] == ["worker_obs"]
    assert jobs["running_stale_count"] == 1
    assert jobs["stale_running"][0]["job_id"] == stale_job.job_id
    assert jobs["recent_failed"][0]["job_id"] == failed_job.job_id


def test_http_mcp_initialize_and_tool_list_share_stdio_server() -> None:
    api = _api()

    initialized = api.mcp_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    tools = api.mcp_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    notification = api.mcp_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    assert initialized["result"]["serverInfo"]["name"] == "pska-core"
    names = [tool["name"] for tool in tools["result"]["tools"]]
    assert {"pska_search", "pska_index_status"} <= set(names)
    assert "pska_agentic_search" not in names
    assert notification is None


def test_http_mcp_tool_call_search_returns_content_json() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-http-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "HTTP MCP note",
            "content": {"text": "http mcp searchable phrase"},
        }
    )

    response = api.mcp_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "pska_search", "arguments": {"query": "searchable", "user_id": "user_primary"}},
        }
    )

    content = response["result"]["content"][0]
    payload = json.loads(content["text"])
    assert content["type"] == "text"
    assert payload["results"][0]["title"] == "HTTP MCP note"


def test_http_mcp_unknown_method_returns_jsonrpc_error() -> None:
    response = _api().mcp_jsonrpc({"jsonrpc": "2.0", "id": 4, "method": "nope", "params": {}})

    assert response["id"] == 4
    assert response["error"]["code"] == -32601


def test_http_routes_cover_mcp_jobs_and_review_contract() -> None:
    api = _api()
    job = JobService(api.store).submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary"})
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_http_approve",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Approve me",
            proposal={"profile_delta": {"style": "concise"}},
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_http_reject",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Reject me",
            proposal={"profile_delta": {"style": "verbose"}},
        )
    )
    with _http_server(api) as base_url:
        initialize_status, initialize = _http_json(
            base_url,
            "POST",
            "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        notification_status, notification = _http_json(
            base_url,
            "POST",
            "/mcp",
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        job_status, job_payload = _http_json(base_url, "GET", f"/jobs/{job.job_id}")
        approve_status, approve_payload = _http_json(
            base_url,
            "POST",
            "/review-items/rev_http_approve/approve",
            {"actor_user_id": "user_primary"},
        )
        reject_status, reject_payload = _http_json(
            base_url,
            "POST",
            "/review-items/rev_http_reject/reject",
            {"actor_user_id": "user_primary", "reason": "no"},
        )

    assert initialize_status == 200
    assert initialize["result"]["serverInfo"]["name"] == "pska-core"
    assert notification_status == 204
    assert notification is None
    assert job_status == 200
    assert job_payload["job"]["job_id"] == job.job_id
    assert approve_status == 200
    assert approve_payload["review_item"]["status"] == "approved"
    assert reject_status == 200
    assert reject_payload["review_item"]["status"] == "rejected"


def test_http_route_ingests_connector_record_contract() -> None:
    api = _api()
    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/connectors/records",
            {
                "schema_version": "pska.connector_record.v1",
                "connector_id": "browser",
                "external_id": "https://example.test/article",
                "source_uri": "https://example.test/article",
                "record_type": "web_page",
                "title": "Example Article",
                "body": "Browser connector captures readable article text.",
                "owner_user_id": "user_primary",
                "space_id": "private_primary",
                "visibility": "private",
                "permission_metadata": {"capture_mode": "current_page"},
                "scan_cursor": "bookmark_cursor_1",
            },
        )

    assert status == 200
    assert payload["source_item"]["source_channel"] == "browser"
    assert payload["source_item"]["source_id"] == "https://example.test/article"
    assert payload["channel_payload"]["extra"]["connector"]["scan_cursor"] == "bookmark_cursor_1"
    assert payload["channel_payload"]["extra"]["permission_metadata"]["capture_mode"] == "current_page"


def test_http_routes_manage_connector_state_contract() -> None:
    api = _api()
    with _http_server(api) as base_url:
        upsert_status, upsert = _http_json(
            base_url,
            "POST",
            "/connectors/states",
            {
                "schema_version": "pska.connector_state.v1",
                "connector_id": "files",
                "owner_user_id": "user_primary",
                "enabled": True,
                "scan_cursor": "cursor_1",
                "sync_status": "succeeded",
                "permission_scope": {"roots": ["/Users/example/notes"]},
            },
        )
        list_status, listed = _http_json(base_url, "GET", "/connectors/states?owner_user_id=user_primary&connector_id=files")
        show_status, shown = _http_json(base_url, "GET", "/connectors/states/conn_user_primary_files")

    assert upsert_status == 200
    assert upsert["connector_state"]["connector_state_id"] == "conn_user_primary_files"
    assert upsert["connector_state"]["scan_cursor"] == "cursor_1"
    assert list_status == 200
    assert [state["connector_state_id"] for state in listed["connector_states"]] == ["conn_user_primary_files"]
    assert show_status == 200
    assert shown["connector_state"]["permission_scope"]["roots"] == ["/Users/example/notes"]


def test_http_routes_cover_digest_worker_contract() -> None:
    api = _api()
    sources = [
        IngestService(api.store).ingest_channel_payload(
            {
                "schema_version": "pska.channel_ingest.v1",
                "source_channel": "manual",
                "record_type": "note",
                "source_id": f"digest-route-note-{index}",
                "owner_user_id": "user_primary",
                "space_id": "private_primary",
                "visibility": "private",
                "title": f"Digest route note {index}",
                "content": {"text": f"PSKA digest workers write grounded candidates {index}."},
            }
        )
        for index in range(2)
    ]
    job = JobService(api.store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "batch_size": 1,
            "source_refs": [{"source_item_id": source.source_item_id} for source in sources],
        },
    )
    with _http_server(api) as base_url:
        lease_status, lease = _http_json(base_url, "POST", f"/jobs/{job.job_id}/lease", {"worker_id": "fastreact-worker", "lease_seconds": 120})
        batch_status, batch = _http_json(base_url, "GET", f"/digest/batches/{job.job_id}?limit=1")
        next_batch_status, next_batch = _http_json(base_url, "GET", f"/digest/batches/{job.job_id}?cursor={batch['next_cursor']}&limit=1")
        candidates_status, candidates = _http_json(
            base_url,
            "POST",
            "/digest/candidates",
            {
                "schema_version": "pska.candidates.v1",
                "owner_user_id": "user_primary",
                "job_id": job.job_id,
                "source_refs": [{"source_item_id": sources[0].source_item_id}],
                "entities": [{"entity_type": "project", "label": "PSKA"}],
            },
        )
        complete_status, complete = _http_json(base_url, "POST", f"/jobs/{job.job_id}/complete", {"result": {"ok": True}})

    assert lease_status == 200
    assert lease["job"]["status"] == "running"
    assert "pska_write_candidates" in lease["allowed_tools"]
    assert batch_status == 200
    assert batch["source_items"][0]["source_item_id"] == sources[0].source_item_id
    assert batch["has_more"] is True
    assert batch["next_cursor"] == "1"
    assert next_batch_status == 200
    assert next_batch["source_items"][0]["source_item_id"] == sources[1].source_item_id
    assert next_batch["has_more"] is False
    assert candidates_status == 200
    assert candidates["summary"]["entities"]
    assert candidates["summary"]["schema_version"] == "pska.candidates.v1"
    assert complete_status == 200
    assert complete["job"]["status"] == "succeeded"


def test_http_routes_cover_job_ops_filters_stats_cancel_and_recover() -> None:
    api = _api()
    service = JobService(api.store)
    digest = service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary"}, priority=5)
    extract = service.submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary"}, priority=10)
    running = api.store.claim_next_job(worker_id="worker_ops", lease_seconds=30)
    assert running is not None
    assert running.job_id == extract.job_id
    running.started_at = utc_now() - timedelta(seconds=120)
    running.leased_until = utc_now() - timedelta(seconds=10)

    with _http_server(api) as base_url:
        list_status, listed = _http_json(base_url, "GET", "/jobs?status=queued&job_type=digest_via_fastreact&limit=5")
        stats_status, stats = _http_json(base_url, "GET", "/jobs/stats")
        cancel_status, canceled = _http_json(base_url, "POST", f"/jobs/{digest.job_id}/cancel", {"reason": "covered by newer job"})
        recover_status, recovered = _http_json(base_url, "POST", "/jobs/recover-stale", {"max_age_seconds": 60})

    assert list_status == 200
    assert [job["job_id"] for job in listed["jobs"]] == [digest.job_id]
    assert stats_status == 200
    assert stats["stats"]["by_status"]["queued"] == 1
    assert stats["stats"]["by_status"]["running"] == 1
    assert stats["stats"]["running_stale_count"] == 1
    assert cancel_status == 200
    assert canceled["job"]["status"] == "canceled"
    assert canceled["job"]["error"] == "covered by newer job"
    assert recover_status == 200
    assert recovered["recovered"][0]["job_id"] == extract.job_id
    assert recovered["recovered"][0]["status"] == "queued"


def test_http_request_logs_include_request_job_and_source_refs(capsys) -> None:
    api = _api()
    job = JobService(api.store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": "src_log"}],
        },
    )

    with _http_server(api) as base_url:
        conn = HTTPConnection(base_url, timeout=5)
        conn.request("GET", f"/jobs/{job.job_id}", headers={"X-PSKA-Request-Id": "req-test-123"})
        response = conn.getresponse()
        response.read()
        request_id = response.getheader("x-pska-request-id")
        conn.close()

        post_status, _payload = _http_json(
            base_url,
            "POST",
            "/digest/schedule",
            {
                "owner_user_id": "user_primary",
                "source_refs": [{"source_item_id": "src_a"}],
                "scope": {"source_item_ids": ["src_b"]},
            },
            headers={"X-Request-Id": "req-test-456"},
        )

    logs = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]

    assert response.status == 200
    assert request_id == "req-test-123"
    assert post_status == 200
    assert logs[0]["event"] == "pska.http_request"
    assert logs[0]["request_id"] == "req-test-123"
    assert logs[0]["path"] == f"/jobs/{job.job_id}"
    assert logs[0]["job_id"] == job.job_id
    assert logs[0]["response_answer_chars"] == 0
    assert "response_event_count" in logs[0]
    assert logs[1]["request_id"] == "req-test-456"
    assert logs[1]["path"] == "/digest/schedule"
    assert logs[1]["source_item_ids_count"] == 2


def test_metrics_report_embedding_coverage_and_connector_freshness() -> None:
    api = _api()
    api.config = PSKAConfig.from_dict({"embedding": {"provider": "fake-bge", "model": "fake-model"}})
    first = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "metrics-note-1",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Metrics note 1",
            "content": {"text": "Metrics coverage note one."},
        }
    )
    second = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "fastreact",
            "record_type": "conversation",
            "source_id": "metrics-note-2",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Metrics note 2",
            "content": {"text": "Metrics coverage note two."},
        }
    )
    first_chunk = api.store.list_chunks_for_sources({first.source_item_id})[0]
    api.store.update_chunk_embedding(first_chunk.chunk_id, [1.0, 0.0, 1.0], provider="fake-bge", model="fake-model")

    metrics = api.metrics()
    ready = api.ready()
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/metrics")

    assert metrics["embedding"]["total_chunks"] == 2
    assert metrics["embedding"]["embedded_chunks"] == 1
    assert metrics["embedding"]["missing_chunks"] == 1
    assert metrics["embedding"]["coverage"] == 0.5
    assert metrics["connectors"]["source_channel_count"] == 2
    assert metrics["connectors"]["source_channels"]["manual"]["latest_source_item_id"] == first.source_item_id
    assert metrics["connectors"]["source_channels"]["fastreact"]["latest_source_item_id"] == second.source_item_id
    assert ready["checks"]["metrics"]["ok"] is True
    assert ready["checks"]["metrics"]["embedding"]["coverage"] == 0.5
    assert status == 200
    assert payload["embedding"]["embedded_chunks"] == 1


def test_digest_schedule_creates_backlog_and_skips_active_sources() -> None:
    api = _api()
    sources = [
        IngestService(api.store).ingest_channel_payload(
            {
                "schema_version": "pska.channel_ingest.v1",
                "source_channel": "manual",
                "record_type": "note",
                "source_id": f"digest-schedule-note-{index}",
                "owner_user_id": "user_primary",
                "space_id": "private_primary",
                "visibility": "private",
                "title": f"Digest schedule note {index}",
                "content": {"text": f"Schedule digest source {index}."},
            }
        )
        for index in range(3)
    ]

    first = api.schedule_digest({"owner_user_id": "user_primary", "limit": 2, "batch_size": 1, "priority": 7})
    second = api.schedule_digest({"owner_user_id": "user_primary", "limit": 3})
    forced = api.schedule_digest({"owner_user_id": "user_primary", "source_item_ids": [sources[0].source_item_id], "force": True})
    stats = api.job_stats()["stats"]

    assert first["job"]["job_type"] == DIGEST_VIA_FASTREACT
    assert first["job"]["priority"] == 7
    assert first["job"]["payload"]["batch_size"] == 1
    assert len(first["scheduled_source_item_ids"]) == 2
    assert first["policy"]["max_source_items"] == 2
    assert {item["reason"] for item in first["selected_source_items"]} == {"new_or_triggered_source"}
    assert first["skipped_source_item_ids"] == [sources[0].source_item_id]
    assert first["skipped_source_items"][0]["reason"] == "limit_reached"
    assert second["scheduled_source_item_ids"] == [sources[0].source_item_id]
    assert sorted(second["skipped_source_item_ids"]) == sorted(source.source_item_id for source in sources[1:])
    assert {item["reason"] for item in second["skipped_source_items"]} == {"active_digest_job"}
    assert forced["scheduled_source_item_ids"] == [sources[0].source_item_id]
    assert stats["digest_backlog"]["jobs"] == 3
    assert stats["digest_backlog"]["source_items"] == 3


def test_digest_schedule_skips_failed_sources_unless_forced() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "digest-failed-covered-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Digest failed covered note",
            "content": {"text": "A failed digest should not be auto-scheduled forever."},
        }
    )
    job = JobService(api.store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source.source_item_id}],
            "scope": {"source_item_ids": [source.source_item_id]},
        },
        max_attempts=1,
    )
    api.store.fail_job(job.job_id, "failed once", retryable=False)

    automatic = api.schedule_digest({"owner_user_id": "user_primary", "limit": 1})
    forced = api.schedule_digest({"owner_user_id": "user_primary", "source_item_ids": [source.source_item_id], "force": True})

    assert automatic["job"] is None
    assert automatic["scheduled_source_item_ids"] == []
    assert automatic["skipped_source_item_ids"] == [source.source_item_id]
    assert automatic["skipped_source_items"][0]["reason"] == "failed_digest_job_requires_force_or_new_trigger"
    assert forced["scheduled_source_item_ids"] == [source.source_item_id]


def test_digest_schedule_skips_succeeded_sources_until_forced_or_new_trigger() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "digest-succeeded-covered-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Digest succeeded covered note",
            "content": {"text": "A successful digest should not be scheduled forever."},
        }
    )
    job = JobService(api.store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source.source_item_id}],
            "scope": {"source_item_ids": [source.source_item_id]},
        },
    )
    api.store.finish_job(job.job_id, {"ok": True})

    automatic = api.schedule_digest({"owner_user_id": "user_primary", "limit": 1})
    forced = api.schedule_digest({"owner_user_id": "user_primary", "source_item_ids": [source.source_item_id], "force": True})

    assert automatic["job"] is None
    assert automatic["scheduled_source_item_ids"] == []
    assert automatic["skipped_source_item_ids"] == [source.source_item_id]
    assert automatic["skipped_source_items"][0]["reason"] == "completed_digest_job"
    assert automatic["policy"]["successful_source_repeat"].startswith("skip completed")
    assert forced["scheduled_source_item_ids"] == [source.source_item_id]


def test_digest_schedule_reschedules_source_changed_after_succeeded_digest() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "digest-succeeded-then-changed-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Digest succeeded then changed note",
            "content": {"text": "The first version was already digested."},
        }
    )
    job = JobService(api.store).submit(
        DIGEST_VIA_FASTREACT,
        {
            "owner_user_id": "user_primary",
            "source_refs": [{"source_item_id": source.source_item_id}],
            "scope": {"source_item_ids": [source.source_item_id]},
        },
    )
    finished = api.store.finish_job(job.job_id, {"ok": True})
    source.updated_at = finished.finished_at + timedelta(seconds=1)

    automatic = api.schedule_digest({"owner_user_id": "user_primary", "limit": 1})

    assert automatic["scheduled_source_item_ids"] == [source.source_item_id]
    assert automatic["selected_source_items"][0]["reason"] == "source_changed_since_last_digest"
    assert automatic["selected_source_items"][0]["covering_job"]["job_id"] == job.job_id


def test_digest_schedule_respects_job_quota_unless_forced() -> None:
    api = _api()
    sources = [
        IngestService(api.store).ingest_channel_payload(
            {
                "schema_version": "pska.channel_ingest.v1",
                "source_channel": "manual",
                "record_type": "note",
                "source_id": f"digest-quota-note-{index}",
                "owner_user_id": "user_primary",
                "space_id": "private_primary",
                "visibility": "private",
                "title": f"Digest quota note {index}",
                "content": {"text": f"Quota source {index}."},
            }
        )
        for index in range(2)
    ]

    first = api.schedule_digest({"owner_user_id": "user_primary", "limit": 1, "quota_window_seconds": 3600, "max_jobs_per_window": 1})
    limited = api.schedule_digest({"owner_user_id": "user_primary", "limit": 1, "quota_window_seconds": 3600, "max_jobs_per_window": 1})
    forced = api.schedule_digest(
        {
            "owner_user_id": "user_primary",
            "source_item_ids": [sources[1].source_item_id],
            "quota_window_seconds": 3600,
            "max_jobs_per_window": 1,
            "force": True,
        }
    )

    assert first["quota"]["enabled"] is True
    assert first["quota_limited"] is False
    assert limited["job"] is None
    assert limited["quota_limited"] is True
    assert limited["quota"]["jobs_in_window"] == 1
    assert forced["scheduled_source_item_ids"] == [sources[1].source_item_id]
    assert forced["quota"]["enabled"] is False


def test_http_route_covers_digest_schedule() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "digest-schedule-http-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Digest schedule HTTP note",
            "content": {"text": "Schedule this through HTTP."},
        }
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "POST", "/digest/schedule", {"owner_user_id": "user_primary", "limit": 1})

    assert status == 200
    assert payload["scheduled_source_item_ids"] == [source.source_item_id]
    assert payload["job"]["job_type"] == DIGEST_VIA_FASTREACT


def test_http_route_covers_files_sync(tmp_path: Path) -> None:
    api = _api()
    (tmp_path / "note.md").write_text("PSKA should sync this file.", encoding="utf-8")

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/files/sync",
            {"owner_user_id": "user_primary", "roots": [str(tmp_path)], "skip_twitter_archives": True},
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["totals"]["scanned"] == 1
    assert payload["totals"]["ingested"] == 1
    assert payload["totals"]["failed"] == 0


def test_http_route_covers_files_sync_with_empty_twitter_archive(tmp_path: Path) -> None:
    notes_root = tmp_path / "notes"
    workspace_root = tmp_path / "workspace"
    notes_root.mkdir()
    (workspace_root / "twitter_archive").mkdir(parents=True)
    (notes_root / "note.md").write_text("PSKA should sync this file and check twitter archive.", encoding="utf-8")
    api = _api()
    api.config = PSKAConfig(files=FilesConfig(roots=(notes_root,)), workspace=WorkspaceConfig(root=workspace_root))

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/files/sync",
            {"owner_user_id": "user_primary"},
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["twitter_archives"]["imported"] == 0
    assert payload["twitter_archives"]["skipped"] == 0
    assert payload["totals"]["scanned"] == 1


def test_http_route_covers_digest_now_skip_sync() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "digest-now-http-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Digest now HTTP note",
            "content": {"text": "Schedule this through digest-now."},
        }
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/digest/now",
                {"owner_user_id": "user_primary", "limit": 1, "skip_sync": True, "max_worker_runs": 0},
        )

    assert status == 200
    assert payload["digest"]["scheduled_source_item_ids"] == [source.source_item_id]
    assert payload["summary"]["scheduled_source_items"] == 1


def test_digest_schedule_agent_service_requires_represented_user_for_private_owner() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "digest-schedule-agent-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Digest schedule agent note",
            "content": {"text": "Agent should need representation to schedule this."},
        }
    )

    with _http_server(api) as base_url:
        no_rep_status, no_rep = _http_json(
            base_url,
            "POST",
            "/digest/schedule",
            {"owner_user_id": "user_primary"},
            headers={"X-PSKA-Caller": "agent_service"},
        )
        rep_status, rep = _http_json(
            base_url,
            "POST",
            "/digest/schedule",
            {"owner_user_id": "user_primary"},
            headers={"X-PSKA-Caller": "agent_service", "X-PSKA-Represented-User-Id": "user_primary"},
        )

    assert no_rep_status == 200
    assert no_rep["owner_user_id"] == "agent_service"
    assert no_rep["job"] is None
    assert no_rep["scheduled_source_item_ids"] == []
    assert rep_status == 200
    assert rep["owner_user_id"] == "user_primary"
    assert rep["scheduled_source_item_ids"] == [source.source_item_id]


def test_service_token_protects_non_health_routes() -> None:
    api = _api(service_token="secret")
    with _http_server(api) as base_url:
        health_status, health = _http_json(base_url, "GET", "/health")
        ready_status, ready = _http_json(base_url, "GET", "/ready")
        authed_status, authed = _http_json(
            base_url,
            "GET",
            "/ready",
            headers={"X-PSKA-Service-Token": "secret"},
        )
        bearer_status, bearer = _http_json(
            base_url,
            "GET",
            "/ready",
            headers={"Authorization": "Bearer secret"},
        )

    assert health_status == 200
    assert health["ok"] is True
    assert ready_status == 401
    assert "service token" in ready["error"]
    assert authed_status == 200
    assert authed["ok"] is True
    assert bearer_status == 200
    assert bearer["ok"] is True


def test_trusted_headers_auth_uses_fastreact_identity_aliases() -> None:
    api = _api(auth=AuthConfig(mode="trusted_headers"))
    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/ingest/channel-payload",
            _minimal_ingest_payload("trusted-alias-note"),
            headers={
                "X-FastReAct-User-Key": "pska:user_primary",
                "X-FastReAct-Tenant-Key": "tenant_acme",
                "X-FastReAct-Roles": "admin,writer",
                "X-FastReAct-Auth-Provider": "sso",
            },
        )

    assert status == 200
    assert payload["tenant_id"] == "tenant_acme"
    assert payload["owner_user_id"] == "user_primary"
    assert api.store.list_source_items(tenant_id="tenant_acme")[0].source_id == "trusted-alias-note"


def test_trusted_headers_auth_requires_identity_header() -> None:
    api = _api(auth=AuthConfig(mode="trusted_headers"))
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/ready")

    assert status == 401
    assert "trusted identity" in payload["error"]


def test_jwt_auth_maps_claims_to_request_context() -> None:
    token = _jwt(
        {
            "sub": "pska:user_primary",
            "tenant_id": "tenant_jwt",
            "tenant_key": "tenant_jwt",
            "tenant": "tenant_jwt",
            "org_id": "tenant_jwt",
            "user_id": "user_primary",
            "user_key": "pska:user_primary",
            "name": "Primary User",
            "email": "primary@example.com",
            "groups": ["team-a"],
            "roles": ["admin"],
            "provider": "authnode",
            "iss": "issuer",
            "aud": "pska",
        },
        secret="jwt-secret",
    )
    api = _api(auth=AuthConfig(mode="jwt", jwt_secret="jwt-secret", jwt_issuer="issuer", jwt_audience="pska"))
    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/ingest/channel-payload",
            _minimal_ingest_payload("jwt-note"),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert status == 200
    assert payload["tenant_id"] == "tenant_jwt"
    assert api.store.list_source_items(tenant_id="tenant_jwt")[0].source_id == "jwt-note"
    context = context_from_headers(
        {"Authorization": f"Bearer {token}"},
        auth_config=AuthConfig(mode="jwt", jwt_secret="jwt-secret", jwt_issuer="issuer", jwt_audience="pska"),
    )
    assert context.tenant_id == "tenant_jwt"
    assert context.user_id == "user_primary"
    assert context.subject == "pska:user_primary"
    assert context.roles == ["admin"]
    assert context.groups == ["team-a"]
    assert context.auth_provider == "authnode"


def test_jwt_auth_requires_bearer_token() -> None:
    api = _api(auth=AuthConfig(mode="jwt", jwt_secret="jwt-secret"))
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/ready")

    assert status == 401
    assert "Bearer JWT required" in payload["error"]


def test_jwt_auth_rejects_invalid_signature() -> None:
    token = _jwt({"sub": "user_primary", "tenant_id": "tenant_jwt"}, secret="wrong")
    api = _api(auth=AuthConfig(mode="jwt", jwt_secret="jwt-secret"))
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/ready", headers={"Authorization": f"Bearer {token}"})

    assert status == 401
    assert "signature" in payload["error"]


def test_jwt_auth_rejects_wrong_issuer() -> None:
    token = _jwt({"sub": "pska:user_primary", "tenant_id": "tenant_jwt", "iss": "wrong"}, secret="jwt-secret")
    api = _api(auth=AuthConfig(mode="jwt", jwt_secret="jwt-secret", jwt_issuer="issuer"))
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/ready", headers={"Authorization": f"Bearer {token}"})

    assert status == 401
    assert "issuer" in payload["error"]


def test_jwt_auth_rejects_wrong_audience() -> None:
    token = _jwt({"sub": "pska:user_primary", "tenant_id": "tenant_jwt", "aud": "other"}, secret="jwt-secret")
    api = _api(auth=AuthConfig(mode="jwt", jwt_secret="jwt-secret", jwt_audience="pska"))
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/ready", headers={"Authorization": f"Bearer {token}"})

    assert status == 401
    assert "audience" in payload["error"]


def test_jwt_auth_rejects_expired_token() -> None:
    token = _jwt({"sub": "pska:user_primary", "tenant_id": "tenant_jwt", "exp": 1}, secret="jwt-secret")
    api = _api(auth=AuthConfig(mode="jwt", jwt_secret="jwt-secret"))
    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/ready", headers={"Authorization": f"Bearer {token}"})

    assert status == 401
    assert "expired" in payload["error"]


def test_local_console_serves_dashboard_assets_when_service_token_enabled() -> None:
    api = _api(service_token="secret")
    with _http_server(api) as base_url:
        status, headers, body = _http_text(base_url, "GET", "/console")
        data_status, data = _http_json(base_url, "GET", "/console/data")
        authed_status, authed = _http_json(
            base_url,
            "GET",
            "/console/data?limit=3",
            headers={"X-PSKA-Service-Token": "secret"},
        )

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert "PSKA" in body
    assert "/console/app.js" in body
    assert data_status == 401
    assert "service token" in data["error"]
    assert authed_status == 200
    assert authed["requires_agentic_service_online"] is False
    assert "source_counts" in authed
    assert "recommended_commands" in authed


def test_local_console_data_shows_home_dashboard_with_agentic_service_offline() -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = _api()
    api.agentic_service = DownAgenticService()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "console-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Console source note",
            "content": {"text": "The console dashboard should show recent sources."},
        }
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(base_url, "GET", "/console/data?owner_user_id=user_primary&limit=5")

    assert status == 200
    assert payload["ok"] is True
    assert payload["requires_agentic_service_online"] is False
    assert payload["service_readiness"]["agentic_service_ok"] is False
    assert payload["service_readiness"]["agentic_service_optional_for_console"] is True
    assert payload["source_counts"]["source_items"] == 1
    assert payload["source_counts"]["chunks"] == 1
    assert payload["pending_reviews"]["total_matching"] == 0
    assert payload["failed_jobs"]["count"] == 0
    assert payload["source_summary"]["recent_sources"][0]["title"] == "Console source note"
    assert "./scripts/pska daily-briefing" in payload["recommended_commands"]


def test_local_console_review_inbox_summarizes_pending_reviews() -> None:
    api = _api()
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_profile_ready",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Profile candidate",
            proposal={
                "profile_delta": {"topic": "PSKA"},
                "confidence": 0.82,
                "source_refs": [{"source_item_id": "src_1", "chunk_id": "chk_1"}],
            },
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_profile_missing_source",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Missing source",
            proposal={"profile_delta": {"topic": "ungrounded"}, "confidence": 0.51},
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_conflict",
            owner_user_id="user_primary",
            review_type=ReviewType.CONFLICT,
            title="Conflict",
            proposal={"confidence": 0.3},
        )
    )

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/console/reviews")
        data_status, payload = _http_json(base_url, "GET", "/console/reviews/data?status=pending&owner_user_id=user_primary")

    assert page_status == 200
    assert "/console/reviews.js" in body
    assert data_status == 200
    by_id = {item["review_item_id"]: item for item in payload["review_items"]}
    assert payload["total_matching"] == 3
    assert by_id["rev_profile_ready"]["review_type"] == "profile_update"
    assert by_id["rev_profile_ready"]["confidence"] == 0.82
    assert by_id["rev_profile_ready"]["source_ref_status"] == "present"
    assert by_id["rev_profile_ready"]["apply_supported"] is True
    assert by_id["rev_profile_ready"]["apply_ready"] is True
    assert "approve_apply" in by_id["rev_profile_ready"]["recommended_actions"]
    assert by_id["rev_profile_missing_source"]["source_ref_status"] == "missing"
    assert by_id["rev_profile_missing_source"]["apply_supported"] is True
    assert by_id["rev_profile_missing_source"]["apply_ready"] is False
    assert "approve_apply" not in by_id["rev_profile_missing_source"]["recommended_actions"]
    assert by_id["rev_conflict"]["apply_supported"] is False
    assert by_id["rev_conflict"]["apply_ready"] is False


def test_local_console_review_actions_use_review_api_and_audit() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "review-console-source",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Review source",
            "content": {"text": "Grounded profile candidate."},
        }
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_apply_profile",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Apply profile candidate",
            proposal={
                "profile_delta": {"topic": "PSKA console"},
                "confidence": 0.88,
                "source_refs": [{"source_item_id": source.source_item_id}],
            },
        )
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/review-items/rev_apply_profile/approve",
            {"actor_user_id": "user_primary", "reason": "console approve apply", "apply": True},
        )
        refreshed_status, refreshed = _http_json(base_url, "GET", "/console/reviews/data?status=pending&owner_user_id=user_primary")

    assert status == 200
    assert payload["review_item"]["status"] == "applied"
    assert payload["application_result"]["applied"] is True
    assert payload["application_result"]["promotion_type"] == "profile_card"
    assert payload["application_result"]["target_ids"]["profile_card_id"]
    assert "Promoted to profile card" in payload["application_result"]["summary"]
    assert refreshed_status == 200
    assert refreshed["total_matching"] == 0
    assert [event.action for event in api.store.list_audit_events()] == ["review.approve", "review.apply"]


def test_local_console_search_page_and_direct_results() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "console-search-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Console Search Note",
            "content": {"text": "Console search should show citations and snippets."},
        }
    )

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/console/search")
        search_status, payload = _http_json(
            base_url,
            "POST",
            "/console/search/query",
            {"query": "citations snippets", "mode": "direct", "user_id": "user_primary", "represented_user_id": "user_primary"},
        )

    assert page_status == 200
    assert "/console/search.js" in body
    assert "Agentic" in body
    assert search_status == 200
    assert payload["ok"] is True
    assert payload["mode"] == "direct"
    assert payload["requires_agentic_service_online"] is False
    assert payload["retrieval"]["results"][0]["title"] == "Console Search Note"
    assert payload["retrieval"]["citations"][0]["source_item_id"]
    assert "diagnostics" in payload["retrieval"]


def test_local_console_agentic_search_can_capture_conversation() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "console-agentic-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Console Agentic Note",
            "content": {"text": "Console agentic capture should cite this source."},
        }
    )

    class FakeAgenticService:
        def __init__(self, retrieval):
            self.retrieval = retrieval

        def ready(self):
            return {"ok": True, "provider": "test", "adapter": "fake"}

        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            retrieval = self.retrieval.search(query, user, represented_user_id=represented_user_id)
            return {
                "retrieval": to_jsonable(retrieval),
                "trace": {
                    "run_id": "run_capture",
                    "events": [
                        {"type": "think", "content": "private intermediate thought"},
                        {"type": "tool_call", "tool_name": "pska_pska_search", "tool_args": {"query": query}},
                        {"type": "tool_result", "tool_name": "pska_pska_search", "content": "large evidence" * 100},
                        {"type": "session_end", "content": "Console captured answer."},
                    ],
                    "query_understanding": {"intent": "test", "privacy_boundary": "acl_first"},
                    "retrieval_plan": ["external_agentic_service", "pska_search"],
                    "iterations": [{"iteration": "1", "query": query}],
                    "evidence_check": "has_citations",
                },
                "answer": "Console captured answer.",
                "agentic_service": {"provider": "test", "adapter": "fake"},
            }

    api.agentic_service = FakeAgenticService(api.retrieval)

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/console/search/query",
            {
                "query": "agentic capture cite",
                "mode": "agentic",
                "capture": True,
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["mode"] == "agentic"
    assert payload["requires_agentic_service_online"] is True
    assert payload["answer"] == "Console captured answer."
    assert payload["capture"]["action"] == "saved"
    assert payload["capture"]["source_item_id"]
    captured = next(item for item in api.store.list_source_items() if item.source_item_id == payload["capture"]["source_item_id"])
    assert captured.source_channel == "pska_agent"
    assert "Console captured answer." in captured.content_text
    trace_summary = captured.metadata["content"]["trace_summary"]
    assert trace_summary["run_id"] == "run_capture"
    assert "events" not in trace_summary
    assert trace_summary["raw_events_retained"] is False
    assert [event["kind"] for event in trace_summary["events_kept"]] == ["tool_call", "tool_result", "final_answer"]


def test_local_console_agentic_search_planning_error_falls_back_to_direct() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "console-agentic-fallback-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Console fallback note",
            "content": {"text": "Console fallback should still run direct retrieval."},
        }
    )

    class BrokenAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake"}

        def search(self, *_args, **_kwargs):
            raise AgenticServiceError("Agentic service unavailable")

    api.agentic_service = BrokenAgenticService()

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/console/search/query",
            {
                "query": "fallback retrieval",
                "mode": "agentic",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert payload["ok"] is False
    assert payload["error"]["type"] == "agentic_service_unavailable"
    assert "Direct retrieval fallback" in payload["error"]["message"]
    assert payload["error"]["detail"] == "Agentic service unavailable"
    assert "direct retrieval" in payload["answer"]
    assert "Console fallback should still run direct retrieval." in payload["answer"]
    assert payload["retrieval"]["results"]
    assert payload["citations"]
    assert payload["fallback"]["mode"] == "direct"
    assert "retrieval" in payload["fallback"]


def test_user_workspace_serves_assets_and_keeps_data_routes_token_protected() -> None:
    api = _api(service_token="secret")
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-token-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Workspace Token Note",
            "content": {"text": "Workspace direct retrieval should require the service token when configured."},
        }
    )

    with _http_server(api) as base_url:
        page_status, headers, body = _http_text(base_url, "GET", "/workspace")
        css_status, _css_headers, css_body = _http_text(base_url, "GET", "/workspace/app.css")
        app_alias_status, _alias_headers, app_alias_body = _http_text(base_url, "GET", "/app")
        blocked_status, blocked = _http_json(
            base_url,
            "POST",
            "/workspace/search/query",
            {"query": "workspace token", "mode": "direct", "user_id": "user_primary", "represented_user_id": "user_primary"},
        )
        authed_status, authed = _http_json(
            base_url,
            "POST",
            "/workspace/search/query",
            {"query": "workspace token", "mode": "direct", "user_id": "user_primary", "represented_user_id": "user_primary"},
            headers={"X-PSKA-Service-Token": "secret"},
        )
        corpus_blocked_status, corpus_blocked = _http_json(base_url, "GET", "/workspace/corpus/data?owner_user_id=user_primary")
        writer_blocked_status, writer_blocked = _http_json(
            base_url,
            "POST",
            "/workspace/writer/suggest",
            {"selected_text": "workspace token", "user_id": "user_primary", "represented_user_id": "user_primary"},
        )

    assert page_status == 200
    assert headers["content-type"].startswith("text/html")
    assert "User Workspace" in body
    assert 'id="chat"' in body
    assert 'id="corpus"' in body
    assert 'id="writer"' in body
    assert 'id="evidence"' in body
    assert "/workspace/app.js" in body
    assert css_status == 200
    assert "white-space: pre-wrap" in css_body
    assert app_alias_status == 200
    assert "User Workspace" in app_alias_body
    assert blocked_status == 401
    assert "service token" in blocked["error"]
    assert corpus_blocked_status == 401
    assert "service token" in corpus_blocked["error"]
    assert writer_blocked_status == 401
    assert "service token" in writer_blocked["error"]
    assert authed_status == 200
    assert authed["workspace"]["surface"] == "user_workspace"
    assert authed["workspace"]["evidence"]["citations"][0]["source_item_id"]


def test_workspace_activity_drives_continue_working() -> None:
    api = _api()

    opened = api.record_workspace_activity(
        {
            "owner_user_id": "user_primary",
            "activity_type": "opened",
            "surface": "document",
            "target_type": "workspace_surface",
            "target_id": "document",
            "title": "文档工作区",
            "summary": "打开文档工作区。",
        }
    )
    api.record_workspace_activity(
        {
            "owner_user_id": "user_primary",
            "activity_type": "edited",
            "surface": "document",
            "target_type": "workspace_surface",
            "target_id": "document",
            "title": "文档工作区",
            "summary": "编辑了当前草稿。",
            "metadata": {"text_length": 42},
        }
    )
    api.record_workspace_activity(
        {
            "owner_user_id": "user_primary",
            "activity_type": "pinned",
            "surface": "review",
            "target_type": "workspace_surface",
            "target_id": "review",
            "title": "Review Center",
        }
    )

    activity = api.workspace_activity(owner_user_id="user_primary", limit=10)
    today = api.workspace_today(owner_user_id="user_primary", limit=10)

    assert opened["activity"]["activity_type"] == "opened"
    assert activity["activity"][0]["activity_type"] == "pinned"
    assert activity["activity"][1]["activity_type"] == "edited"
    assert activity["continue_working"][0]["target_id"] == "review"
    assert activity["continue_working"][0]["pinned"] is True
    assert today["source"]["uses_workspace_activity"] is True
    assert today["continue_working"][0]["id"] == "review"
    assert today["continue_working"][1]["activity_type"] == "edited"


def test_workspace_activity_http_endpoint_is_token_protected() -> None:
    api = _api(service_token="secret")
    payload = {
        "owner_user_id": "user_primary",
        "activity_type": "viewed",
        "surface": "review",
        "target_type": "workspace_surface",
        "target_id": "review",
        "title": "Review Center",
    }

    with _http_server(api) as base_url:
        blocked_status, blocked = _http_json(base_url, "POST", "/workspace/activity", payload)
        authed_status, authed = _http_json(
            base_url,
            "POST",
            "/workspace/activity",
            payload,
            headers={"X-PSKA-Service-Token": "secret"},
        )
        data_status, data = _http_json(
            base_url,
            "GET",
            "/workspace/activity/data?owner_user_id=user_primary",
            headers={"X-PSKA-Service-Token": "secret"},
        )

    assert blocked_status == 401
    assert "service token" in blocked["error"]
    assert authed_status == 200
    assert authed["activity"]["activity_type"] == "viewed"
    assert data_status == 200
    assert data["continue_working"][0]["target_id"] == "review"


def test_discovery_producers_drive_today_discoveries() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "discovery-topic-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Discovery Topic Note",
            "content": {"text": "Discovery producer should surface this as a topic."},
        }
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_relationship_discovery",
            owner_user_id="user_primary",
            review_type=ReviewType.RELATIONSHIP_CANDIDATE,
            title="Relationship candidate",
            proposal={"confidence": 0.81, "source_refs": [{"source_item_id": "src_rel"}]},
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_conflict_discovery",
            owner_user_id="user_primary",
            review_type=ReviewType.CONFLICT,
            title="Conflict candidate",
            proposal={"confidence": 0.66, "source_refs": [{"source_item_id": "src_conflict"}]},
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_memory_discovery",
            owner_user_id="user_primary",
            review_type=ReviewType.MEMORY_CANDIDATE,
            title="Memory candidate",
            proposal={"memory_candidate": "PSKA prefers producer-backed discoveries.", "confidence": 0.74},
        )
    )
    api.store.upsert_discovery_item(
        DiscoveryItem(
            discovery_id="disc_old",
            owner_user_id="user_primary",
            discovery_type="relationship",
            title="Old discovery",
            evidence=[],
            confidence=0.5,
            producer="RelationshipDiscoveryProducer",
            created_at=utc_now() - timedelta(days=8),
        )
    )

    discoveries = api.workspace_discoveries(owner_user_id="user_primary", limit=20, min_score=0)
    ranked_discoveries = api.workspace_discoveries(owner_user_id="user_primary", limit=20)
    today = api.workspace_today(owner_user_id="user_primary", limit=20)

    by_type = {item["type"]: item for item in discoveries["discoveries"]}
    assert {"relationship", "conflict", "memory", "topic"} <= set(by_type)
    assert by_type["relationship"]["producer"] == "RelationshipDiscoveryProducer"
    assert by_type["conflict"]["producer"] == "ConflictDiscoveryProducer"
    assert by_type["memory"]["producer"] == "MemoryDiscoveryProducer"
    assert by_type["topic"]["producer"] == "TopicDiscoveryProducer"
    assert by_type["relationship"]["fingerprint"]
    assert by_type["relationship"]["evidence_snapshot"] == by_type["relationship"]["evidence"]
    assert by_type["relationship"]["discovery_score"] >= ranked_discoveries["min_score"]
    assert by_type["topic"]["discovery_score"] >= ranked_discoveries["min_score"]
    assert by_type["topic"]["quality_signals"]["source_topic_floor"] == 0.52
    assert all(item["discovery_score"] >= ranked_discoveries["min_score"] for item in ranked_discoveries["discoveries"])
    assert any(item["type"] == "topic" for item in ranked_discoveries["discoveries"])
    assert all(item["status"] == "new" for item in today["discoveries"])
    assert all(item["discovery_score"] >= today.get("discovery_min_score", ranked_discoveries["min_score"]) for item in today["discoveries"])
    assert all(item["id"] != "disc_old" for item in today["discoveries"])
    assert today["source"]["uses_dedicated_discovery_feed"] is True


def test_user_workspace_direct_search_returns_evidence_summary() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-direct-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Workspace Direct Note",
            "content": {"text": "Workspace direct chat should show citations, snippets, gaps, and graph evidence summaries."},
        }
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/search/query",
            {
                "query": "citations snippets graph evidence",
                "mode": "direct",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["mode"] == "direct"
    assert payload["requires_agentic_service_online"] is False
    assert payload["workspace"]["chat_status"]["message"] == "Direct retrieval completed."
    assert payload["workspace"]["raw_json_hidden_by_default"] is True
    assert payload["workspace"]["writer_available"] is True
    assert payload["workspace"]["corpus_available"] is True
    evidence = payload["workspace"]["evidence"]
    assert evidence["citations"][0]["title"] == "Workspace Direct Note"
    assert evidence["source_refs"] == evidence["citations"]
    assert "graph_paths" in evidence
    assert "memory_context" in evidence
    assert "profile_context" in evidence
    assert "gaps" in evidence
    assert "conflicts" in evidence


def test_user_workspace_agentic_failure_reports_direct_fallback() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-agentic-fallback-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Workspace fallback note",
            "content": {"text": "Workspace should still show direct retrieval when agentic planning is unavailable."},
        }
    )

    class BrokenAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake"}

        def search(self, *_args, **_kwargs):
            raise AgenticServiceError("Agentic service offline")

    api.agentic_service = BrokenAgenticService()

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/search/query",
            {
                "query": "agentic unavailable direct retrieval",
                "mode": "agentic",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )
        app_status, _headers, app_js = _http_text(base_url, "GET", "/workspace/app.js")

    assert status == 200
    assert payload["ok"] is False
    assert payload["mode"] == "agentic"
    assert payload["display_mode"] == "direct_fallback"
    assert payload["requires_agentic_service_online"] is True
    assert payload["workspace"]["chat_status"]["message"] == "Agentic search is unavailable; direct retrieval fallback is shown."
    assert payload["workspace"]["chat_status"]["display_mode"] == "direct_fallback"
    assert payload["workspace"]["evidence"]["citations"][0]["title"] == "Workspace fallback note"
    assert payload["fallback"]["mode"] == "direct"
    assert payload["fallback"]["display_mode"] == "direct_fallback"
    assert app_status == 200
    assert "PSKA Direct fallback" in app_js
    assert "Agentic service did not return a usable grounded answer. Direct retrieval found source refs below." in app_js
    assert "finalAnswerFromEvents" in app_js
    assert "FastReAct tool trace" in app_js
    assert "Raw FastReAct events" in app_js
    assert "Raw PSKA response" in app_js


def test_user_workspace_corpus_explorer_filters_and_summarizes_knowledge() -> None:
    api = _api()
    files_source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "files",
            "record_type": "note",
            "source_id": "workspace-corpus-file",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Workspace Corpus File",
            "content": {"text": "Alpha corpus explorer should expose chunk snippets and graph evidence."},
        }
    )
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-corpus-manual",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Workspace Corpus Manual",
            "content": {"text": "Beta manual source should be filtered out when files channel is selected."},
        }
    )
    api.store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_workspace_corpus",
            owner_user_id="user_primary",
            layer=MemoryLayer.SEMANTIC,
            text="Alpha corpus memory is visible as readable text.",
            confidence=0.87,
            source_refs=[SourceRef(source_item_id=files_source.source_item_id)],
            created_by_user_id="agent_service",
        )
    )
    api.store.add_profile_card(
        UserProfileCard(
            profile_card_id="upc_workspace_corpus",
            owner_user_id="user_primary",
            profile={"topic": "alpha corpus"},
            confidence=0.8,
            source_refs=[SourceRef(source_item_id=files_source.source_item_id)],
        )
    )
    graph = HypergraphService(api.store)
    graph.create_entity(Entity("ent_workspace_pska", "project", "PSKA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_workspace_alpha", "topic", "Alpha Corpus", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="documents",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_workspace_pska", "subject"), ("ent_workspace_alpha", "object")],
        evidence_text="PSKA documents Alpha Corpus evidence.",
        source_refs=[SourceRef(source_item_id=files_source.source_item_id)],
        confidence=0.91,
    )

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/workspace")
        status, payload = _http_json(
            base_url,
            "GET",
            "/workspace/corpus/data?owner_user_id=user_primary&source_channel=files&query=Alpha&limit=10",
        )

    assert page_status == 200
    assert "corpus-form" in body
    assert "corpus-chunks" in body
    assert "corpus-graph" in body
    assert status == 200
    assert payload["read_only"] is True
    assert payload["filters"]["source_channel"] == "files"
    assert payload["filters"]["query"] == "Alpha"
    assert payload["filters"]["available_source_channels"] == ["files", "manual"]
    assert payload["counts"]["sources_matching"] == 1
    assert payload["sources"][0]["title"] == "Workspace Corpus File"
    assert payload["sources"][0]["source_channel"] == "files"
    assert payload["sources"][0]["chunk_count"] == 1
    assert "Alpha corpus explorer" in payload["chunks"][0]["snippet"]
    assert payload["documents"][0]["chunk_count"] == 1
    assert payload["memories"][0]["text"] == "Alpha corpus memory is visible as readable text."
    assert payload["memories"][0]["source_ref_status"] == "present"
    assert payload["profiles"][0]["profile"] == {"topic": "alpha corpus"}
    assert payload["profiles"][0]["source_ref_status"] == "present"
    assert payload["entities"][0]["label"] in {"PSKA", "Alpha Corpus"}
    assert payload["hyperedges"][0]["relation_type"] == "documents"
    assert payload["hyperedges"][0]["members"][0]["label"] == "PSKA"
    assert payload["hyperedges"][0]["source_refs"][0]["source_item_id"] == files_source.source_item_id


def test_api_job_context_returns_documents_and_passage_windows() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-context-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "Graph context document-first evidence. " * 8},
        }
    )
    job = api.jobs.submit(
        DIGEST_VIA_FASTREACT,
        {"owner_user_id": "user_primary", "source_refs": [{"source_item_id": source.source_item_id}]},
    )

    payload = api.job_context(job.job_id)

    assert payload["documents"][0]["source_item_id"] == source.source_item_id
    assert payload["passage_windows"][0]["document_id"] == payload["documents"][0]["document_id"]
    assert payload["passage_windows"][0]["token_estimate"] > 0
    assert payload["context_policy"]["input_strategy"] == "document_first"
    assert payload["context_policy"]["chunks_role"] == "retrieval_slices_compatibility"


def test_workspace_graph_data_links_digest_claim_hyperedge_to_passage_evidence() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-v2-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "content": {"text": "PSKA GraphRAG v2 formalizes digest notes into claim-backed hyperedges."},
        }
    )
    chunk = api.store.list_chunks_for_sources({source.source_item_id})[0]
    ref = SourceRef(source_item_id=source.source_item_id, document_id=chunk.document_id, chunk_id=chunk.chunk_id)
    api.store.add_knowledge_claim(
        KnowledgeClaim(
            knowledge_claim_id="kc_graph_v2",
            owner_user_id="user_primary",
            claim_type="fact",
            statement="PSKA GraphRAG v2 把 digest note 形式化为 claim-backed hyperedge。",
            source_refs=[ref],
            evidence_text="formalizes digest notes into claim-backed hyperedges",
            subject="PSKA GraphRAG v2",
            predicate="formalizes",
            object="digest notes",
            confidence=0.9,
        )
    )
    api.store.add_digest_note(
        DigestNote(
            digest_note_id="dig_graph_v2",
            owner_user_id="user_primary",
            title="GraphRAG v2 digest",
            synopsis="Digest notes are first-class graph nodes grounded in source passages.",
            source_refs=[ref],
            confidence=0.9,
        )
    )
    api.store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_graph_v2",
            owner_user_id="user_primary",
            layer=MemoryLayer.SEMANTIC,
            text="PSKA GraphRAG v2 treats digest notes as graph nodes.",
            confidence=0.8,
            source_refs=[ref],
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_graph_v2_action",
            owner_user_id="user_primary",
            review_type=ReviewType.ACTION_CANDIDATE,
            title="Review graph action",
            proposal={
                "plain_text_summary": "Check whether digest notes connect to evidence passages.",
                "source_refs": [to_jsonable(ref)],
            },
        )
    )
    api.store.add_entity(Entity("ent_pska_graphrag_v2", "system", "PSKA GraphRAG v2", "user_primary", "private_primary", Visibility.PRIVATE))
    api.store.add_entity(Entity("ent_digest_note", "artifact", "digest notes", "user_primary", "private_primary", Visibility.PRIVATE))
    graph = HypergraphService(api.store)
    graph.create_hyperedge(
        relation_type="formalizes",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_pska_graphrag_v2", "system"), ("ent_digest_note", "artifact")],
        evidence_text="formalizes digest notes into claim-backed hyperedges",
        source_refs=[ref],
        confidence=0.9,
    )

    payload = api.workspace_graph_data(owner_user_id="user_primary", limit=20)

    node_types = {node["type"] for node in payload["nodes"]}
    edge_types = {edge["type"] for edge in payload["edges"]}
    assert {"source", "document", "passage", "claim", "digest", "phrase", "entity", "fact", "hyperedge", "memory", "action"} <= node_types
    assert {"contains", "grounds", "summarizes", "formalizes", "suggests_relationship", "member", "represented_by", "participates_in", "mentions", "links_to", "remembered_from", "needs_review_from"} <= edge_types
    assert payload["counts"]["claims"] == 1
    assert payload["counts"]["digest_notes"] == 1
    assert payload["counts"]["memories"] == 1
    assert payload["counts"]["review_items"] == 1
    assert payload["counts"]["phrases"] >= 2
    assert payload["counts"]["facts"] == 1
    insights = payload["insights"]
    assert insights["layer_coverage"]["evidence"] >= 3
    assert insights["layer_coverage"]["understanding"] >= 2
    assert insights["layer_coverage"]["semantic"] >= 3
    assert insights["evidence_health"]["grounded_nodes"] >= 4
    assert insights["topic_clusters"]
    assert insights["guided_tour"]
    filtered = api.workspace_graph_data(owner_user_id="user_primary", limit=20, node_types={"source", "document", "passage", "claim", "digest", "fact", "hyperedge"})
    assert {node["type"] for node in filtered["nodes"]}.isdisjoint({"entity", "phrase"})
    assert filtered["projection"]["unfiltered_nodes"] >= filtered["projection"]["nodes"]
    assert filtered["projection"]["node_types"] == ["claim", "digest", "document", "fact", "hyperedge", "passage", "source"]
    subgraph = api.workspace_graph_subgraph(owner_user_id="user_primary", node_id="digest:dig_graph_v2", limit=20, hops=1)
    subgraph_node_ids = {node["id"] for node in subgraph["nodes"]}
    assert subgraph["ok"] is True
    assert "digest:dig_graph_v2" in subgraph_node_ids
    assert "claim:kc_graph_v2" in subgraph_node_ids
    assert subgraph["projection"]["nodes"] < payload["projection"]["nodes"]
    assert subgraph["evidence_path"]["understanding_node_count"] >= 1
    search_subgraph = api.workspace_graph_search_subgraph(
        owner_user_id="user_primary",
        query="digest",
        limit=20,
        hops=1,
        node_types={"source", "document", "passage", "claim", "digest", "fact", "hyperedge"},
    )
    assert search_subgraph["ok"] is True
    assert search_subgraph["matches"]
    assert {node["type"] for node in search_subgraph["nodes"]} <= {"source", "document", "passage", "claim", "digest", "fact", "hyperedge"}

    reindex = api.graph_reindex(owner_user_id="user_primary", limit=20)

    assert reindex["ok"] is True
    assert reindex["projection"]["graph_nodes"] == len(payload["nodes"])
    assert reindex["projection"]["graph_edges"] == len(payload["edges"])
    assert api.store.count_table("graph_nodes") == len(payload["nodes"])
    assert api.store.count_table("graph_edges") == len(payload["edges"])


def test_workspace_graph_path_defaults_to_agentic_graphrag_with_deterministic_seeds() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Note",
            "content": {"text": "GraphRAG online queries should inspect passage neighbors and graph facts."},
        }
    )

    class CapturingAgenticService(FakeAgenticService):
        def __init__(self, retrieval):
            super().__init__(retrieval)
            self.query = ""
            self.skills = None

        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None):
            self.query = query
            self.skills = skills
            return {
                "answer": "Agentic GraphRAG answer. " * 40,
                "retrieval": {"citations": [{"source_item_id": "src_graph_path"}]},
                "trace": {
                    "retrieval_plan": ["deterministic_seeds", "graph_expansion", "synthesis"],
                    "expansion_decisions": [
                        {"target": "previous_next_passage", "decision": "inspect_if_evidence_gap"},
                        {"target": "connected_fact_neighbors", "decision": "inspect_if_query_entities_match"},
                    ],
                    "fact_relevance_filter": {
                        "kept_facts": [{"fact_id": "fact_graph_path", "statement": "GraphRAG queries inspect passage neighbors."}],
                        "filtered_out_facts": [{"fact_id": "fact_unrelated", "statement": "Unrelated fact."}],
                    },
                    "evidence_check": "has_citations",
                },
                "source_refs": [{"source_item_id": "src_graph_path"}],
                "agentic_service": {"provider": "test", "adapter": "fake"},
            }

    agentic = CapturingAgenticService(api.retrieval)
    api.agentic_service = agentic

    payload = api.workspace_graph_path(query="GraphRAG online queries", owner_user_id="user_primary")

    assert payload["ok"] is True
    assert payload["mode"] == "agentic"
    assert payload["requires_agentic_service_online"] is True
    assert payload["answer"] == ("Agentic GraphRAG answer. " * 40).strip()
    assert payload["answer_mode"] == "agentic_synthesis"
    assert payload["deterministic"]["mode"] == "deterministic"
    assert payload["agentic_contract"]["pattern"] == "hipporag_style_agentic_graphrag"
    assert payload["query_seeds"]["terms"]
    assert payload["supporting_passages"][0]["source_item_id"]
    assert payload["path_summary"]["result_count"] >= 1
    assert payload["path_summary"]["filter_mode"] == "agentic_llm_relevance"
    assert payload["top_facts"][0]["fact_id"] == "fact_graph_path"
    assert payload["filtered_out_facts"][0]["fact_id"] == "fact_unrelated"
    assert "deterministic_seeds" in agentic.query
    assert "supporting_passages" in agentic.query
    assert "previous/next passage windows" in agentic.query
    assert "do not open with GraphRAG/retrieval/graph-path status" in agentic.query
    assert "Keep retrieval diagnostics, graph path counts" in agentic.query
    assert agentic.skills == []
    assert payload["agentic_trace"]["expansion_decisions"][0]["target"] == "previous_next_passage"

    deterministic = api.workspace_graph_path(query="GraphRAG online queries", owner_user_id="user_primary", mode="deterministic")
    assert deterministic["path_summary"]["filter_mode"] == "deterministic_relevance"
    assert deterministic["answer"].startswith("关键结论：")
    assert "基于当前 PSKA 检索与图谱路径" not in deterministic["answer"]
    assert "条 graph path" not in deterministic["answer"].casefold()
    assert "作为多跳线索" not in deterministic["answer"]
    assert "图谱路径" not in deterministic["answer"]
    assert "filtered_out_facts" in deterministic
    assert "supporting_passages" in agentic.query
    assert "score_debug" not in agentic.query


def test_workspace_graph_path_synthesizes_grounded_answer_when_agentic_answer_is_short() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-short-answer",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Short Answer Note",
            "content": {"text": "GraphRAG short agentic answers should be supplemented with grounded passages and citations."},
        }
    )

    class ShortAgenticService(FakeAgenticService):
        def __init__(self, retrieval):
            super().__init__(retrieval)
            self.calls = 0

        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            self.calls += 1
            return {
                "answer": "Too short.",
                "trace": {"expansion_decisions": [{"target": "seed", "decision": "use"}]},
                "agentic_service": {"provider": "test"},
            }

    service = ShortAgenticService(api.retrieval)
    api.agentic_service = service

    payload = api.workspace_graph_path(query="GraphRAG short agentic answers", owner_user_id="user_primary")

    assert payload["ok"] is True
    assert payload["answer_mode"] == "deterministic_synthesis_for_short_agentic"
    assert payload["agentic_answer"] == "Too short."
    assert payload["agentic_repair"]["attempted"] is True
    assert payload["agentic_repair"]["accepted"] is False
    assert service.calls == 2
    assert "关键结论" in payload["answer"]
    assert len(payload["answer"]) >= 300


def test_workspace_graph_path_repairs_short_agentic_answer_before_fallback() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-repair-answer",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Repair Answer Note",
            "content": {"text": "GraphRAG repaired agentic answers should remain agentic synthesis when the repair is grounded and long enough."},
        }
    )

    class RepairingAgenticService(FakeAgenticService):
        def __init__(self, retrieval):
            super().__init__(retrieval)
            self.queries = []

        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            self.queries.append(query)
            if len(self.queries) == 1:
                return {
                    "answer": "Too short.",
                    "trace": {"expansion_decisions": [{"target": "seed", "decision": "use"}]},
                    "source_refs": [{"source_item_id": "src_first"}],
                    "agentic_service": {"provider": "test"},
                }
            return {
                "answer": "修复后的答案说明：PSKA 的 GraphRAG 会先使用 passage、claim、fact 和 digest 作为证据种子，再通过图谱路径检查相关事实是否足够支撑回答。关键结论是，系统应优先给出带引用的综合解释，而不是只返回实体列表。第二个结论是，digest note 和 knowledge claim 应该作为一等图谱节点参与问答，因为它们保存了文档被理解后的语义。风险是，如果 FastReAct 第一次回答过短，用户会误以为没有足够证据；因此 repair loop 会要求它重新组织关键结论、风险、下一步和不确定性。下一步是继续降低短回答比例，并记录 repair 是否成功。若证据不足，回答也必须明确指出缺口，而不能假装已经完成推理。证据来自 src_first 和当前 deterministic seeds。",
                "retrieval": {"citations": [{"source_item_id": "src_repair"}]},
                "trace": {"expansion_decisions": [{"target": "repair", "decision": "rewrite_with_seed_evidence"}]},
                "source_refs": [{"source_item_id": "src_repair"}],
                "agentic_service": {"provider": "test", "run_id": "repair_run"},
            }

    service = RepairingAgenticService(api.retrieval)
    api.agentic_service = service

    payload = api.workspace_graph_path(query="GraphRAG repaired agentic answers", owner_user_id="user_primary")

    assert payload["ok"] is True
    assert payload["answer_mode"] == "agentic_synthesis"
    assert payload["agentic_repair"]["attempted"] is True
    assert payload["agentic_repair"]["accepted"] is True
    assert payload["agentic_trace"]["repair"]["accepted"] is True
    assert payload["agentic_service"]["run_id"] == "repair_run"
    assert len(service.queries) == 2
    assert "Repair the previous PSKA GraphRAG answer" in service.queries[1]


def test_workspace_graph_path_rejects_unusable_agentic_answer() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-unusable",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Unusable Note",
            "content": {"text": "GraphRAG unusable answers should fall back when MCP tools fail."},
        }
    )

    class UnusableAgenticService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            return {
                "answer": "The PSKA knowledge tools are unavailable due to an MCP transport coroutine conflict (`readuntil()` concurrent call).",
                "trace": {"events": [{"type": "error", "message": "MCP transport readuntil failed"}]},
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = UnusableAgenticService(api.retrieval)

    payload = api.workspace_graph_path(query="GraphRAG unusable answers", owner_user_id="user_primary")

    assert payload["ok"] is False
    assert payload["display_mode"] == "deterministic_fallback"
    assert payload["error"]["type"] == "agentic_graph_answer_unusable"


def test_workspace_graph_path_rejects_agentic_tool_timeout_report() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-tool-timeout",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Tool Timeout Note",
            "content": {"text": "Acme Example pipeline next action is Prepare partner meeting brief."},
        }
    )

    class ToolTimeoutAgenticService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            return {
                "answer": "PSKA tools are unreachable (timeout). No evidence retrieved.",
                "trace": {
                    "events": [{"type": "tool_result", "content": "MCP request timeout (30.0s)"}],
                    "evidence_check": "No evidence retrieved",
                },
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = ToolTimeoutAgenticService(api.retrieval)

    payload = api.workspace_graph_path(query="Acme Example pipeline next action", owner_user_id="user_primary")

    assert payload["ok"] is False
    assert payload["display_mode"] == "deterministic_fallback"
    assert payload["error"]["type"] == "agentic_graph_answer_unusable"
    assert payload["error"]["detail"] in {"pska tools are unreachable", "mcp request timeout", "no evidence retrieved"}


def test_workspace_graph_path_rejects_agentic_query_truncation_claim() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-query-truncated",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Query Truncated Note",
            "content": {"text": "GraphRAG should not show a query truncation hallucination as a finished answer."},
        }
    )

    class QueryTruncatedAgenticService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            return {
                "answer": "However, your question was truncated — the full query was not received.",
                "trace": {"evidence_check": "insufficient_query"},
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = QueryTruncatedAgenticService(api.retrieval)

    payload = api.workspace_graph_path(query="What is in the Excel pipeline for Acme Example?", owner_user_id="user_primary")

    assert payload["ok"] is False
    assert payload["display_mode"] == "deterministic_fallback"
    assert payload["error"]["detail"] == "question was truncated"


def test_workspace_graph_path_rejects_generic_operational_agentic_summary() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-operational-summary",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Operational Summary Note",
            "content": {"text": "GraphRAG should answer the asked question, not summarize system readiness."},
        }
    )

    class OperationalSummaryAgenticService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3):
            return {
                "answer": "PSKA knowledge base is operational with source items, entities, hyperedges, knowledge claims, and pending review items spanning people and companies.",
                "trace": {"evidence_check": "system_status"},
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = OperationalSummaryAgenticService(api.retrieval)

    payload = api.workspace_graph_path(query="Acme Example 当前 pipeline 里的下一步行动是什么？", owner_user_id="user_primary")

    assert payload["ok"] is False
    assert payload["display_mode"] == "deterministic_fallback"
    assert payload["error"]["detail"] == "pska knowledge base is operational"


def test_workspace_graph_path_rejects_agentic_answer_that_misses_query_anchors() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-query-anchor-miss",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Query Anchor Miss Note",
            "content": {"text": "Acme Example ARR is 1200000 and next action is Prepare partner meeting brief."},
        }
    )

    class AnchorMissAgenticService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None):
            return {
                "answer": "PSKA knowledge base currently contains source documents about acme-example, startup market dynamics, and founder execution themes.",
                "trace": {"evidence_check": "system_overview"},
                "source_refs": [{"source_item_id": "src_overview"}],
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = AnchorMissAgenticService(api.retrieval)

    payload = api.workspace_graph_path(query="Acme Example ARR next action", owner_user_id="user_primary")

    assert payload["ok"] is False
    assert payload["display_mode"] == "deterministic_fallback"
    assert payload["error"]["detail"] in {
        "pska knowledge base currently contains",
        "agentic_answer_missed_query_fields",
        "agentic_answer_missed_query_anchors",
    }


def test_workspace_graph_path_rejects_partial_pipeline_overview_missing_fields() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "graph-path-partial-pipeline-overview",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Graph Path Partial Pipeline Overview Note",
            "content": {"text": "Acme Example lead is Alice Example and next action is Prepare partner meeting brief."},
        }
    )

    class PartialPipelineOverviewAgenticService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None):
            return {
                "answer": (
                    "Sales pipeline data includes Acme Example ($1.2M ARR, active) and Widget Co. "
                    "This overview says the tenant has startup investment analysis, market timing notes, "
                    "founder execution calibration, and relationship evidence across the benchmark corpus. "
                    "It mentions Acme Example and ARR, but it is still framed as a broad pipeline overview "
                    "rather than answering every requested field from the user question."
                ),
                "trace": {"evidence_check": "partial_overview"},
                "source_refs": [{"source_item_id": "src_overview"}],
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = PartialPipelineOverviewAgenticService(api.retrieval)

    payload = api.workspace_graph_path(query="Acme Example 当前 pipeline 的 ARR、负责人、状态和下一步行动是什么？", owner_user_id="user_primary")

    assert payload["ok"] is False
    assert payload["display_mode"] == "deterministic_fallback"
    assert payload["error"]["detail"] == "agentic_answer_missed_query_fields"


def test_workspace_graph_path_answers_pipeline_next_step_from_table() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "portfolio-pipeline.xlsx",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "portfolio-pipeline.xlsx",
            "content": {
                "text": (
                    "# Workbook: portfolio-pipeline.xlsx\n\n"
                    "## Sheet: Pipeline\n\n"
                    "| Company | Lead | Status | ARR | Next Step |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| Acme Example | Alice Example | active | 1200000 | Prepare partner meeting brief |\n"
                    "| Widget Co | Charlie Example | watch | 450000 | Review COO transition risk |\n"
                )
            },
            "extra": {"extraction": {"extractor": "xlsx-zip-xml"}},
        }
    )

    payload = api.workspace_graph_path(query="Acme Example 当前 pipeline 里的下一步行动是什么？", owner_user_id="user_primary", mode="deterministic")

    assert payload["ok"] is True
    assert "Prepare partner meeting brief" in payload["answer"]
    assert "Alice Example" in payload["answer"]
    assert "1200000" in payload["answer"]


def test_user_workspace_writer_suggests_with_selected_text_and_evidence() -> None:
    api = _api()
    source = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-writer-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Workspace Writer Note",
            "content": {"text": "Writer suggestions should cite grounded PSKA evidence about alpha writing."},
        }
    )
    api.store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_workspace_writer",
            owner_user_id="user_primary",
            layer=MemoryLayer.SEMANTIC,
            text="The user prefers grounded Chinese writing suggestions about alpha writing.",
            confidence=0.9,
            source_refs=[SourceRef(source_item_id=source.source_item_id)],
            created_by_user_id="agent_service",
        )
    )
    api.store.add_profile_card(
        UserProfileCard(
            profile_card_id="upc_workspace_writer",
            owner_user_id="user_primary",
            profile={"writing": {"language": "zh", "style": "grounded"}},
            confidence=0.85,
            source_refs=[SourceRef(source_item_id=source.source_item_id)],
        )
    )
    graph = HypergraphService(api.store)
    graph.create_entity(Entity("ent_writer_pska", "project", "PSKA", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_writer_alpha", "topic", "alpha writing", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="supports",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_writer_pska", "system"), ("ent_writer_alpha", "topic")],
        evidence_text="PSKA supports grounded alpha writing.",
        source_refs=[SourceRef(source_item_id=source.source_item_id)],
        confidence=0.92,
    )
    memory_count = len(api.store.list_agent_memories(owner_user_id="user_primary"))
    profile_count = len(api.store.list_profile_cards(owner_user_id="user_primary"))
    hyperedge_count = len(api.store.list_hyperedges_for_entities({"ent_writer_pska", "ent_writer_alpha"}))

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/workspace")
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/writer/suggest",
            {
                "selected_text": "alpha writing needs grounded evidence",
                "draft_text": "我正在写一段关于 alpha writing 的中文说明。",
                "instruction": "请给中文改写建议，并说明引用哪些 PSKA 证据。",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert page_status == 200
    assert 'contenteditable="true"' in body
    assert "writer-suggest" in body
    assert "selected-text" in body
    assert status == 200
    assert payload["ok"] is True
    assert payload["mode"] == "writer_suggest"
    assert payload["read_only"] is True
    assert payload["default_language"] == "zh"
    assert payload["does_not_mutate_memory_profile_graph"] is True
    assert payload["query_context"]["selected_text"] == "alpha writing needs grounded evidence"
    assert "alpha writing needs grounded evidence" in payload["query_context"]["query"]
    assert payload["suggestion"]["language"] == "zh"
    assert "中文写作建议" in payload["suggestion"]["summary"]
    assert payload["suggestion"]["used_context"]["citation_count"] >= 1
    assert payload["suggestion"]["used_context"]["memory_count"] >= 1
    assert payload["suggestion"]["used_context"]["profile_count"] >= 1
    assert payload["evidence"]["citations"][0]["title"] == "Workspace Writer Note"
    assert payload["evidence"]["source_refs"] == payload["evidence"]["citations"]
    assert payload["evidence"]["memory_context"]
    assert payload["evidence"]["profile_context"]
    assert len(api.store.list_agent_memories(owner_user_id="user_primary")) == memory_count
    assert len(api.store.list_profile_cards(owner_user_id="user_primary")) == profile_count
    assert len(api.store.list_hyperedges_for_entities({"ent_writer_pska", "ent_writer_alpha"})) == hyperedge_count


def test_local_console_memory_page_is_read_only_and_flags_risky_records() -> None:
    api = _api()
    api.store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_console_ready",
            owner_user_id="user_primary",
            layer=MemoryLayer.SEMANTIC,
            text="PSKA console remembers grounded facts.",
            confidence=0.9,
            source_refs=[SourceRef(source_item_id="src_1", chunk_id="chk_1")],
            created_by_user_id="agent_service",
        )
    )
    api.store.add_agent_memory(
        AgentMemory(
            agent_memory_id="agm_console_missing",
            owner_user_id="user_primary",
            layer=MemoryLayer.EPISODIC,
            text="Missing source memory.",
            confidence=0.3,
            source_refs=[],
            decay_policy="manual",
        )
    )
    api.store.add_profile_card(
        UserProfileCard(
            profile_card_id="upc_console",
            owner_user_id="user_primary",
            profile={"communication": {"style": "concise"}},
            confidence=0.92,
            source_refs=[SourceRef(message_id="msg_profile")],
        )
    )

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/console/memory")
        data_status, payload = _http_json(base_url, "GET", "/console/memory/data?owner_user_id=user_primary&limit=10")

    assert page_status == 200
    assert "/console/memory.js" in body
    assert "Profile Cards" in body
    assert data_status == 200
    assert payload["read_only"] is True
    assert payload["memory_count"] == 2
    assert payload["profile_count"] == 1
    by_id = {item["agent_memory_id"]: item for item in payload["agent_memories"]}
    assert by_id["agm_console_ready"]["source_ref_status"] == "present"
    assert by_id["agm_console_ready"]["created_by_user_id"] == "agent_service"
    assert by_id["agm_console_ready"]["needs_attention"] is False
    assert by_id["agm_console_missing"]["source_ref_status"] == "missing"
    assert by_id["agm_console_missing"]["needs_attention"] is True
    assert payload["profile_cards"][0]["profile"] == {"communication": {"style": "concise"}}
    assert payload["profile_cards"][0]["source_ref_status"] == "present"


def test_local_console_jobs_page_reports_ops_recovery_without_mutation(monkeypatch) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = _api()
    api.agentic_service = DownAgenticService()
    service = JobService(api.store)
    stale_job = service.submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary"})
    failed_digest = service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)
    running = api.store.claim_next_job(worker_id="worker_console", lease_seconds=30)
    assert running.job_id == stale_job.job_id
    running.leased_until = utc_now() - timedelta(seconds=5)
    api.store.claim_next_job(worker_id="worker_console", lease_seconds=30)
    api.store.fail_job(failed_digest.job_id, "FastReAct timed out", retryable=False)
    service.submit(DIGEST_VIA_FASTREACT, {"owner_user_id": "user_primary", "source_item_ids": ["src_backlog"]})

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/console/jobs")
        data_status, payload = _http_json(base_url, "GET", "/console/jobs/data?limit=10")

    assert page_status == 200
    assert "/console/jobs.js" in body
    assert "Recovery Commands" in body
    assert data_status == 200
    assert payload["read_only"] is True
    assert payload["requires_agentic_service_online"] is False
    assert payload["service_readiness"]["agentic_service_ok"] is False
    assert payload["worker_health"]["by_status"]["running"] == 1
    assert payload["worker_health"]["by_status"]["failed"] == 1
    assert payload["worker_health"]["stale_running_count"] == 1
    assert payload["digest_backlog"]["jobs"] == 1
    assert payload["recent_failed"][0]["job_id"] == failed_digest.job_id
    statuses = {issue["id"]: issue["status"] for issue in payload["issues"]}
    assert statuses["agentic_service"] == "agentic_service_down"
    assert statuses["stale_jobs"] == "stale_job"
    assert statuses["failed_digest"] == "failed_digest"
    assert "./scripts/pska job-recover --max-age-seconds 900" in payload["recommended_recovery_commands"]
    assert "./scripts/pska fastreact-digest-worker-command" in payload["recommended_recovery_commands"]
    assert "lsof -nP -iTCP:8765 -sTCP:LISTEN" in payload["recommended_recovery_commands"]
    assert "digest_via_fastreact backlog should be processed by the configured agentic service adapter" in payload["notes"][1]


def test_local_console_sources_page_reports_connectors_and_files_roots() -> None:
    api = _api()
    first = IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "files",
            "record_type": "note",
            "source_id": "console-source-file",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Console file source",
            "content": {"text": "Sources page should show files connector state."},
        }
    )
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "console-source-manual",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Console manual source",
            "content": {"text": "Sources page should show manual source channel."},
        }
    )
    api.store.upsert_connector_state(
        ConnectorState(
            connector_state_id="conn_user_primary_files",
            connector_id="files",
            owner_user_id="user_primary",
            enabled=True,
            scan_cursor="cursor_1",
            sync_status="succeeded",
            permission_scope={"roots": ["/Users/example/notes"]},
            config={"ignore": ["*.tmp"]},
        )
    )

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/console/sources")
        data_status, payload = _http_json(base_url, "GET", "/console/sources/data?owner_user_id=user_primary&limit=10")

    assert page_status == 200
    assert "/console/sources.js" in body
    assert "Files Commands" in body
    assert data_status == 200
    assert payload["read_only"] is True
    assert payload["source_counts"]["source_items"] == 2
    assert payload["source_counts"]["chunks"] == 2
    assert set(payload["source_channels"]) == {"files", "manual"}
    assert payload["source_channels"]["files"]["latest_source_item_id"] == first.source_item_id
    assert payload["connector_state"]["state_count"] == 1
    assert payload["connector_state"]["states"][0]["roots"] == ["/Users/example/notes"]
    assert payload["files"]["roots"] == ["/Users/example/notes"]
    assert "./scripts/pska files-sync --root /Users/example/notes" in payload["files"]["recommended_commands"]
    assert "./scripts/pska files-watch --root /Users/example/notes --initial-sync" in payload["recommended_commands"]
    assert "Knowledge Sources" in payload["notes"][0]


def test_cli_service_check_smokes_online_contract(monkeypatch, capsys) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = _api()
    api.agentic_service = DownAgenticService()
    with _http_server(api) as base_url:
        code = service_check(_namespace(url=f"http://{base_url}", service_token=None, timeout_seconds=2))

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["checks"]["health"]["ok"] is True
    assert payload["checks"]["ready"]["payload"]["checks"]["agentic_service"]["ok"] is False
    assert payload["checks"]["mcp_tools"]["has_pska_search"] is True
    assert payload["checks"]["database_alignment"]["ok"] is True


def test_cli_service_check_fails_on_database_mismatch(monkeypatch, capsys) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = _api()
    api.agentic_service = DownAgenticService()
    with _http_server(api) as base_url:
        code = service_check(
            _namespace(
                url=f"http://{base_url}",
                service_token=None,
                timeout_seconds=2,
                expected_database_url="postgresql:///different",
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["checks"]["database_alignment"] == {
        "ok": False,
        "expected": "postgresql:///different",
        "actual": "in_memory",
    }


def test_cli_service_check_uses_service_token(capsys) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    api = _api(service_token="secret")
    api.agentic_service = DownAgenticService()
    with _http_server(api) as base_url:
        blocked = service_check(_namespace(url=f"http://{base_url}", service_token=None, timeout_seconds=2))
        blocked_output = json.loads(capsys.readouterr().out)
        allowed = service_check(_namespace(url=f"http://{base_url}", service_token="secret", timeout_seconds=2))
        allowed_output = json.loads(capsys.readouterr().out)

    assert blocked == 1
    assert allowed == 0
    assert blocked_output["checks"]["ready"]["status"] == 401
    assert allowed_output["ok"] is True


def test_http_api_agent_service_needs_represented_user_for_private_search() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "private-agent-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Agent private note",
            "content": {"text": "agent private secret phrase"},
        }
    )
    with _http_server(api) as base_url:
        no_rep_status, no_rep = _http_json(
            base_url,
            "POST",
            "/search",
            {"query": "secret"},
            headers={"X-PSKA-Caller": "agent_service"},
        )
        rep_status, rep = _http_json(
            base_url,
            "POST",
            "/search",
            {"query": "secret"},
            headers={"X-PSKA-Caller": "agent_service", "X-PSKA-Represented-User-Id": "user_primary"},
        )

    assert no_rep_status == 200
    assert no_rep["results"] == []
    assert no_rep["request_user_id"] == "agent_service"
    assert rep_status == 200
    assert rep["results"][0]["title"] == "Agent private note"
    assert rep["request_user_id"] == "user_primary"


def test_http_mcp_agent_service_context_cannot_bypass_acl() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "mcp-agent-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "MCP agent note",
            "content": {"text": "mcp agent private phrase"},
        }
    )
    request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "pska_search",
            "arguments": {"query": "phrase", "user_id": "user_primary"},
        },
    }
    with _http_server(api) as base_url:
        no_rep_status, no_rep_response = _http_json(
            base_url,
            "POST",
            "/mcp",
            request,
            headers={"X-PSKA-Caller": "agent_service"},
        )
        rep_status, rep_response = _http_json(
            base_url,
            "POST",
            "/mcp",
            request,
            headers={"X-PSKA-Caller": "agent_service", "X-PSKA-Represented-User-Id": "user_primary"},
        )

    no_rep_payload = json.loads(no_rep_response["result"]["content"][0]["text"])
    rep_payload = json.loads(rep_response["result"]["content"][0]["text"])
    assert no_rep_status == 200
    assert no_rep_payload["results"] == []
    assert no_rep_payload["request_user_id"] == "agent_service"
    assert rep_status == 200
    assert rep_payload["results"][0]["title"] == "MCP agent note"
    assert rep_payload["request_user_id"] == "user_primary"


def test_postgres_graph_store_defaults_to_store_backed_neighbors() -> None:
    store = _store()
    graph = HypergraphService(store)
    graph.create_entity(Entity("ent_a", "person", "A", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_entity(Entity("ent_b", "project", "B", "user_primary", "private_primary", Visibility.PRIVATE))
    graph.create_hyperedge(
        relation_type="works_on",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        directionality=Directionality.DIRECTED,
        members=[("ent_a", "person"), ("ent_b", "project")],
    )

    edges = PostgresGraphStore(store).neighbors({"ent_a"})

    assert len(edges) == 1
    assert edges[0][0].relation_type == "works_on"


class FakeFastreact:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = []

    def ready(self) -> dict:
        return {"ok": True}

    def chat_completion(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.response


class FailingFastreact:
    def ready(self) -> dict:
        return {"ok": False}

    def chat_completion(self, **_kwargs) -> dict:
        raise FastreactError("Fastreact down")


class FakeAgenticService:
    def __init__(self, retrieval):
        self.retrieval = retrieval

    def ready(self):
        return {"ok": True, "provider": "test", "adapter": "fake"}

    def search(self, query, user, *, represented_user_id=None, max_iterations=3):
        retrieval = self.retrieval.search(query, user, represented_user_id=represented_user_id)
        return {
            "answer": "Fake external agentic answer.",
            "retrieval": to_jsonable(retrieval),
            "trace": {
                "query_understanding": {"intent": "test", "privacy_boundary": "acl_first"},
                "retrieval_plan": ["external_agentic_service", "pska_search"],
                "iterations": [{"iteration": "1", "query": query}],
                "evidence_check": "has_citations" if retrieval.citations else "insufficient_evidence",
            },
            "agentic_service": {"provider": "test", "adapter": "fake"},
        }


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store


def _api(*, service_token: str | None = None, auth: AuthConfig | None = None) -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.config = PSKAConfig(service=ServiceConfig(service_token=service_token), auth=auth or AuthConfig())
    api.store = _store()
    api.retrieval = RetrievalService(api.store, ACLService(api.store))
    api.agentic_service = FakeAgenticService(api.retrieval)
    api.ingest = IngestService(api.store)
    api.mcp = MCPServer("postgresql:///unused", store=api.store, config=api.config)
    api.jobs = JobService(api.store)
    api.reviews = ReviewService(api.store)
    api.candidates = CandidateWriteService(api.store)
    return api


def _minimal_ingest_payload(source_id: str) -> dict:
    return {
        "schema_version": "pska.channel_ingest.v1",
        "source_channel": "manual",
        "record_type": "note",
        "source_id": source_id,
        "owner_user_id": "user_primary",
        "space_id": "private_primary",
        "visibility": "private",
        "title": source_id,
        "content": {"text": f"{source_id} searchable content"},
    }


def _jwt(claims: dict, *, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_segment = _jwt_segment(header)
    payload_segment = _jwt_segment(claims)
    signing_input = f"{header_segment}.{payload_segment}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    signature_segment = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def _jwt_segment(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")


def _source_item():
    from pska_core.models import SourceItem

    return SourceItem(
        source_item_id="src_1",
        source_channel="manual",
        record_type="note",
        source_id="note_1",
        owner_user_id="user_primary",
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
        visible_team_ids=[],
        title="Note",
        url=None,
        content_text="Project Atlas depends on PSKA.",
        content_hash="hash_1",
    )


def _namespace(**kwargs):
    from argparse import Namespace

    return Namespace(**kwargs)


class _http_server:
    def __init__(self, api: PSKAApi) -> None:
        self.api = api
        self.server = None
        self.thread = None

    def __enter__(self) -> str:
        class Handler(PSKARequestHandler):
            pass

        Handler.api = self.api
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"{host}:{port}"

    def __exit__(self, *_args) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _http_json(base_url: str, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
    conn = HTTPConnection(base_url, timeout=5)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if body:
        request_headers.setdefault("content-type", "application/json")
    conn.request(method, path, body=body, headers=request_headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    if not data:
        return response.status, None
    return response.status, json.loads(data.decode("utf-8"))


def _http_text(base_url: str, method: str, path: str, headers: dict | None = None):
    conn = HTTPConnection(base_url, timeout=5)
    conn.request(method, path, headers=dict(headers or {}))
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    conn.close()
    return response.status, response_headers, data
