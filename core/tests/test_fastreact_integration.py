from __future__ import annotations

from datetime import timedelta
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import threading

from http.server import ThreadingHTTPServer

from pska_core.acl import ACLService
from pska_core.agentic_service import AgenticServiceError
from pska_core.api import PSKAApi, PSKARequestHandler
from pska_core.candidates import CandidateWriteService
from pska_core.cli import service_check
from pska_core.enums import Directionality, MemoryLayer, ReviewType, UserRole, Visibility
from pska_core.fastreact_client import FastreactError, HttpFastreactClient, FastreactConfig
import pska_core.fastreact_client as fastreact_module
from pska_core.graph_store import PostgresGraphStore
from pska_core.hypergraph import HypergraphService
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, EXTRACT_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import AgentMemory, ConnectorState, Entity, ReviewItem, SourceRef, User, UserProfileCard, utc_now
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
    assert "exec" not in pska_tools
    assert "read_file" not in pska_tools
    assert pska_tools["write_file"] == "require_approval"
    assert pska_tools["edit_file"] == "require_approval"
    assert pska_tools["pska_pska_search"] == "allow"


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


def test_metrics_report_embedding_coverage_and_connector_freshness(monkeypatch) -> None:
    api = _api()
    monkeypatch.setenv("PSKA_EMBEDDING_PROVIDER", "fake-bge")
    monkeypatch.setenv("PSKA_EMBEDDING_MODEL", "fake-model")
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


def test_local_console_serves_dashboard_assets_when_service_token_enabled(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_SERVICE_TOKEN", "secret")
    api = _api()
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
                "query": "你好啊",
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
    assert payload["fallback"]["mode"] == "direct"
    assert "retrieval" in payload["fallback"]


def test_user_workspace_serves_assets_and_keeps_data_routes_token_protected(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_SERVICE_TOKEN", "secret")
    api = _api()
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
    assert "does not add connector scope" in payload["notes"][0]


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


def test_cli_service_check_uses_service_token(monkeypatch, capsys) -> None:
    class DownAgenticService:
        def ready(self):
            return {"ok": False, "provider": "test", "adapter": "fake", "error": "not reachable"}

    monkeypatch.setenv("PSKA_SERVICE_TOKEN", "secret")
    api = _api()
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

    def ready(self) -> dict:
        return {"ok": True}

    def chat_completion(self, **_kwargs) -> dict:
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


def _api() -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.store = _store()
    api.retrieval = RetrievalService(api.store, ACLService(api.store))
    api.agentic_service = FakeAgenticService(api.retrieval)
    api.ingest = IngestService(api.store)
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


def _http_text(base_url: str, method: str, path: str, headers: dict | None = None):
    conn = HTTPConnection(base_url, timeout=5)
    conn.request(method, path, headers=dict(headers or {}))
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    conn.close()
    return response.status, response_headers, data
