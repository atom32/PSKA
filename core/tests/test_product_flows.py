from __future__ import annotations

import base64
import time

from pska_core.acl import ACLService
from pska_core.api import PSKAApi
from pska_core.auth import RequestContext
from pska_core.candidates import CandidateWriteService
from pska_core.config import AuthConfig, PSKAConfig, ServiceConfig
from pska_core.enums import UserRole
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import DEFAULT_TENANT_ID, User
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.store import InMemoryKnowledgeStore


def test_text_and_upload_sources_create_documents_and_digest_jobs() -> None:
    api = _api()
    api.update_workspace_prompt_profiles(
        {
            "profiles": {
                "digest": {
                    "focus": "prefer reviewable claims",
                    "candidate_policy": "source_refs_required",
                }
            }
        }
    )

    text_response = api.create_text_source(
        {
            "title": "Pasted strategy note",
            "text": "Pasted notes should enter the private document library with stable references.",
        }
    )
    upload_response = api.create_upload_source(
        {
            "filename": "field-notes.md",
            "bytes_base64": base64.b64encode(b"# Field notes\nUploaded files share the same processing pipeline.").decode("ascii"),
            "digest_mode": "manual",
        }
    )

    text_item_id = text_response["source_item_ids"][0]
    upload_item_id = upload_response["source_item_ids"][0]
    text_item = api.store.source_items[text_item_id]
    upload_item = api.store.source_items[upload_item_id]
    jobs = api.store.list_jobs(tenant_id=DEFAULT_TENANT_ID, job_type=DIGEST_VIA_FASTREACT)

    assert text_response["ok"] is True
    assert text_response["sync_report"]["ingested"] == 1
    assert text_response["chunk_stats"]["count"] >= 1
    assert text_response["digest"]["scheduled_source_item_ids"] == [text_item_id]
    assert text_item.source_channel == "text"
    assert text_item.visibility == "private"
    assert upload_response["digest"] is None
    assert upload_response["sync_report"]["ingested"] == 1
    assert upload_item.source_channel == "upload"
    assert upload_item.title == "field-notes.md"
    assert upload_item.metadata["extra"]["extraction"]["extractor"] == "utf8"
    assert jobs[0].payload["triggered_by"] == "user_primary"
    assert jobs[0].payload["producer"] == "pska.digest_scheduler"
    assert jobs[0].payload["prompt_profile_type"] == "digest"
    assert jobs[0].payload["prompt_profile_id"]


def test_document_soft_delete_hides_retrieval_and_restore_recovers_it() -> None:
    api = _api()
    response = api.create_text_source(
        {
            "title": "Lifecycle note",
            "text": "Lifecycle deletion should remove uniquelifecyclekeyword from searchable evidence.",
            "digest_mode": "manual",
        }
    )
    source_item_id = response["source_item_ids"][0]
    user = api.store.get_user("user_primary", tenant_id=DEFAULT_TENANT_ID)

    before = api.retrieval.search("uniquelifecyclekeyword", user)
    dry_run = api.workspace_documents_delete({"source_item_ids": [source_item_id]})
    deleted = api.workspace_documents_delete({"source_item_ids": [source_item_id], "execute": True, "reason": "test delete"})
    after_delete = api.retrieval.search("uniquelifecyclekeyword", user)
    documents = api.workspace_documents_data()
    deleted_status = api.store.source_items[source_item_id].lifecycle_status
    restored = api.workspace_documents_delete({"source_item_ids": [source_item_id], "execute": True, "restore": True})
    after_restore = api.retrieval.search("uniquelifecyclekeyword", user)

    assert [result.source_item_id for result in before.results] == [source_item_id]
    assert dry_run["dry_run"] is True
    assert dry_run["counts"]["chunks"] >= 1
    assert deleted["deleted"]["source_items"] == 1
    assert deleted_status == "deleted"
    assert after_delete.results == []
    assert documents["documents"][0]["lifecycle_status"] == "deleted"
    assert restored["restore"] is True
    assert api.store.source_items[source_item_id].lifecycle_status == "active"
    assert [result.source_item_id for result in after_restore.results] == [source_item_id]


def test_prompt_profile_precedence_and_ask_conversation_lineage() -> None:
    api = _api()
    api.create_text_source(
        {
            "title": "Conversation evidence",
            "text": "Conversation answers should cite conversationproductkeyword evidence from the document library.",
            "digest_mode": "manual",
        }
    )
    api.update_workspace_prompt_profiles(
        {
            "profiles": [
                {"profile_type": "ask", "scope": "tenant", "config": {"style": "tenant-default", "answer_language": "zh-CN"}},
                {"profile_type": "ask", "scope": "user", "config": {"style": "user-override"}},
            ]
        }
    )

    profile_response = api.workspace_prompt_profiles()
    conversation_response = api.create_workspace_ask_conversation({"title": "Evidence thread"})
    conversation_id = conversation_response["conversation"]["conversation_id"]
    events = list(
        api.workspace_ask_conversation_event_stream(
            conversation_id,
            {"query": "What mentions conversationproductkeyword?", "intent": "quick"},
        )
    )
    saved = api.workspace_ask_conversation(conversation_id)

    assert profile_response["effective"]["ask"]["scope"] == "user"
    assert profile_response["effective"]["ask"]["config"]["answer_language"] == "zh-CN"
    assert profile_response["effective"]["ask"]["config"]["style"] == "user-override"
    assert events[0][0] == "conversation"
    assert events[-1][0] == "done"
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert saved["runs"][0]["status"] == "succeeded"
    assert saved["runs"][0]["prompt_profile_id"] == profile_response["effective"]["ask"]["prompt_profile_id"]
    assert saved["messages"][1]["citations"]


def test_digest_now_api_includes_tenant_context() -> None:
    api = _api()
    response = api.create_text_source(
        {
            "title": "Digest tenant note",
            "text": "Digest scheduling should carry tenant context from the API path.",
            "digest_mode": "manual",
        }
    )
    source_item_id = response["source_item_ids"][0]

    digest = api.digest_now(
        {
            "skip_sync": True,
            "max_worker_runs": 0,
            "source_item_ids": [source_item_id],
            "limit": 1,
        }
    )

    assert digest["ok"] is True
    assert digest["digest"]["tenant_id"] == DEFAULT_TENANT_ID
    assert digest["summary"]["scheduled_source_items"] == 1


def test_digest_now_api_uses_request_context_owner(monkeypatch) -> None:
    api = _api()
    context = RequestContext(tenant_id="tenant_graphintell", user_id="test_user")
    response = api.create_text_source(
        {
            "title": "Tenant digest note",
            "text": "Tenant-scoped digest scheduling should use the authenticated workspace owner.",
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = response["source_item_ids"][0]
    captured = {}

    def fake_worker(args, _config):
        captured["job_id"] = args.job_id
        return [{"ok": True, "processed": 0, "reason": "unit_test"}]

    monkeypatch.setattr("pska_core.cli._run_fastreact_digest_worker", fake_worker)

    digest = api.digest_now(
        {
            "skip_sync": True,
            "max_worker_runs": 1,
            "source_item_ids": [source_item_id],
            "limit": 1,
        },
        context=context,
    )

    assert digest["ok"] is True
    assert digest["digest"]["tenant_id"] == "tenant_graphintell"
    assert digest["digest"]["owner_user_id"] == "test_user"
    assert digest["digest"]["scheduled_source_item_ids"] == [source_item_id]
    assert digest["summary"]["scheduled_source_items"] == 1
    assert captured["job_id"] == digest["digest"]["job"]["job_id"]


def test_chinese_query_and_scoped_source_retrieval() -> None:
    api = _api()
    response = api.create_text_source(
        {
            "title": "中文人物说明",
            "text": "徐大为是一个在知识图谱、AI 与现实之间搭桥的人，长期关注技术落地。",
            "digest_mode": "manual",
        }
    )
    source_item_id = response["source_item_ids"][0]
    user = api.store.get_user("user_primary", tenant_id=DEFAULT_TENANT_ID)

    cjk = api.retrieval.search("看看徐大为是谁", user, top_k=1)
    scoped = api.retrieval.search("这份附件讲了什么", user, top_k=1, source_item_ids={source_item_id})
    quick = api._workspace_ask_quick(
        query="这份附件讲了什么",
        scope={"source_item_ids": [source_item_id]},
        intent="quick",
        surface="today",
        tenant_id=DEFAULT_TENANT_ID,
        owner_user_id="user_primary",
        represented_user_id="user_primary",
        user=user,
        top_k=1,
        started_at=time.perf_counter(),
    )

    assert [result.source_item_id for result in cjk.results] == [source_item_id]
    assert [result.source_item_id for result in scoped.results] == [source_item_id]
    assert scoped.results[0].source == "scope"
    assert scoped.score_debug["ranker"] == "scoped_source"
    assert quick["evidence"]["source_windows"]
    assert quick["evidence"]["results"][0]["passage_window_id"]
    assert "知识图谱" in quick["evidence"]["source_windows"][0]["text"]


def _api() -> PSKAApi:
    api = object.__new__(PSKAApi)
    api.config = PSKAConfig(service=ServiceConfig(), auth=AuthConfig())
    api.store = InMemoryKnowledgeStore()
    api.store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    api.retrieval = RetrievalService(api.store, ACLService(api.store))
    api.agentic_service = object()
    api.ingest = IngestService(api.store)
    api.mcp = MCPServer("postgresql:///unused", store=api.store, config=api.config)
    api.jobs = JobService(api.store)
    api.reviews = ReviewService(api.store)
    api.candidates = CandidateWriteService(api.store)
    return api
