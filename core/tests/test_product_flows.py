from __future__ import annotations

import base64
import json
from pathlib import Path
import time
import zipfile

import pytest

from pska_core.acl import ACLService
from pska_core.api import PSKAApi
from pska_core.auth import RequestContext
from pska_core.candidates import CandidateWriteService
from pska_core.config import AuthConfig, DocumentParserConfig, FilesConfig, PSKAConfig, ServiceConfig
from pska_core.enums import UserRole
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.mcp_server import MCPServer
from pska_core.models import DEFAULT_TENANT_ID, DigestNote, SourceRef, User
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


def test_upload_source_uses_configured_spreadsheet_limits(tmp_path: Path) -> None:
    api = _api()
    api.config = PSKAConfig(
        service=ServiceConfig(),
        auth=AuthConfig(),
        files=FilesConfig(spreadsheet_max_rows_per_sheet=2, spreadsheet_max_columns=2),
    )
    workbook = tmp_path / "annual-report.xlsx"
    _write_minimal_xlsx(
        workbook,
        rows=[
            ["Metric", "Value", "Comment"],
            ["Revenue", "1200000", "audited"],
            ["Margin", "0.42", "audited"],
        ],
    )

    response = api.create_upload_source(
        {
            "filename": workbook.name,
            "bytes_base64": base64.b64encode(workbook.read_bytes()).decode("ascii"),
            "digest_mode": "manual",
        }
    )

    upload_item = api.store.source_items[response["source_item_ids"][0]]
    assert "| Metric | Value |" in upload_item.content_text
    assert "Comment" not in upload_item.content_text
    assert "Margin" not in upload_item.content_text
    extraction = upload_item.metadata["extra"]["extraction"]
    assert extraction["row_limit_per_sheet"] == 2
    assert extraction["column_limit"] == 2
    assert extraction["sheets"][0]["truncated_rows"] is True
    assert extraction["sheets"][0]["truncated_columns"] is True


def test_upload_spreadsheet_items_are_isolated_by_owner(tmp_path: Path) -> None:
    api = _api()
    api.store.add_user(User("user_secondary", "secondary", UserRole.USER))
    workbook = tmp_path / "shared.xlsx"
    _write_minimal_xlsx(
        workbook,
        rows=[
            ["Metric", "Value"],
            ["Revenue", "1200000"],
        ],
    )
    payload = {
        "source_id": "shared-spreadsheet",
        "uri": "pska-upload://unit-test/shared-spreadsheet",
        "filename": workbook.name,
        "bytes_base64": base64.b64encode(workbook.read_bytes()).decode("ascii"),
        "digest_mode": "manual",
    }

    primary = api.create_upload_source(payload, context=RequestContext(tenant_id=DEFAULT_TENANT_ID, user_id="user_primary"))
    secondary = api.create_upload_source(payload, context=RequestContext(tenant_id=DEFAULT_TENANT_ID, user_id="user_secondary"))
    primary_item = api.store.source_items[primary["source_item_ids"][0]]
    secondary_item = api.store.source_items[secondary["source_item_ids"][0]]

    assert primary_item.content_hash == secondary_item.content_hash
    assert primary_item.source_item_id != secondary_item.source_item_id
    assert primary_item.owner_user_id == "user_primary"
    assert secondary_item.owner_user_id == "user_secondary"


def test_upload_source_honors_configured_max_bytes() -> None:
    api = _api()
    api.config = PSKAConfig(service=ServiceConfig(), auth=AuthConfig(), files=FilesConfig(max_bytes=4))

    with pytest.raises(ValueError, match="uploaded file exceeds max_bytes"):
        api.create_upload_source(
            {
                "filename": "too-large.txt",
                "bytes_base64": base64.b64encode(b"12345").decode("ascii"),
                "digest_mode": "manual",
            }
        )


def test_upload_source_sanitizes_nul_characters_before_storing_config() -> None:
    api = _api()

    response = api.create_upload_source(
        {
            "filename": "nul-report.txt",
            "text": "before\x00after",
            "metadata": {"raw": "meta\x00value"},
            "digest_mode": "manual",
        }
    )

    source_item = api.store.source_items[response["source_item_ids"][0]]
    knowledge_source = next(iter(api.store.knowledge_sources.values()))
    assert "\x00" not in source_item.content_text
    assert "before\ufffdafter" in source_item.content_text
    assert "\x00" not in json.dumps(source_item.metadata, ensure_ascii=False)
    assert "\x00" not in json.dumps(knowledge_source.config, ensure_ascii=False)


def test_upload_source_uses_configured_document_parser_for_pdf_tables(monkeypatch) -> None:
    api = _api()
    api.config = PSKAConfig(
        service=ServiceConfig(),
        auth=AuthConfig(),
        document_parser=DocumentParserConfig(
            enabled=True,
            url="http://parser.test/rag/model_parser_file",
            return_json=True,
        ),
    )

    def fake_parser(path: Path, raw: bytes, config: DocumentParserConfig) -> dict[str, object]:
        assert path.name == "annual-report.pdf"
        assert raw.startswith(b"%PDF")
        assert config.return_json is True
        return {
            "code": "200",
            "status": "success",
            "content": "| Metric | Value |\n| --- | --- |\n| Revenue | 1200000 |",
            "json_content": json.dumps(
                {"parsing_res_list_merge": [{"block_label": "table", "block_content": "Revenue 1200000"}]}
            ),
        }

    monkeypatch.setattr("pska_core.files_connector._call_document_parser_server", fake_parser)

    response = api.create_upload_source(
        {
            "filename": "annual-report.pdf",
            "bytes_base64": base64.b64encode(b"%PDF-1.4\nmock").decode("ascii"),
            "digest_mode": "manual",
        }
    )

    upload_item = api.store.source_items[response["source_item_ids"][0]]
    assert "| Metric | Value |" in upload_item.content_text
    extraction = upload_item.metadata["extra"]["extraction"]
    assert extraction["extractor"] == "doc-parser-server"
    assert extraction["json_block_labels"]["table"] == 1


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


def test_ask_conversation_delete_archives_thread_without_cross_owner_access() -> None:
    api = _api()
    conversation = api.create_workspace_ask_conversation({"title": "Temporary thread"})["conversation"]
    conversation_id = conversation["conversation_id"]
    list(api.workspace_ask_conversation_event_stream(conversation_id, {"query": "你好", "intent": "quick"}))

    deleted = api.delete_workspace_ask_conversation(conversation_id)
    listed = api.workspace_ask_conversations()
    archived = api.workspace_ask_conversation(conversation_id)

    assert deleted["conversation"]["status"] == "archived"
    assert conversation_id not in {item["conversation_id"] for item in listed["conversations"]}
    assert archived["conversation"]["status"] == "archived"
    with pytest.raises(KeyError):
        api.delete_workspace_ask_conversation(conversation_id, {"owner_user_id": "other_user"})


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


def test_workspace_digest_run_consumes_worker_for_current_user(monkeypatch) -> None:
    api = _api()
    context = RequestContext(tenant_id="tenant_graphintell", user_id="test_user_2")
    response = api.create_text_source(
        {
            "title": "Manual digest source",
            "text": "Manual digest should schedule and consume the current user's digest job.",
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = response["source_item_ids"][0]
    captured = {}

    def fake_worker(args, _config):
        captured["tenant_id"] = args.tenant_id
        captured["owner_user_id"] = args.owner_user_id
        captured["job_id"] = args.job_id
        return [{"ok": True, "processed": 1, "stage": "fastreact_worker"}]

    monkeypatch.setattr("pska_core.cli._run_fastreact_digest_worker", fake_worker)

    digest = api.workspace_digest_run(
        {
            "source_item_ids": [source_item_id],
            "force": True,
            "run_worker": True,
        },
        context=context,
    )

    assert digest["ok"] is True
    assert digest["scheduled"]["owner_user_id"] == "test_user_2"
    assert digest["scheduled"]["tenant_id"] == "tenant_graphintell"
    assert digest["worker_status"]["processed"] == 1
    assert digest["summary"]["worker_processed"] == 1
    assert captured["tenant_id"] == "tenant_graphintell"
    assert captured["owner_user_id"] == "test_user_2"
    assert captured["job_id"] == digest["scheduled"]["job"]["job_id"]


def test_workspace_digest_run_defaults_to_background_queue() -> None:
    api = _api()
    context = RequestContext(tenant_id="tenant_graphintell", user_id="test_user_2")
    response = api.create_text_source(
        {
            "title": "Queued digest source",
            "text": "Workspace digest should queue background work without blocking the product UI.",
            "digest_mode": "manual",
        },
        context=context,
    )
    source_item_id = response["source_item_ids"][0]

    digest = api.workspace_digest_run(
        {
            "source_item_ids": [source_item_id],
            "force": True,
        },
        context=context,
    )

    assert digest["ok"] is True
    assert digest["mode"] == "queued"
    assert digest["queued"] is True
    assert digest["worker_runs"] == []
    assert digest["worker_status"]["requested"] is False
    assert digest["summary"]["queued_jobs"] == 1
    assert digest["scheduled"]["job"]["status"] == "queued"
    assert digest["scheduled"]["owner_user_id"] == "test_user_2"
    assert digest["scheduled"]["tenant_id"] == "tenant_graphintell"


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


def test_ask_intent_greeting_and_product_help_do_not_retrieve_user_sources() -> None:
    api = _api()
    api.create_text_source(
        {
            "title": "Private evidence",
            "text": "privatehelpkeyword should never be cited for greeting or product help.",
            "digest_mode": "manual",
        }
    )

    greeting = api.workspace_ask({"query": "你好", "intent": "auto"})
    product_help = api.workspace_ask({"query": "hello，你能做什么？", "intent": "auto"})

    assert greeting["intent"] == "greeting"
    assert greeting["answer_type"] == "direct_greeting"
    assert greeting["citations"] == []
    assert greeting["route"]["retrieval_owner"] == "none"
    assert product_help["intent"] == "product_help"
    assert product_help["answer_type"] == "product_help"
    assert product_help["citations"] == []


def test_ask_hard_scope_drops_out_of_scope_evidence() -> None:
    api = _api()
    first = api.create_text_source(
        {
            "title": "Scoped first document",
            "text": "The selected attachment discusses firstscopekeyword and bounded evidence.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    second = api.create_text_source(
        {
            "title": "Unselected second document",
            "text": "The unselected document contains secondscopekeyword and must not leak.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    user = api.store.get_user("user_primary", tenant_id=DEFAULT_TENANT_ID)

    retrieval = api.retrieval.search(
        "secondscopekeyword",
        user,
        source_item_ids={first},
        scope_mode="hard",
        top_k=5,
    )
    answer = api.workspace_ask(
        {
            "query": "这份附件讲了什么？",
            "intent": "auto",
            "scope": {"mode": "hard", "source_item_ids": [first]},
        }
    )

    assert {result.source_item_id for result in retrieval.results} == {first}
    assert second not in {citation["source_item_id"] for citation in answer["citations"]}
    assert answer["route"]["scope_applied"]["mode"] == "hard"
    assert answer["evidence_check"]["status"] == "supported"


def test_evidence_brief_can_be_created_from_ask_run() -> None:
    api = _api()
    api.create_text_source(
        {
            "title": "Ask brief source",
            "text": "briefaskkeyword is evidence that an Ask answer can become an Evidence Brief draft.",
            "digest_mode": "manual",
        }
    )
    conversation = api.create_workspace_ask_conversation({"title": "Brief from Ask"})
    conversation_id = conversation["conversation"]["conversation_id"]
    events = list(api.workspace_ask_conversation_event_stream(conversation_id, {"query": "What mentions briefaskkeyword?", "intent": "quick"}))
    saved = api.workspace_ask_conversation(conversation_id)
    run_id = saved["runs"][0]["run_id"]

    brief = api.workspace_evidence_brief_create({"ask_run_id": run_id})

    assert events[-1][0] == "done"
    assert events[-1][1]["answer"]
    assert events[-1][1]["citations"]
    assert brief["ok"] is True
    assert run_id in brief["brief"]["lineage"]["ask_run_ids"]
    assert any((node["metadata"] or {}).get("artifact_type") == "ask_run" for node in brief["nodes"])


def test_evidence_brief_from_ask_run_does_not_mix_latest_digest_notes() -> None:
    api = _api()
    stale_source = api.create_text_source(
        {
            "title": "Unrelated digest source",
            "text": "unrelatedbriefkeyword should not appear when the brief is scoped to an Ask run.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    api.store.add_digest_note(
        DigestNote(
            digest_note_id="dig_unrelated_for_ask_brief",
            owner_user_id="user_primary",
            title="Unrelated latest digest",
            synopsis="unrelatedbriefkeyword belongs to a different artifact.",
            source_refs=[SourceRef(source_item_id=stale_source)],
            confidence=0.9,
            tenant_id=DEFAULT_TENANT_ID,
        )
    )
    api.create_text_source(
        {
            "title": "Scoped Ask brief source",
            "text": "scopedaskbriefkeyword is the only evidence for this Ask-run brief.",
            "digest_mode": "manual",
        }
    )
    conversation = api.create_workspace_ask_conversation({"title": "Scoped Ask Brief"})
    conversation_id = conversation["conversation"]["conversation_id"]
    list(api.workspace_ask_conversation_event_stream(conversation_id, {"query": "What mentions scopedaskbriefkeyword?", "intent": "quick"}))
    run_id = api.workspace_ask_conversation(conversation_id)["runs"][0]["run_id"]

    brief = api.workspace_evidence_brief_create({"ask_run_id": run_id, "title": "Scoped Ask Brief"})
    board_text = "\n".join(
        "\n".join(str(value) for value in [node.get("title"), node.get("body_markdown")])
        for node in brief["nodes"]
    )

    assert brief["ok"] is True
    assert brief["brief"]["lineage"]["ask_run_ids"] == [run_id]
    assert brief["brief"]["lineage"]["digest_note_ids"] == []
    assert "scopedaskbriefkeyword" in board_text
    assert "unrelatedbriefkeyword" not in board_text


def test_unrelated_question_returns_no_answer_instead_of_random_citations() -> None:
    api = _api()
    api.create_text_source(
        {
            "title": "Orion knowledge memo",
            "text": "Orion Ledger uses BridgeTrace for source windows and review decisions.",
            "digest_mode": "manual",
        }
    )

    answer = api.workspace_ask({"query": "这个资料库能证明木星的质量是多少吗？", "intent": "quick", "top_k": 5})

    assert answer["answer_type"] == "no_answer"
    assert answer["citations"] == []
    assert answer["no_answer_reasons"]
    assert answer["evidence_check"]["query_anchors"]


def test_linking_digest_creates_topic_paths_and_delete_marks_supports() -> None:
    api = _api()
    first = api.create_text_source(
        {
            "title": "Commonprotocol Alpha integration memo",
            "text": "commonprotocol connects ingestion, review, and graph evidence for the first project.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    second = api.create_text_source(
        {
            "title": "Commonprotocol Beta integration memo",
            "text": "The second project also uses commonprotocol to connect graph evidence and review.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]

    linking = api.workspace_digest_linking_run({"source_item_ids": [first, second]})
    topics = api.workspace_graph_topics(query="commonprotocol")
    paths = api.workspace_graph_paths(query="commonprotocol")
    graph = api.workspace_graph_data()
    support_status_before = {support.status for support in api.store.artifact_supports.values()}
    deleted = api.workspace_documents_delete({"source_item_ids": [first], "execute": True, "reason": "test topic support delete"})
    support_status_after = {support.status for support in api.store.artifact_supports.values() if support.source_item_id == first}
    topics_after_delete = api.workspace_graph_topics(query="commonprotocol")
    refs_after_delete = {
        ref["source_item_id"]
        for topic in topics_after_delete["topics"]
        for ref in topic["source_refs"]
    }

    assert linking["topic_count"] >= 1
    assert linking["relationship_candidate_count"] >= 1
    assert any(topic["source_count"] >= 2 for topic in topics["topics"])
    assert all(topic["review_eligible"] for topic in topics["topics"] if topic["normalized_label"] == "commonprotocol")
    assert paths["topic_paths"]
    assert any(node["type"] == "topic" for node in graph["nodes"])
    assert "active" in support_status_before
    assert deleted["deleted"]["stale_artifact_supports"] >= 1
    assert support_status_after == {"evidence_removed"}
    assert first not in refs_after_delete
    assert second in refs_after_delete


def test_linking_digest_ignores_negated_topic_mentions() -> None:
    api = _api()
    first = api.create_text_source(
        {
            "title": "First protocol memo",
            "text": "commonprotocol coordinates source windows, digest review, and graph evidence.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    second = api.create_text_source(
        {
            "title": "Second protocol memo",
            "text": "The second memo also uses commonprotocol for reviewable graph evidence.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    negative = api.create_text_source(
        {
            "title": "commonprotocol unrelated operations memo",
            "text": "This operations memo intentionally does not mention commonprotocol and is unrelated to graph evidence.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]

    api.workspace_digest_linking_run({"source_item_ids": [first, second, negative]})
    topics = api.workspace_graph_topics(query="commonprotocol")
    topic = next(item for item in topics["topics"] if item["normalized_label"] == "commonprotocol")
    source_ids = {ref["source_item_id"] for ref in topic["source_refs"]}

    assert first in source_ids
    assert second in source_ids
    assert negative not in source_ids


def test_linking_digest_keeps_lexical_only_topics_diagnostic() -> None:
    api = _api()
    first = api.create_text_source(
        {
            "title": "Alpha field note",
            "text": "commonprotocol appears only as ordinary body text for the first source.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    second = api.create_text_source(
        {
            "title": "Beta research note",
            "text": "commonprotocol appears only as ordinary body text for the second source.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]

    linking = api.workspace_digest_linking_run({"source_item_ids": [first, second]})
    topics = api.workspace_graph_topics(query="commonprotocol")
    paths = api.workspace_graph_paths(query="commonprotocol")
    topic = next(item for item in topics["topics"] if item["normalized_label"] == "commonprotocol")

    assert linking["topic_count"] >= 1
    assert linking["relationship_candidate_count"] == 0
    assert topic["quality_tier"] == "diagnostic"
    assert topic["review_eligible"] is False
    assert paths["topic_paths"] == []


def test_hard_purge_removes_unreviewed_topic_derivatives() -> None:
    api = _api()
    first = api.create_text_source(
        {
            "title": "Sharedsignal first memo",
            "text": "Sharedsignal is present in a title-backed source.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    second = api.create_text_source(
        {
            "title": "Sharedsignal second memo",
            "text": "Sharedsignal is present in another title-backed source.",
            "digest_mode": "manual",
        }
    )["source_item_ids"][0]
    linking = api.workspace_digest_linking_run({"source_item_ids": [first, second]})

    purged = api.workspace_documents_delete({"source_item_ids": [first], "execute": True, "hard_delete": True, "reason": "test hard purge"})
    documents = api.workspace_documents_data()
    topics = api.workspace_graph_topics(query="sharedsignal")

    assert linking["relationship_candidate_count"] >= 1
    assert first not in api.store.source_items
    assert first not in {document["source_item_id"] for document in documents["documents"]}
    assert all(mention.source_item_id != first for mention in api.store.topic_mentions.values())
    assert all(support.source_item_id != first for support in api.store.artifact_supports.values())
    assert all(support.artifact_id not in {item["review_item_id"] for item in linking["review_items"]} for support in api.store.artifact_supports.values())
    assert all(review.review_item_id not in {item["review_item_id"] for item in linking["review_items"]} for review in api.store.review_items.values())
    assert first not in {ref["source_item_id"] for topic in topics["topics"] for ref in topic["source_refs"]}
    assert purged["deleted"]["source_items"] == 1


def _write_minimal_xlsx(path: Path, *, rows: list[list[str]]) -> None:
    def cell_name(row_index: int, column_index: int) -> str:
        letters = ""
        index = column_index
        while index:
            index, remainder = divmod(index - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return f"{letters}{row_index}"

    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            cells.append(
                f'<c r="{cell_name(row_index, column_index)}" t="inlineStr">'
                f"<is><t>{_xml_escape(value)}</t></is>"
                "</c>"
            )
        sheet_rows.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Pipeline" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
