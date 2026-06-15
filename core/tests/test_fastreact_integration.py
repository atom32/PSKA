from __future__ import annotations

from datetime import timedelta
from http.client import HTTPConnection
import threading
import json

from http.server import ThreadingHTTPServer

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.api import PSKAApi, PSKARequestHandler
import pska_core.api as api_module
from pska_core.candidates import CandidateWriteService
from pska_core.cli import service_check
from pska_core.enums import Directionality, ReviewType, UserRole, Visibility
from pska_core.fastreact_client import FastreactError, HttpFastreactClient, FastreactConfig
import pska_core.fastreact_client as fastreact_module
from pska_core.graph_store import PostgresGraphStore
from pska_core.hypergraph import HypergraphService
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, EXTRACT_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import Entity, ReviewItem, User, utc_now
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
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
    )

    assert response["run_id"] == "run_123"
    assert captured["url"] == "http://fastreact.test/v1/chat/completions"
    assert captured["timeout"] == 7
    assert captured["headers"]["X-fastreact-service-token"] == "token"
    assert captured["payload"]["user_key"] == "pska:user_primary"
    assert captured["payload"]["metadata"] == {
        "caller": "pska",
        "purpose": "extract",
        "pska_user_id": "user_primary",
        "pska_job_id": "job_123",
        "scope": {"source_item_ids": ["src_1"]},
    }


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
    assert ready["missing_pska_tools"] == ["pska_agentic_search", "pska_index_status", "pska_job_context", "pska_write_candidates"]


def test_fastreact_ready_accepts_namespaced_pska_tools(monkeypatch) -> None:
    namespaced_tools = [
        "pska_pska_search",
        "pska_pska_agentic_search",
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
        "pska_agentic_search",
        "pska_index_status",
        "pska_job_context",
        "pska_search",
        "pska_write_candidates",
        "pska_pska_agentic_search",
        "pska_pska_index_status",
        "pska_pska_job_context",
        "pska_pska_search",
        "pska_pska_write_candidates",
    }


def test_fastreact_job_records_run_id_and_event() -> None:
    store = _store()
    store.upsert_source_item(_source_item())
    service = JobService(store, fastreact=FakeFastreact({"run_id": "run_extract", "content": "done"}))
    job = service.submit(EXTRACT_VIA_FASTREACT, {"owner_user_id": "user_primary"}, max_attempts=1)

    completed = service.run_next()

    assert completed is not None
    assert completed.status == "succeeded"
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
    class DownFastreact:
        def ready(self):
            raise FastreactError("not reachable")

    monkeypatch.setattr(api_module, "HttpFastreactClient", DownFastreact)
    api = object.__new__(PSKAApi)
    api.store = _store()
    api.mcp = MCPServer("postgresql:///unused", store=api.store)

    ready = api.ready()

    assert ready["ok"] is True
    assert ready["checks"]["database"]["ok"] is True
    assert ready["checks"]["schema"]["ok"] is True
    assert ready["checks"]["mcp"]["ok"] is True
    assert "pska_search" in ready["checks"]["mcp"]["tools"]
    assert ready["checks"]["fastreact"]["ok"] is False


def test_api_ready_reports_job_worker_observability(monkeypatch) -> None:
    class DownFastreact:
        def ready(self):
            raise FastreactError("not reachable")

    monkeypatch.setattr(api_module, "HttpFastreactClient", DownFastreact)
    api = _api()
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
    assert {"pska_search", "pska_agentic_search", "pska_index_status"} <= set(names)
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
    assert first["skipped_source_item_ids"] == []
    assert second["scheduled_source_item_ids"] == [sources[0].source_item_id]
    assert sorted(second["skipped_source_item_ids"]) == sorted(source.source_item_id for source in sources[1:])
    assert forced["scheduled_source_item_ids"] == [sources[0].source_item_id]
    assert stats["digest_backlog"]["jobs"] == 3
    assert stats["digest_backlog"]["source_items"] == 3


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


def test_service_token_protects_non_health_routes(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_SERVICE_TOKEN", "secret")
    api = _api()
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


def test_cli_service_check_smokes_online_contract(monkeypatch, capsys) -> None:
    class DownFastreact:
        def ready(self):
            raise FastreactError("not reachable")

    monkeypatch.setattr(api_module, "HttpFastreactClient", DownFastreact)
    api = _api()
    with _http_server(api) as base_url:
        code = service_check(_namespace(url=f"http://{base_url}", service_token=None, timeout_seconds=2))

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["checks"]["health"]["ok"] is True
    assert payload["checks"]["ready"]["payload"]["checks"]["fastreact"]["ok"] is False
    assert payload["checks"]["mcp_tools"]["has_pska_search"] is True


def test_cli_service_check_uses_service_token(monkeypatch, capsys) -> None:
    class DownFastreact:
        def ready(self):
            raise FastreactError("not reachable")

    monkeypatch.setattr(api_module, "HttpFastreactClient", DownFastreact)
    monkeypatch.setenv("PSKA_SERVICE_TOKEN", "secret")
    api = _api()
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

    def ready(self) -> dict:
        return {"ok": True}

    def chat_completion(self, **_kwargs) -> dict:
        return self.response


class FailingFastreact:
    def ready(self) -> dict:
        return {"ok": False}

    def chat_completion(self, **_kwargs) -> dict:
        raise FastreactError("Fastreact down")


def _store() -> InMemoryKnowledgeStore:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    store.add_user(User("agent_service", "agent_service", UserRole.AGENT_SERVICE))
    return store


def _api() -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.store = _store()
    api.retrieval = RetrievalService(api.store, ACLService(api.store))
    api.agentic = AgenticSearchService(api.retrieval)
    api.mcp = MCPServer("postgresql:///unused", store=api.store)
    api.jobs = JobService(api.store)
    api.reviews = ReviewService(api.store)
    api.candidates = CandidateWriteService(api.store)
    return api


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
