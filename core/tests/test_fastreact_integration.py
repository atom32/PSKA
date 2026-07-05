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

import pytest
from http.server import ThreadingHTTPServer

from pska_core.acl import ACLService
from pska_core.agentic_service import AgenticServiceError, _agentic_messages, normalize_agentic_event_response
from pska_core.api import (
    PSKAApi,
    PSKARequestHandler,
    _ask_agent_steps_from_events,
    _ask_answer_quality_flags,
    _ask_clean_evidence_text,
    _ask_hydrate_retrieval_source_windows,
    _ask_is_stream_done_event,
    _ask_public_trace_event,
    _ask_query_terms,
    _ask_quick_answer,
    _ask_route_intent,
    _ask_retrieval_from_agentic_trace,
    _ask_structural_evidence_hits,
    _ask_validate_source_refs,
    _ask_verify_evidence,
)
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
from pska_core.models import AgentMemory, Chunk, ConnectorState, DigestNote, DiscoveryItem, Document, Entity, KnowledgeBase, KnowledgeBaseSourceItem, KnowledgeClaim, ReviewItem, SourceItem, SourceRef, User, UserProfileCard, utc_now
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
        tool_policy={"mode": "allowlist", "allowed_tools": ["pska_pska_search"]},
    )

    assert captured["payload"]["user_key"] == "pska:user_primary"
    assert captured["payload"]["metadata"]["tenant_key"] == "tenant_acme"
    assert captured["payload"]["metadata"]["pska_tenant_id"] == "tenant_acme"
    assert captured["payload"]["tool_policy"] == {"mode": "allowlist", "allowed_tools": ["pska_pska_search"]}
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

    assert "use only PSKA read-only MCP tools" in joined
    assert "pska_read_evidence_context" in joined
    assert "pska_graph_context" in joined
    assert "pska_digest_context" in joined
    assert "do not call read_file" in joined
    assert "exec" in joined
    assert "retrieve source evidence through PSKA tools" in joined
    assert "4-8 concrete bullets" in joined


def test_agentic_search_prompt_includes_pska_tenant_identity() -> None:
    messages = _agentic_messages("What is known?", tenant_id="tenant_acme", user_id="alice")
    system = messages[0]["content"]

    assert "tenant_id='tenant_acme'" in system
    assert "user_id='alice'" in system
    assert "PSKA MCP identity is forwarded by the runtime" in system
    assert "do not invent or override tenant/user arguments" in system


def test_ask_agent_steps_ignore_sse_done_frames() -> None:
    assert _ask_is_stream_done_event({"type": "done", "content": "[DONE]"}) is True
    assert _ask_is_stream_done_event({"type": "message", "content": "[DONE]"}) is True

    steps = _ask_agent_steps_from_events(
        [
            {"type": "session_start", "event_id": "run:1"},
            {"type": "message", "content": "[DONE]"},
            {"type": "done", "content": "[DONE]"},
        ]
    )

    assert [step["phase"] for step in steps] == ["understand"]


def test_ask_deep_retrieval_extracts_pska_tool_result_citations() -> None:
    trace = {
        "events": [
            {
                "type": "tool_result",
                "tool_name": "pska_pska_search",
                "content": json.dumps(
                    {
                        "results": [
                            {
                                "title": "companies-acme-example.md",
                                "snippet": "Founded by Alice.",
                                "citation": {
                                    "source_item_id": "src_acme",
                                    "chunk_id": "chk_acme_0",
                                    "title": "companies-acme-example.md",
                                },
                            }
                        ]
                    }
                ),
            }
        ]
    }

    retrieval = _ask_retrieval_from_agentic_trace(trace)

    assert retrieval["results"][0]["source_item_id"] == "src_acme"
    assert retrieval["citations"] == [
        {
            "source_item_id": "src_acme",
            "chunk_id": "chk_acme_0",
            "title": "companies-acme-example.md",
            "url": None,
            "snippet": None,
        }
    ]


def test_ask_quality_allows_report_markdown_headings() -> None:
    answer = "## 结论\n\n- acme-example 是一家垂直 AI 公司。[来源：companies-acme-example.md]"

    assert _ask_answer_quality_flags(answer) == []


def test_ask_quick_clean_evidence_removes_inline_frontmatter_and_headings() -> None:
    cleaned = _ask_clean_evidence_text(
        "--- title: acme-example type: company --- # acme-example Founded 2024. ## State - Active.\n| a | b |\n| --- | --- |"
    )

    assert "---" not in cleaned
    assert "#" not in cleaned
    assert "title:" not in cleaned
    assert "| --- |" not in cleaned
    assert "acme-example Founded 2024" in cleaned
    assert "a / b" in cleaned


def test_ask_quick_extracts_row_label_financial_table_values_by_year() -> None:
    retrieval = {
        "results": [
            {
                "source_item_id": "src_financial_report",
                "title": "annual-report.pdf",
                "snippet": (
                    "| 项目 | 2025年 | 2024年 |\n"
                    "| --- | --- | --- |\n"
                    "| 营业收入（元） | 92,507,796,069.94 | 92,495,525,118.30 |\n"
                    "| 归属于上市公司股东的净利润（元） | 14,195,371,894.42 | 11,977,327,023.54 |\n"
                    "| 基本每股收益（元/股） | 1.546 | 1.297 |"
                ),
            }
        ]
    }

    answer = _ask_quick_answer(
        "2025年的营业收入（元）、归属于上市公司股东的净利润（元）、基本每股收益（元/股）分别是多少？",
        retrieval,
    )

    assert "营业收入（元） = 92,507,796,069.94" in answer
    assert "归属于上市公司股东的净利润（元） = 14,195,371,894.42" in answer
    assert "基本每股收益（元/股） = 1.546" in answer
    assert "11,977,327,023.54" not in answer


def test_ask_quick_extracts_plain_pdf_table_values_by_label() -> None:
    retrieval = {
        "results": [
            {
                "source_item_id": "src_pdf_report",
                "title": "annual-report.pdf",
                "snippet": (
                    "六、主要会计数据和财务指标 公司是否需追溯调整或重述以前年度会计数据 □ 是 √ 否 "
                    "2025 年 2024 年 本年比上年 增减 2023 年 "
                    "营业收入（元） 92,507,796,069.94 92,495,525,118.30 0.01% 89,341,177,610.40 "
                    "归属于上市公司股东的净利润（元） 14,195,371,894.42 11,977,327,023.54 18.52% 14,108,439,648.97 "
                    "经营活动产生的现金流量净额 25,339,411,083.10 13,264,092,022.73 91.04%"
                ),
            }
        ]
    }

    answer = _ask_quick_answer(
        "只根据当前知识库回答：海康威视2025年年度报告“主要会计数据和财务指标”表中，2025年营业收入（元）、归属于上市公司股东的净利润（元）、经营活动产生的现金流量净额（元）分别是多少？请给出精确数字。",
        retrieval,
    )

    assert "营业收入（元） = 92,507,796,069.94" in answer
    assert "归属于上市公司股东的净利润（元） = 14,195,371,894.42" in answer
    assert "经营活动产生的现金流量净额（元） = 25,339,411,083.10" in answer
    assert "92,495,525,118.30" not in answer


def test_ask_quick_plain_pdf_table_ignores_partial_label_matches() -> None:
    retrieval = {
        "results": [
            {
                "source_item_id": "src_pdf_report",
                "title": "annual-report.pdf",
                "snippet": (
                    "公司研发投入情况 2025 年 2024 年 变动比例 "
                    "研发投入占营业收入比例 12.70% 12.83% -0.13% "
                    "5、现金流 单位：元 项目 2025 年 2024 年 同比增减 "
                    "经营活动产生的现金流量净额 25,339,411,083.10 13,264,092,022.73 91.04%"
                ),
            },
            {
                "source_item_id": "src_pdf_report",
                "title": "annual-report.pdf",
                "snippet": (
                    "六、主要会计数据和财务指标 2025 年 2024 年 本年比上年 增减 2023 年 "
                    "营业收入（元） 92,507,796,069.94 92,495,525,118.30 0.01% 89,341,177,610.40 "
                    "归属于上市公司股东的净利润（元） 14,195,371,894.42 11,977,327,023.54 18.52% 14,107,726,276.26 "
                    "经营活动产生的现金流量净额（元） 25,339,411,083.10 13,264,092,022.73 91.04%"
                ),
            },
        ]
    }

    answer = _ask_quick_answer(
        "只根据当前知识库回答：2025年营业收入（元）、归属于上市公司股东的净利润（元）、经营活动产生的现金流量净额（元）分别是多少？请给出精确数字。",
        retrieval,
    )

    assert "营业收入（元） = 92,507,796,069.94" in answer
    assert "归属于上市公司股东的净利润（元） = 14,195,371,894.42" in answer
    assert "经营活动产生的现金流量净额（元） = 25,339,411,083.10" in answer
    assert "营业收入（元） = 12.70%" not in answer


def test_ask_quick_plain_pdf_table_selects_requested_year_column() -> None:
    retrieval = {
        "results": [
            {
                "source_item_id": "src_pdf_report",
                "title": "annual-report.pdf",
                "snippet": (
                    "六、主要会计数据和财务指标 2025 年 2024 年 本年比上年 增减 2023 年 "
                    "营业收入（元） 92,507,796,069.94 92,495,525,118.30 0.01% 89,341,177,610.40 "
                    "归属于上市公司股东的净利润（元） 14,195,371,894.42 11,977,327,023.54 18.52% 14,107,726,276.26 "
                    "经营活动产生的现金流量净额（元） 25,339,411,083.10 13,264,092,022.73 91.04%"
                ),
            }
        ]
    }

    answer = _ask_quick_answer(
        "只根据当前知识库回答：2025年年度报告表中，2024年营业收入（元）、归属于上市公司股东的净利润（元）、经营活动产生的现金流量净额（元）分别是多少？请给出精确数字。",
        retrieval,
    )

    assert "营业收入（元） = 92,495,525,118.30" in answer
    assert "归属于上市公司股东的净利润（元） = 11,977,327,023.54" in answer
    assert "经营活动产生的现金流量净额（元） = 13,264,092,022.73" in answer
    assert "92,507,796,069.94" not in answer


def test_ask_quick_extracts_requested_columns_from_matching_wide_table_row() -> None:
    retrieval = {
        "results": [
            {
                "source_item_id": "src_mock_table",
                "title": "mock-large-table.md",
                "snippet": (
                    "| RowID | Year | Quarter | Segment | Region | Revenue_million | GrossMargin_pct | OperatingProfit_million | InventoryDays | R&D_million | FreeCashFlow_million |\n"
                    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                    "| MOCK-1010 | 2025 | Q4 | Robotics | Americas | 100.00 | 10.00 | 10.00 | 30 | 5.00 | 8.00 |\n"
                    "| MOCK-1132 | 2025 | Q4 | Robotics | Europe | 1816.00 | 34.15 | 610.56 | 62 | 125.80 | 406.50 |"
                ),
            }
        ]
    }

    answer = _ask_quick_answer(
        "在表中，2025 年 Q4、Segment=Robotics、Region=Europe 这一行的 Revenue_million、GrossMargin_pct、OperatingProfit_million、InventoryDays、R&D_million、FreeCashFlow_million 各是多少？",
        retrieval,
    )

    assert "Revenue_million = 1816.00" in answer
    assert "GrossMargin_pct = 34.15" in answer
    assert "OperatingProfit_million = 610.56" in answer
    assert "InventoryDays = 62" in answer
    assert "R&D_million = 125.80" in answer
    assert "FreeCashFlow_million = 406.50" in answer
    assert "Year = 2025" not in answer
    assert "Region = Europe" not in answer


def test_ask_source_window_uses_retrieved_chunk_for_large_table_rows() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    source_item_id = "src_large_table"
    document_id = "doc_large_table"
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="upload",
            record_type="file",
            source_id="large-table.xlsx",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="large-table.xlsx",
            url=None,
            content_text="table body",
            content_hash="hash_large_table",
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="large-table.xlsx",
            body="| RowNo | BorrowerId |\n| 1 | BOR-FIRST |",
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_large_table_834",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "| RowNo | BorrowerId | DrawnCNYmm | LTV | InternalRating | ECLStage | Checksum |\n"
                "| 1375 | BOR-NOISE | 11.00 | 0.1100 | BBB | Stage 1 | CHK-NOISE |\n"
                "| 1376 | BOR-TXC-HLD-1376 | 654.32 | 0.7261 | AA- | Stage 2 | CHK-TXC-1376-4812 |"
            ),
            ordinal=834,
        )
    )
    retrieval = {
        "results": [
            {
                "source_item_id": source_item_id,
                "document_id": document_id,
                "chunk_id": "chk_large_table_834",
                "title": "large-table.xlsx",
                "snippet": "| 1376 | BOR-TXC-HLD-1376 |",
                "citation": {"source_item_id": source_item_id, "chunk_id": "chk_large_table_834", "title": "large-table.xlsx"},
            }
        ],
        "citations": [{"source_item_id": source_item_id, "chunk_id": "chk_large_table_834", "title": "large-table.xlsx"}],
    }

    hydrated = _ask_hydrate_retrieval_source_windows(
        store,
        retrieval,
        query="RowNo 1376 BOR-TXC-HLD-1376 DrawnCNYmm LTV InternalRating ECLStage Checksum",
        tenant_id="tenant_default",
        owner_user_id="user_primary",
    )

    window_text = hydrated["source_windows"][0]["text"]
    assert "BOR-TXC-HLD-1376" in window_text
    assert "654.32" in window_text
    assert "CHK-TXC-1376-4812" in window_text
    assert "BOR-FIRST" not in window_text


def test_ask_source_window_adds_previous_table_header_for_split_rows() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    source_item_id = "src_split_table"
    document_id = "doc_split_table"
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="upload",
            record_type="file",
            source_id="split-table.md",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="split-table.md",
            url=None,
            content_text="split table body",
            content_hash="hash_split_table",
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="split-table.md",
            body="split table body",
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_split_header",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "| RowID | Year | Quarter | Segment | Region | Revenue_million | GrossMargin_pct | OperatingProfit_million | InventoryDays | R&D_million | FreeCashFlow_million |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
            ),
            ordinal=1,
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_split_target",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "| MOCK-1131 | 2025 | Q4 | Robotics | Domestic West | 1799.00 | 34.40 | 609.26 | 59 | 125.80 | 402.47 |\n"
                "| MOCK-1132 | 2025 | Q4 | Robotics | Europe | 1816.00 | 34.15 | 610.56 | 62 | 125.80 | 406.50 |"
            ),
            ordinal=100,
        )
    )
    retrieval = {
        "results": [
            {
                "source_item_id": source_item_id,
                "document_id": document_id,
                "chunk_id": "chk_split_target",
                "title": "split-table.md",
                "snippet": "| MOCK-1132 | 2025 | Q4 | Robotics | Europe |",
                "citation": {"source_item_id": source_item_id, "chunk_id": "chk_split_target", "title": "split-table.md"},
            }
        ],
        "citations": [{"source_item_id": source_item_id, "chunk_id": "chk_split_target", "title": "split-table.md"}],
    }

    query = "RowID MOCK-1132 这一行的 Revenue_million、GrossMargin_pct、OperatingProfit_million、InventoryDays、R&D_million、FreeCashFlow_million 分别是多少？只输出字段=值。"
    hydrated = _ask_hydrate_retrieval_source_windows(
        store,
        retrieval,
        query=query,
        tenant_id="tenant_default",
        owner_user_id="user_primary",
    )
    answer = _ask_quick_answer(query, hydrated)

    assert "Revenue_million = 1816.00" in answer
    assert "GrossMargin_pct = 34.15" in answer
    assert "OperatingProfit_million = 610.56" in answer
    assert "InventoryDays = 62" in answer
    assert "R&D_million = 125.80" in answer
    assert "FreeCashFlow_million = 406.50" in answer
    assert "MOCK-1131" not in answer


def test_ask_validate_source_only_refs_selects_relevant_chunk() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    source_item_id = "src_annual_report"
    document_id = "doc_annual_report"
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="upload",
            record_type="file",
            source_id="annual-report.pdf",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="annual-report.pdf",
            url=None,
            content_text="annual report",
            content_hash="hash_annual_report",
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="annual-report.pdf",
            body="annual report body",
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_report_toc",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text="目录 公司简介 重要提示",
            ordinal=0,
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_report_metrics",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "| 项目 | 2025年 |\n"
                "| 营业收入（元） | 92,507,796,069.94 |\n"
                "| 归属于上市公司股东的净利润（元） | 14,195,371,894.42 |"
            ),
            ordinal=8,
        )
    )

    refs, dropped = _ask_validate_source_refs(
        [{"source_item_id": source_item_id, "title": "annual-report.pdf"}],
        store=store,
        tenant_id="tenant_default",
        owner_user_id="user_primary",
        query="2025 营业收入 92,507,796,069.94 归属于上市公司股东的净利润 14,195,371,894.42",
    )

    assert dropped == []
    assert refs[0]["chunk_id"] == "chk_report_metrics"
    assert "92,507,796,069.94" in refs[0]["snippet"]


def test_retrieval_prioritizes_exact_table_identifiers() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    source_item_id = "src_exact_table"
    document_id = "doc_exact_table"
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="manual",
            record_type="note",
            source_id="exact-table",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="exact table",
            url=None,
            content_text="large exact table",
            content_hash="hash_exact_table",
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="exact table",
            body="exact table body",
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_exact_noise",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text="Robotics Europe Q4 Revenue_million GrossMargin_pct repeated summary without the target row id.",
            ordinal=1,
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_exact_target",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text="| RowID | Year | Quarter | Segment | Region | Revenue_million |\n| MOCK-1132 | 2025 | Q4 | Robotics | Europe | 1816.00 |",
            ordinal=2,
        )
    )

    user = store.get_user("user_primary", tenant_id="tenant_default")
    response = RetrievalService(store, ACLService(store)).search(
        "RowID MOCK-1132 Revenue_million 是多少？",
        user,
        represented_user_id="user_primary",
        top_k=1,
    )

    assert response.results[0].result_id == "chk_exact_target"
    assert response.results[0].score_debug["exact_identifier"] == 1.0


def test_retrieval_prioritizes_complete_metric_table_chunks() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    source_item_id = "src_metric_table"
    document_id = "doc_metric_table"
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="upload",
            record_type="file",
            source_id="annual-report.pdf",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="annual-report.pdf",
            url=None,
            content_text="annual report",
            content_hash="hash_metric_table",
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="annual-report.pdf",
            body="annual report body",
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_metric_noise",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "海康威视 2025 年年度报告 主要会计数据 财务指标 2025 年 2024 年 "
                "研发投入占营业收入比例 12.70% 12.83% -0.13% "
                "主要会计数据 财务指标 年度报告 营业收入"
            ),
            ordinal=81,
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_metric_target",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "六、主要会计数据和财务指标 2025 年 2024 年 本年比上年 增减 2023 年 "
                "营业收入（元） 92,507,796,069.94 92,495,525,118.30 0.01% 89,341,177,610.40 "
                "归属于上市公司股东的净利润（元） 14,195,371,894.42 11,977,327,023.54 18.52% 14,107,726,276.26 "
                "经营活动产生的现金流量净额（元） 25,339,411,083.10 13,264,092,022.73 91.04%"
            ),
            ordinal=9,
        )
    )

    user = store.get_user("user_primary", tenant_id="tenant_default")
    response = RetrievalService(store, ACLService(store)).search(
        "只根据当前知识库回答：海康威视2025年年度报告主要会计数据和财务指标表中，2025年营业收入（元）、归属于上市公司股东的净利润（元）、经营活动产生的现金流量净额（元）分别是多少？",
        user,
        represented_user_id="user_primary",
        top_k=1,
    )

    assert response.results[0].result_id == "chk_metric_target"
    assert response.results[0].score_debug["metric_phrase_match"] > 0


def test_retrieval_prefers_matching_report_year_source() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    for year, source_item_id, document_id, chunk_id, revenue in [
        ("2025", "src_report_2025", "doc_report_2025", "chk_report_2025", "92,507,796,069.94"),
        ("2024", "src_report_2024", "doc_report_2024", "chk_report_2024", "92,495,525,118.30"),
    ]:
        store.upsert_source_item(
            SourceItem(
                source_item_id=source_item_id,
                source_channel="upload",
                record_type="file",
                source_id=f"annual-report-{year}.pdf",
                owner_user_id="user_primary",
                space_id="private_primary",
                visibility=Visibility.PRIVATE,
                visible_team_ids=[],
                title=f"annual-report-{year}.pdf",
                url=None,
                content_text=f"{year} annual report",
                content_hash=f"hash_report_{year}",
            )
        )
        store.add_document(
            Document(
                document_id=document_id,
                source_item_id=source_item_id,
                owner_user_id="user_primary",
                space_id="private_primary",
                visibility=Visibility.PRIVATE,
                visible_team_ids=[],
                title=f"annual-report-{year}.pdf",
                body=f"{year} annual report body",
            )
        )
        store.add_chunk(
            Chunk(
                chunk_id=chunk_id,
                document_id=document_id,
                source_item_id=source_item_id,
                owner_user_id="user_primary",
                space_id="private_primary",
                visibility=Visibility.PRIVATE,
                visible_team_ids=[],
                text=(
                    f"六、主要会计数据和财务指标 {year} 年 2023 年 "
                    f"营业收入（元） {revenue} 89,341,177,610.40 "
                    "归属于上市公司股东的净利润（元） 11,977,327,023.54 14,107,726,276.26 "
                    "经营活动产生的现金流量净额（元） 13,264,092,022.73 16,622,209,721.05"
                ),
                ordinal=10,
            )
        )

    user = store.get_user("user_primary", tenant_id="tenant_default")
    response = RetrievalService(store, ACLService(store)).search(
        "只根据当前知识库回答：2024年年度报告主要会计数据和财务指标表中，2024年营业收入（元）是多少？",
        user,
        represented_user_id="user_primary",
        top_k=1,
    )

    assert response.results[0].source_item_id == "src_report_2024"
    assert response.results[0].score_debug["document_year_match"] > 0


def test_mcp_read_evidence_context_focuses_wide_table_chunk() -> None:
    store = InMemoryKnowledgeStore()
    store.add_user(User("user_primary", "primary", UserRole.ADMIN))
    source_item_id = "src_mcp_wide_table"
    document_id = "doc_mcp_wide_table"
    filler_columns = " | ".join(f"Filler{i}" for i in range(80))
    store.upsert_source_item(
        SourceItem(
            source_item_id=source_item_id,
            source_channel="upload",
            record_type="file",
            source_id="mcp-wide-table.xlsx",
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="mcp-wide-table.xlsx",
            url=None,
            content_text="wide table body",
            content_hash="hash_mcp_wide_table",
        )
    )
    store.add_document(
        Document(
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            title="mcp-wide-table.xlsx",
            body="| RowNo | BorrowerId |\n| 1 | BOR-FIRST |",
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_mcp_wide_table_01",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "| CheckId | RowNo | RelatedFacilityId | Rule |\n"
                "| AUDIT-1376 | 1376 | FAC-TXC-1376 | sample cross-check row |"
            ),
            ordinal=1,
        )
    )
    store.add_chunk(
        Chunk(
            chunk_id="chk_mcp_wide_table_99",
            document_id=document_id,
            source_item_id=source_item_id,
            owner_user_id="user_primary",
            space_id="private_primary",
            visibility=Visibility.PRIVATE,
            visible_team_ids=[],
            text=(
                "| RowNo | BorrowerId | DrawnCNYmm | LTV | InternalRating | ECLStage | Checksum | Notes |\n"
                f"| 1375 | BOR-NOISE | 11.00 | 0.1100 | BBB | Stage 1 | CHK-NOISE | {filler_columns} |\n"
                "| 1376 | BOR-TXC-HLD-1376 | 654.32 | 0.7261 | AA- | Stage 2 | CHK-TXC-1376-4812 | target row |"
            ),
            ordinal=99,
        )
    )
    server = MCPServer("postgresql:///unused", store=store)

    payload = server.pska_read_evidence_context(
        {
            "query": "RowNo 1376 BOR-TXC-HLD-1376 DrawnCNYmm LTV InternalRating ECLStage Checksum",
            "source_refs": [{"source_item_id": source_item_id}],
            "max_chunk_chars": 700,
        }
    )

    assert payload["chunks"][0]["chunk_id"] == "chk_mcp_wide_table_99"
    assert payload["citations"][0]["chunk_id"] == "chk_mcp_wide_table_99"
    chunk_text = payload["chunks"][0]["text"]
    assert "BOR-TXC-HLD-1376" in chunk_text
    assert "654.32" in chunk_text
    assert "CHK-TXC-1376-4812" in chunk_text
    assert "1375" not in chunk_text
    assert max(len(line) for line in chunk_text.splitlines()) <= 500


def test_ask_evidence_check_accepts_structural_contact_anchors() -> None:
    evidence = {
        "results": [
            {
                "source_item_id": "src_pdf",
                "chunk_id": "chk_tail",
                "title": "annual-report.pdf",
                "snippet": "Corporate office tel 852-217 95122 web www.example.com",
                "source_window": {
                    "source_item_id": "src_pdf",
                    "document_id": "doc_pdf",
                    "chunk_id": "chk_tail",
                    "title": "annual-report.pdf",
                    "text": "Corporate office tel 852-217 95122 web www.example.com",
                },
            }
        ],
        "citations": [
            {
                "source_item_id": "src_pdf",
                "document_id": "doc_pdf",
                "chunk_id": "chk_tail",
                "title": "annual-report.pdf",
            }
        ],
        "source_windows": [],
    }

    check = _ask_verify_evidence(
        query="年报最后一页的联系电话和网址是什么？",
        evidence=evidence,
        scope={},
        ask_intent="quick",
    )

    assert check["status"] == "supported"
    assert check["used_citations"][0]["support_hits"][-2:] == ["url", "phone"]


def test_ask_quick_answer_prioritizes_structured_contact_values() -> None:
    answer = _ask_quick_answer(
        "年报最后一页的联系电话和网址是什么？",
        {
            "results": [
                {
                    "snippet": "tail page garbled label j852-217 95122 label jwww.example.com office j86-755-86013388",
                }
            ],
            "diagnostics": {},
        },
    )

    assert "网址：www.example.com" in answer
    assert "联系电话：852-217 95122；86-755-86013388" in answer


def test_ask_structured_markers_do_not_match_inside_identifiers() -> None:
    hits = _ask_structural_evidence_hits(
        "Only use GraphIntell_table.xlsx and output RecordId, Balance, Status, Checksum.",
        "https://example.com info@example.com +1-202-555-0100",
    )

    assert "phone" not in hits
    assert "email" not in hits


def test_ask_auto_understanding_uses_agentic_classifier_for_table_lookup_route() -> None:
    class IntentClassifierService:
        def __init__(self):
            self.calls = []

        def ready(self):
            return {"ok": True, "provider": "test", "adapter": "classifier"}

        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            self.calls.append(
                {
                    "query": query,
                    "max_iterations": max_iterations,
                    "skills": skills,
                    "tool_policy": tool_policy,
                    "session_id": session_id,
                }
            )
            return {
                "answer": json.dumps(
                    {
                        "ask_intent": "kb_search",
                        "selected_intent": "quick",
                        "requires_retrieval": True,
                        "confidence": 0.91,
                        "reasons": ["exact table row lookup"],
                    },
                    ensure_ascii=False,
                ),
                "trace": {},
                "agentic_service": {"provider": "test", "adapter": "classifier"},
            }

    api = _api()
    service = IntentClassifierService()
    api.agentic_service = service
    query = (
        "只根据已上传的 GraphIntell_records.xlsx，在 Records 表中定位 RecordId 为 REC-002 且 RowNo 为 2 的唯一一行。"
        "请只输出 Balance、Limit、Status、Checksum 的精确值。"
    )

    payload = api.workspace_ask_understand({"query": query, "intent": "auto", "session_id": "ask-test"})
    understand = payload["understand"]

    assert understand["intent"] == "kb_search"
    assert understand["selected_intent"] == "quick"
    assert understand["intent_contract"]["interaction_intent"] == "evidence_qa"
    assert understand["intent_contract"]["task_intent"] == "kb_search"
    assert understand["intent_contract"]["requires_evidence"] is True
    assert understand["intent_contract"]["execution_depth"] == "quick"
    assert understand["routing_owner"] == "agentic_intent_classifier"
    assert understand["intent_classifier"]["status"] == "classified"
    assert service.calls[0]["max_iterations"] == 1
    assert service.calls[0]["skills"] == []
    assert service.calls[0]["tool_policy"] == {"mode": "none"}


def test_ask_auto_understanding_honors_agentic_classifier_deep_route() -> None:
    class IntentClassifierService:
        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            return {
                "answer": json.dumps(
                    {
                        "ask_intent": "graph_research",
                        "selected_intent": "deep",
                        "requires_retrieval": True,
                        "confidence": 0.88,
                        "reasons": ["multi-hop relationship analysis"],
                    },
                    ensure_ascii=False,
                ),
                "trace": {},
                "agentic_service": {"provider": "test", "adapter": "classifier"},
            }

        def ready(self):
            return {"ok": True}

    api = _api()
    api.agentic_service = IntentClassifierService()
    query = "Find the graph path and relationship between REC-002 and Supplier Alpha."

    payload = api.workspace_ask_understand({"query": query, "intent": "auto"})
    understand = payload["understand"]

    assert understand["intent"] == "graph_research"
    assert understand["selected_intent"] == "deep"
    assert understand["intent_contract"]["interaction_intent"] == "graph_research"
    assert understand["intent_contract"]["requires_evidence"] is True
    assert understand["intent_contract"]["execution_depth"] == "deep"
    assert understand["routing_owner"] == "agentic_intent_classifier"


def test_ask_explicit_deep_can_skip_intent_classifier_with_history() -> None:
    class UnexpectedClassifierService:
        def __init__(self):
            self.calls = []

        def ready(self):
            return {"ok": True}

        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            self.calls.append(query)
            raise AssertionError("intent classifier should not run for a forced deep route")

    api = _api()
    service = UnexpectedClassifierService()
    api.agentic_service = service

    payload = api.workspace_ask_understand(
        {
            "query": "继续展开上一个回答",
            "intent": "deep",
            "skip_intent_classifier": True,
            "scope": {"recent_messages": [{"role": "user", "content": "previous question"}]},
        }
    )
    understand = payload["understand"]

    assert service.calls == []
    assert understand["intent"] == "kb_search"
    assert understand["selected_intent"] == "deep"
    assert understand["routing_owner"] == "user_or_caller_override"
    assert understand["routing_mode"] == "forced"
    assert "intent_classifier" not in understand
    assert "intent_classifier_skipped" in understand["reasons"]
    assert understand["intent_contract"]["requires_evidence"] is True
    assert understand["intent_contract"]["depth_override"] == {"requested": "deep", "applied": True, "reason": "applied"}


def test_ask_force_deep_preserves_non_retrieval_greeting_intent() -> None:
    class UnexpectedClassifierService:
        def __init__(self):
            self.calls = []

        def ready(self):
            return {"ok": True}

        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            self.calls.append(query)
            raise AssertionError("intent classifier or deep route should not run for a greeting")

    api = _api()
    service = UnexpectedClassifierService()
    api.agentic_service = service

    response = api.workspace_ask({"query": "你好啊", "intent": "deep", "skip_intent_classifier": True})

    assert service.calls == []
    assert response["intent"] == "greeting"
    assert response["answer_type"] == "direct_greeting"
    assert response["route"]["retrieval_owner"] == "none"
    assert response["route"]["selected_intent"] == "quick"
    assert response["route"]["intent_contract"]["interaction_intent"] == "greeting"
    assert response["route"]["intent_contract"]["task_intent"] == "none"
    assert response["route"]["intent_contract"]["requires_evidence"] is False
    assert response["route"]["intent_contract"]["execution_depth"] == "none"
    assert response["route"]["intent_contract"]["depth_override"] == {
        "requested": "deep",
        "applied": False,
        "reason": "non_evidence_intent",
    }


def test_ask_auto_understanding_recognizes_writing_operation() -> None:
    api = _api()

    payload = api.workspace_ask_understand({"query": "请基于这份资料写一段不超过两句话的报告摘要。", "intent": "auto"})
    understand = payload["understand"]

    assert understand["intent"] == "writing"
    assert understand["intent_contract"]["interaction_intent"] == "writing"
    assert understand["intent_contract"]["answer_contract"] == "writing_context_answer"
    assert understand["intent_contract"]["requires_evidence"] is True


def test_ask_quick_answer_extracts_requested_fields_from_matching_table_row() -> None:
    answer = _ask_quick_answer(
        (
            "In uploaded operations.xlsx, locate the unique row where RecordId REC-002 and RowNo 2. "
            "Only output Balance, Limit, Status, Checksum. Do not cite RowNo 1 or 3."
        ),
        {
            "results": [
                {
                    "snippet": "RecordId REC-002 RowNo 2",
                    "source_window": {
                        "text": (
                            "| RowNo | RecordId | Balance | Limit | Status | Checksum |\n"
                            "| --- | --- | --- | --- | --- | --- |\n"
                            "| 1 | REC-001 | 10.00 | 20.00 | Current | CHK-REC-001-1000 |\n"
                            "| 2 | REC-002 | 25.50 | 40.00 | Review | CHK-REC-002-9911 |\n"
                            "| 3 | REC-003 | 30.00 | 50.00 | Current | CHK-REC-003-3000 |"
                        )
                    },
                }
            ],
            "diagnostics": {},
        },
    )

    assert answer.splitlines() == [
        "Balance = 25.50",
        "Limit = 40.00",
        "Status = Review",
        "Checksum = CHK-REC-002-9911",
    ]
    assert "联系电话" not in answer
    assert "REC-001" not in answer
    assert "REC-003" not in answer


def test_ask_quick_answer_keeps_more_requested_fact_values() -> None:
    answer = _ask_quick_answer(
        "Northstar latency、Owner、Status 和 budget ceiling 是什么？",
        {
            "results": [
                {
                    "snippet": (
                        "Intent Browser Fixture. Northstar latency is 42 ms. Owner is Ada Example. "
                        "Status is production-ready. The budget ceiling is 8800 USD."
                    )
                },
                {
                    "snippet": (
                        "Workbook: portfolio-pipeline.xlsx Sheet: Pipeline / Company / Lead / Status / ARR / "
                        "Next Step / Acme Example / Alice Example / active / 1200000 / Prepare partner meeting brief."
                    )
                },
            ]
        },
    )

    assert "42 ms" in answer
    assert "Ada Example" in answer
    assert "production-ready" in answer
    assert "8800 USD" in answer
    assert "portfolio-pipeline" not in answer
    assert "1200000" not in answer
    assert "当前资料支持以下结论" not in answer


def test_ask_quick_writing_fallback_composes_from_top_evidence_only() -> None:
    answer = _ask_quick_answer(
        "请基于这份资料写一段不超过两句话的报告摘要，保留数字。",
        {
            "results": [
                {
                    "snippet": (
                        "Intent Browser Fixture. Northstar latency is 42 ms. Owner is Ada Example. "
                        "Status is production-ready. The budget ceiling is 8800 USD."
                    )
                },
                {
                    "snippet": (
                        "Unrelated browser regression marker. Annual recurring revenue is 987654321. "
                        "This should not be pulled into the requested writing output."
                    )
                },
            ]
        },
        ask_intent="writing",
    )

    assert answer.startswith("报告摘要：")
    assert "42 ms" in answer
    assert "8800 USD" in answer
    assert "987654321" not in answer
    assert "当前资料支持以下结论" not in answer


def test_workspace_ask_quick_uses_agentic_final_synthesis_without_tools() -> None:
    class QuickSynthesisAgenticService:
        def __init__(self):
            self.calls = []

        def ready(self):
            return {"ok": True, "provider": "test", "adapter": "synthesis"}

        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            self.calls.append(
                {
                    "query": query,
                    "max_iterations": max_iterations,
                    "skills": skills,
                    "tool_policy": tool_policy,
                    "session_id": session_id,
                }
            )
            return {
                "answer": json.dumps(
                    {
                        "answer": "海康威视 2024 年营业收入为 92,495,525,118.30 元，约 924.96 亿元；该数值来自已校验证据中的营业收入（元）字段。"
                    },
                    ensure_ascii=False,
                ),
                "trace": {},
                "agentic_service": {"provider": "test", "adapter": "synthesis"},
            }

    api = _api()
    service = QuickSynthesisAgenticService()
    api.agentic_service = service
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "quick-synthesis-report",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "海康威视2024年报",
            "content": {
                "text": (
                    "海康威视2024年营业收入多少元。营业收入构成 4 单位：亿元 PBG 134.67。"
                    "主要会计数据和财务指标：营业收入（元） 92,495,525,118.30。"
                )
            },
        }
    )

    response = api.workspace_ask(
        {
            "query": "海康威视2024年营业收入多少元？",
            "intent": "quick",
            "session_id": "quick-synthesis-session",
        }
    )

    assert response["answer"].startswith("海康威视 2024 年营业收入为")
    assert "营业收入构成 4" not in response["answer"]
    assert response["route"]["final_synthesis_owner"] == "fastreact_agentic_service"
    assert response["trace"]["final_synthesis"]["status"] == "succeeded"
    assert service.calls[0]["max_iterations"] == 1
    assert service.calls[0]["skills"] == []
    assert service.calls[0]["tool_policy"] == {"mode": "none"}
    assert service.calls[0]["session_id"] == "quick-synthesis-session"


def test_workspace_ask_conversation_stream_marks_run_failed_when_closed() -> None:
    api = object.__new__(PSKAApi)
    api.store = InMemoryKnowledgeStore()
    api.store.add_user(User("user_primary", "primary", UserRole.ADMIN))

    def fake_event_stream(_payload, context=None):
        yield ("progress", {"progress": {"step_id": "search"}})

    api.workspace_ask_event_stream = fake_event_stream
    stream = api.workspace_ask_conversation_event_stream(
        "ask_close_test",
        {"query": "hello", "owner_user_id": "user_primary", "tenant_id": "tenant_default"},
    )
    conversation_event = next(stream)
    run_id = conversation_event[1]["run"]["run_id"]

    stream.close()

    runs = api.store.list_ask_runs("ask_close_test", tenant_id="tenant_default", owner_user_id="user_primary", limit=1)
    saved = api.workspace_ask_conversation("ask_close_test", {"owner_user_id": "user_primary", "tenant_id": "tenant_default"})
    assert runs[0].run_id == run_id
    assert runs[0].status == "failed"
    assert runs[0].result["error"] == "stream_closed_before_done"
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][1]["content"].startswith("Ask PSKA 运行未完成")


def test_workspace_ask_conversation_stream_persists_progress_before_close() -> None:
    api = object.__new__(PSKAApi)
    api.store = InMemoryKnowledgeStore()
    api.store.add_user(User("user_primary", "primary", UserRole.ADMIN))

    def fake_event_stream(_payload, context=None):
        yield ("progress", {"progress": {"step_id": "search", "status": "running"}})
        yield ("agent_step", {"step": {"step": "expand", "status": "running"}})

    api.workspace_ask_event_stream = fake_event_stream
    stream = api.workspace_ask_conversation_event_stream(
        "ask_progress_close_test",
        {"query": "hello", "owner_user_id": "user_primary", "tenant_id": "tenant_default"},
    )
    next(stream)
    assert next(stream)[0] == "progress"

    stream.close()

    runs = api.store.list_ask_runs("ask_progress_close_test", tenant_id="tenant_default", owner_user_id="user_primary", limit=1)
    assert runs[0].status == "failed"
    assert runs[0].result["progress"][0]["step_id"] == "search"
    assert runs[0].result["error"] == "stream_closed_before_done"


def test_workspace_ask_conversation_deep_omits_history_for_independent_question() -> None:
    class ConversationIntentService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            if str(query).startswith("You are PSKA's agentic Ask intent classifier"):
                return {
                    "answer": json.dumps(
                        {
                            "ask_intent": "kb_search",
                            "selected_intent": "quick",
                            "requires_retrieval": True,
                            "confidence": 0.86,
                            "reasons": ["independent scoped question"],
                        },
                        ensure_ascii=False,
                    ),
                    "trace": {},
                    "agentic_service": {"provider": "test", "adapter": "classifier"},
                }
            return super().search(
                query,
                user,
                represented_user_id=represented_user_id,
                max_iterations=max_iterations,
                skills=skills,
                tool_policy=tool_policy,
                session_id=session_id,
            )

    api = _api()
    api.agentic_service = ConversationIntentService(api.retrieval)
    conversation_id = api.create_workspace_ask_conversation({"title": "Evidence thread"})["conversation"]["conversation_id"]
    list(
        api.workspace_ask_conversation_event_stream(
            conversation_id,
            {"query": "What does reusablehistorykeyword say?", "intent": "quick"},
        )
    )

    api.agentic_service.calls.clear()
    list(
        api.workspace_ask_conversation_event_stream(
            conversation_id,
            {"query": "只根据已上传的 report.pdf，请输出最后一页的官网和电话。", "intent": "deep"},
        )
    )

    deep_query = api.agentic_service.calls[-1]["query"]
    assert "recent_messages" not in deep_query
    assert "conversation_summary" not in deep_query
    assert "reusablehistorykeyword" not in deep_query


def test_workspace_ask_conversation_deep_keeps_history_for_follow_up_question() -> None:
    class ConversationIntentService(FakeAgenticService):
        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None, session_id=None):
            if str(query).startswith("You are PSKA's agentic Ask intent classifier"):
                return {
                    "answer": json.dumps(
                        {
                            "ask_intent": "follow_up",
                            "selected_intent": "deep",
                            "requires_retrieval": True,
                            "confidence": 0.9,
                            "reasons": ["continues prior answer"],
                        },
                        ensure_ascii=False,
                    ),
                    "trace": {},
                    "agentic_service": {"provider": "test", "adapter": "classifier"},
                }
            return super().search(
                query,
                user,
                represented_user_id=represented_user_id,
                max_iterations=max_iterations,
                skills=skills,
                tool_policy=tool_policy,
                session_id=session_id,
            )

    api = _api()
    api.agentic_service = ConversationIntentService(api.retrieval)
    conversation_id = api.create_workspace_ask_conversation({"title": "Evidence thread"})["conversation"]["conversation_id"]
    list(
        api.workspace_ask_conversation_event_stream(
            conversation_id,
            {"query": "What does reusablehistorykeyword say?", "intent": "quick"},
        )
    )

    api.agentic_service.calls.clear()
    list(
        api.workspace_ask_conversation_event_stream(
            conversation_id,
            {"query": "继续展开上一个回答", "intent": "deep"},
        )
    )

    deep_query = api.agentic_service.calls[-1]["query"]
    assert "recent_messages" in deep_query
    assert "reusablehistorykeyword" in deep_query


def test_ask_query_terms_splits_mixed_english_chinese() -> None:
    terms = _ask_query_terms("acme example是一个什么样公司？")

    assert terms[:2] == ["acme", "example"]
    assert "example是一个什么样公司" not in terms
    deep_terms = _ask_query_terms("请深入分析 acme-example 的优势和风险，并给出可引用结论。")
    assert deep_terms[:3] == ["acme-example", "优势", "风险"]
    assert "请深入分析" not in deep_terms


def test_ask_auto_route_uses_agentic_selected_intent_instead_of_keyword_rules() -> None:
    query = "请深入调研 Northstar Robotics 是否应该进入 Q3 reserve-allocation shortlist。先判断需要查哪些证据，再给出可引用结论。"

    assert _ask_route_intent(query, intent="auto") == "quick"
    assert _ask_route_intent(query, intent="auto", agentic_selected_intent="deep") == "deep"


def test_agentic_event_response_prefers_final_source_ref_ids() -> None:
    response = normalize_agentic_event_response(
        [
            {
                "type": "session_end",
                "content": json.dumps(
                    {
                        "answer": "最终回答。",
                        "citations": ["Relevant Deep Note"],
                        "source_refs": ["src_relevant"],
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        provider="fastreact",
        adapter="fastreact",
        url="http://fastreact.test",
    )

    assert response["answer"] == "最终回答。"
    assert response["source_refs"] == [{"source_item_id": "src_relevant"}]
    assert response["citations"] == [{"title": "Relevant Deep Note"}]


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
    assert ready["missing_pska_tools"] == [
        "pska_digest_context",
        "pska_graph_context",
        "pska_index_status",
        "pska_job_context",
        "pska_read_evidence_context",
        "pska_write_candidates",
    ]


def test_fastreact_ready_accepts_namespaced_pska_tools(monkeypatch) -> None:
    namespaced_tools = [
        "pska_pska_search",
        "pska_pska_index_status",
        "pska_pska_read_evidence_context",
        "pska_pska_graph_context",
        "pska_pska_digest_context",
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
        "pska_read_evidence_context",
        "pska_graph_context",
        "pska_digest_context",
        "pska_job_context",
        "pska_search",
        "pska_write_candidates",
        "pska_pska_index_status",
        "pska_pska_read_evidence_context",
        "pska_pska_graph_context",
        "pska_pska_digest_context",
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
    pska_tools = policy["tool_rules"]

    assert config["service"]["port"] == 18741
    assert policy["default_action"] == "deny"
    assert pska_tools["exec"] == "deny"
    assert pska_tools["read_file"] == "deny"
    assert pska_tools["write_file"] == "deny"
    assert pska_tools["edit_file"] == "deny"
    assert pska_tools["pska_pska_search"] == "allow"
    assert pska_tools["pska_pska_read_evidence_context"] == "allow"
    assert pska_tools["pska_pska_ingest_channel_payload"] == "deny"
    assert policy["tool_profiles"]["pska_ask_read"]["tools"] == [
        "pska_pska_search",
        "pska_pska_index_status",
        "pska_pska_read_evidence_context",
        "pska_pska_graph_context",
        "pska_pska_digest_context",
    ]


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


def test_workspace_readiness_marks_fastreact_unready_when_pska_mcp_server_is_dead() -> None:
    class DeadMCPAgenticService:
        def ready(self):
            return {
                "ok": True,
                "provider": "fastreact",
                "adapter": "fastreact",
                "pska_tools_loaded": True,
                "missing_pska_tools": [],
                "ready": {
                    "mcp": {
                        "ready": True,
                        "servers": [
                            {
                                "name": "pska",
                                "alive": False,
                                "tools": ["pska_pska_search", "pska_pska_job_context"],
                            }
                        ],
                    }
                },
            }

    api = _api()
    api.agentic_service = DeadMCPAgenticService()

    readiness = api.workspace_readiness()

    assert readiness["summary"]["fastreact_ok"] is False
    assert readiness["summary"]["fastreact_pska_mcp_ok"] is False


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
    (workspace_root / "tenants" / "tenant_default" / "users" / "user_primary" / "sources" / "archives" / "twitter").mkdir(parents=True)
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


def test_console_dashboard_source_counts_are_tenant_scoped() -> None:
    api = _api()
    api.store.add_user(User("user_tenant_a", "tenant-a", UserRole.USER, tenant_id="tenant_a"))
    api.store.add_user(User("user_tenant_b", "tenant-b", UserRole.USER, tenant_id="tenant_b"))
    ingest = IngestService(api.store)

    ingest.ingest_channel_payload(
        {
            **_minimal_ingest_payload("tenant-a-source"),
            "tenant_id": "tenant_a",
            "owner_user_id": "user_tenant_a",
            "content": {"text": "Tenant A has one source."},
        }
    )
    for source_id in ("tenant-b-source-one", "tenant-b-source-two"):
        ingest.ingest_channel_payload(
            {
                **_minimal_ingest_payload(source_id),
                "tenant_id": "tenant_b",
                "owner_user_id": "user_tenant_b",
                "content": {"text": f"Tenant B source {source_id}."},
            }
        )

    tenant_a = api.console_dashboard(owner_user_id="user_tenant_a", tenant_id="tenant_a")
    tenant_b = api.console_dashboard(owner_user_id="user_tenant_b", tenant_id="tenant_b")

    assert tenant_a["source_counts"] == {"source_items": 1, "chunks": 1}
    assert tenant_b["source_counts"] == {"source_items": 2, "chunks": 2}


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
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_relationship_incomplete",
            owner_user_id="user_primary",
            review_type=ReviewType.RELATIONSHIP_CANDIDATE,
            title="Incomplete relationship",
            proposal={"confidence": 0.72, "source_refs": [{"source_item_id": "src_rel"}]},
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_relationship_missing_confidence",
            owner_user_id="user_primary",
            review_type=ReviewType.RELATIONSHIP_CANDIDATE,
            title="Relationship without confidence",
            proposal={
                "relation_type": "related_to",
                "source_refs": [{"source_item_id": "src_rel"}],
                "members": [
                    {"entity_type": "topic", "label": "Alpha"},
                    {"entity_type": "topic", "label": "Beta"},
                ],
            },
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_profile_approved",
            owner_user_id="user_primary",
            review_type=ReviewType.PROFILE_UPDATE,
            title="Approved profile candidate",
            proposal={
                "profile_delta": {"topic": "approved"},
                "confidence": 0.93,
                "source_refs": [{"source_item_id": "src_approved", "chunk_id": "chk_approved"}],
            },
            status="approved",
        )
    )

    with _http_server(api) as base_url:
        page_status, _headers, body = _http_text(base_url, "GET", "/console/reviews")
        data_status, payload = _http_json(base_url, "GET", "/console/reviews/data?status=pending&owner_user_id=user_primary")

    assert page_status == 200
    assert "/console/reviews.js" in body
    assert data_status == 200
    by_id = {item["review_item_id"]: item for item in payload["review_items"]}
    assert payload["total_matching"] == 5
    assert payload["analytics"]["total"] == 6
    assert payload["analytics"]["status_counts"]["pending"] == 5
    assert payload["analytics"]["status_counts"]["approved"] == 1
    assert payload["analytics"]["review_type_counts"]["profile_update"] == 3
    assert payload["analytics"]["source_ref_status_counts"] == {"missing": 2, "present": 4}
    assert payload["analytics"]["apply_ready_count"] == 2
    assert payload["analytics"]["by_review_type"]["profile_update"]["status_counts"]["approved"] == 1
    assert by_id["rev_profile_ready"]["review_type"] == "profile_update"
    assert by_id["rev_profile_ready"]["confidence"] == 0.82
    assert by_id["rev_profile_ready"]["source_ref_status"] == "present"
    assert by_id["rev_profile_ready"]["apply_supported"] is True
    assert by_id["rev_profile_ready"]["apply_ready"] is True
    assert "approve_apply" in by_id["rev_profile_ready"]["recommended_actions"]
    assert by_id["rev_profile_ready"]["remediation"]["status"] == "ready"
    assert by_id["rev_profile_ready"]["remediation"]["actions"][1]["action_id"] == "approve_apply"
    assert by_id["rev_profile_missing_source"]["source_ref_status"] == "missing"
    assert by_id["rev_profile_missing_source"]["apply_supported"] is True
    assert by_id["rev_profile_missing_source"]["apply_ready"] is False
    assert "approve_apply" not in by_id["rev_profile_missing_source"]["recommended_actions"]
    assert by_id["rev_profile_missing_source"]["remediation"]["status"] == "blocked"
    assert by_id["rev_profile_missing_source"]["remediation"]["blockers"][0]["blocker_id"] == "missing_source_refs"
    assert by_id["rev_conflict"]["apply_supported"] is False
    assert by_id["rev_conflict"]["apply_ready"] is False
    assert by_id["rev_conflict"]["remediation"]["blockers"][0]["blocker_id"] == "auto_apply_unsupported"
    assert by_id["rev_relationship_incomplete"]["apply_supported"] is True
    assert by_id["rev_relationship_incomplete"]["apply_ready"] is False
    assert "approve_apply" not in by_id["rev_relationship_incomplete"]["recommended_actions"]
    assert {blocker["blocker_id"] for blocker in by_id["rev_relationship_incomplete"]["remediation"]["blockers"]} >= {"missing_relation_type", "missing_relationship_members"}
    assert by_id["rev_relationship_missing_confidence"]["apply_supported"] is True
    assert by_id["rev_relationship_missing_confidence"]["apply_ready"] is False
    assert "approve_apply" not in by_id["rev_relationship_missing_confidence"]["recommended_actions"]
    assert by_id["rev_relationship_missing_confidence"]["remediation"]["blockers"][0]["blocker_id"] == "invalid_confidence"


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


def test_writing_workspace_is_tenant_scoped_and_composes_selected_answers() -> None:
    api = _api()
    tenant_a = context_from_headers({"X-PSKA-Tenant-Id": "tenant_a", "X-PSKA-User-Id": "user_primary"}, {})
    tenant_b = context_from_headers({"X-PSKA-Tenant-Id": "tenant_b", "X-PSKA-User-Id": "user_primary"}, {})

    board_a = api.workspace_writing_create_board({"title": "Reserve memo", "goal": "Decide the memo structure"}, context=tenant_a)["board"]
    board_b = api.workspace_writing_create_board({"title": "Reserve memo", "goal": "Different tenant"}, context=tenant_b)["board"]
    spoofed = api.workspace_writing_create_board(
        {
            "title": "Spoofed tenant",
            "tenant_id": "tenant_b",
            "owner_user_id": "other_user",
            "represented_user_id": "other_user",
        },
        context=tenant_a,
    )["board"]
    section = api.workspace_writing_create_node(
        board_a["board_id"],
        {"node_type": "section", "title": "Recommendation", "position": {"x": 120, "y": 120}},
        context=tenant_a,
    )["node"]
    answer = api.workspace_writing_create_node(
        board_a["board_id"],
        {
            "node_type": "answer",
            "title": "Evidence-backed answer",
            "body_markdown": "The selected evidence supports a cautious recommendation.",
            "citations": [{"title": "Source A", "source_item_id": "src_a"}],
            "source_refs": [{"source_item_id": "src_a"}],
            "position": {"x": 420, "y": 120},
        },
        context=tenant_a,
    )["node"]
    api.workspace_writing_create_edge(
        board_a["board_id"],
        {
            "source_node_id": answer["node_id"],
            "target_node_id": section["node_id"],
            "edge_type": "included_in",
            "label": "纳入章节",
        },
        context=tenant_a,
    )

    list_a = api.workspace_writing_boards(context=tenant_a)["boards"]
    list_b = api.workspace_writing_boards(context=tenant_b)["boards"]
    composed = api.workspace_writing_compose(
        board_a["board_id"],
        {
            "section_node_id": section["node_id"],
            "answer_node_ids": [answer["node_id"]],
            "tenant_id": "tenant_b",
            "owner_user_id": "other_user",
        },
        context=tenant_a,
    )

    assert spoofed["tenant_id"] == "tenant_a"
    assert spoofed["owner_user_id"] == "user_primary"
    assert {board["board_id"] for board in list_a} == {board_a["board_id"], spoofed["board_id"]}
    assert [board["board_id"] for board in list_b] == [board_b["board_id"]]
    assert composed["retrieval_used"] is False
    assert "Evidence-backed answer" in composed["draft_markdown"]
    assert "src_a" in composed["draft_markdown"]
    with pytest.raises(KeyError):
        api.workspace_writing_board(board_a["board_id"], context=tenant_b)
    with pytest.raises(KeyError):
        api.workspace_writing_board(spoofed["board_id"], context=tenant_b)
    deleted = api.workspace_writing_delete_board(board_b["board_id"], context=tenant_b)
    assert deleted["deleted"]["board_id"] == board_b["board_id"]
    assert api.workspace_writing_boards(context=tenant_b)["boards"] == []


def test_writing_suggest_questions_does_not_persist_nodes() -> None:
    api = _api()
    context = context_from_headers({"X-PSKA-Tenant-Id": "tenant_a", "X-PSKA-User-Id": "user_primary"}, {})
    board = api.workspace_writing_create_board({"title": "Inquiry", "goal": "Write a supported memo"}, context=context)["board"]
    question = api.workspace_writing_create_node(
        board["board_id"],
        {"node_type": "question", "title": "What should this memo prove?", "body_markdown": "Clarify the central claim."},
        context=context,
    )["node"]

    before = api.workspace_writing_board(board["board_id"], context=context)
    suggestions = api.workspace_writing_suggest_questions(
        board["board_id"],
        {"node_id": question["node_id"], "direction": "evidence_gap"},
        context=context,
    )
    after = api.workspace_writing_board(board["board_id"], context=context)

    assert suggestions["persisted"] is False
    assert suggestions["suggestions"]
    assert len(after["nodes"]) == len(before["nodes"])


def test_evidence_brief_creates_writing_draft_with_lineage_and_refs() -> None:
    api = _api()
    context = context_from_headers({"X-PSKA-Tenant-Id": "tenant_a", "X-PSKA-User-Id": "user_primary"}, {})
    source = api.ingest.ingest_channel_payload(
        {
            **_minimal_ingest_payload("brief-source"),
            "tenant_id": "tenant_a",
            "content": {"text": "Evidence brief drafts must keep citations and review lineage."},
        }
    )
    knowledge_base = api.create_workspace_knowledge_base({"name": "Brief Corpus"}, context=context)["knowledge_base"]
    api.workspace_documents_link(
        {
            "source_item_ids": [source.source_item_id],
            "target_knowledge_base_id": knowledge_base["knowledge_base_id"],
            "execute": True,
        },
        context=context,
    )
    ref = SourceRef(source_item_id=source.source_item_id)
    api.store.add_digest_note(
        DigestNote(
            digest_note_id="dig_brief",
            owner_user_id="user_primary",
            title="Briefable digest",
            synopsis="Digest synopsis for a future Evidence Wiki page.",
            source_refs=[ref],
            key_points=[{"text": "Digest point keeps provenance."}],
            job_id="job_brief",
            tenant_id="tenant_a",
        )
    )
    api.store.add_knowledge_claim(
        KnowledgeClaim(
            knowledge_claim_id="kc_brief",
            owner_user_id="user_primary",
            claim_type="observation",
            statement="Evidence briefs preserve citations.",
            source_refs=[ref],
            evidence_text="Evidence brief drafts must keep citations and review lineage.",
            confidence=0.82,
            job_id="job_brief",
            tenant_id="tenant_a",
        )
    )
    api.store.add_review_item(
        ReviewItem(
            review_item_id="rev_brief",
            owner_user_id="user_primary",
            review_type=ReviewType.LOW_CONFIDENCE,
            title="Review brief candidate",
            proposal={"job_id": "job_brief", "source_refs": [{"source_item_id": source.source_item_id}], "plain_text_summary": "Review before publish."},
            tenant_id="tenant_a",
        )
    )

    payload = api.workspace_evidence_brief_create({"job_id": "job_brief"}, context=context)
    board = payload["board"]
    nodes = payload["nodes"]
    draft = next(node for node in nodes if node["node_type"] == "draft")

    assert payload["ok"] is True
    assert board["metadata"]["kind"] == "evidence_wiki_brief"
    assert board["metadata"]["publish_status"] == "draft"
    assert payload["brief"]["publish_status"] == "draft"
    assert payload["brief"]["review_status"] == "needs_review"
    assert payload["brief"]["lineage"]["digest_note_ids"] == ["dig_brief"]
    assert payload["brief"]["lineage"]["knowledge_claim_ids"] == ["kc_brief"]
    assert payload["brief"]["lineage"]["review_item_ids"] == ["rev_brief"]
    assert payload["brief"]["source_refs"][0]["source_item_id"] == source.source_item_id
    assert payload["brief"]["source_refs"][0]["knowledge_base_ids"] == [knowledge_base["knowledge_base_id"]]
    assert payload["brief"]["source_refs"][0]["knowledge_base_names"] == ["Brief Corpus"]
    assert payload["brief"]["lineage"]["knowledge_base_ids"] == [knowledge_base["knowledge_base_id"]]
    assert payload["brief"]["knowledge_base_names"] == ["Brief Corpus"]
    assert board["metadata"]["knowledge_base_ids"] == [knowledge_base["knowledge_base_id"]]
    assert board["metadata"]["knowledge_base_scope"]["source_item_count"] == 1
    assert draft["source_refs"][0]["source_item_id"] == source.source_item_id
    assert draft["source_refs"][0]["knowledge_base_name"] == "Brief Corpus"
    assert draft["metadata"]["lineage"]["knowledge_base_ids"] == [knowledge_base["knowledge_base_id"]]
    assert "Evidence briefs preserve citations." in draft["body_markdown"]
    assert "Knowledge bases: Brief Corpus" in draft["body_markdown"]
    evidence_node = next(node for node in nodes if node["metadata"].get("artifact_type") == "digest_note")
    assert evidence_node["source_refs"][0]["knowledge_base_name"] == "Brief Corpus"
    persisted = api.workspace_writing_board(board["board_id"], context=context)
    assert persisted["board"]["metadata"]["lineage"]["job_id"] == "job_brief"
    assert persisted["board"]["metadata"]["knowledge_base_names"] == ["Brief Corpus"]
    draft_search = api.workspace_evidence_wiki_search({"query": "Evidence brief drafts must keep citations"}, context=context)
    assert draft_search["results"] == []
    draft_page = api.workspace_evidence_wiki_page(board["board_id"], context=context)
    assert draft_page["ok"] is False
    assert draft_page["reason"] == "not_published"

    blocked_publish = api.workspace_evidence_wiki_publish({"board_id": board["board_id"], "publish_status": "published"}, context=context)
    assert blocked_publish["ok"] is False
    assert blocked_publish["reason"] == "review_gate"
    assert blocked_publish["review_gate"]["blocking_review_items"] == [{"review_item_id": "rev_brief", "status": "pending"}]
    assert api.workspace_evidence_wiki_search({"query": "Evidence brief drafts must keep citations"}, context=context)["results"] == []

    ReviewService(api.store).approve("rev_brief", actor_user_id="user_primary")
    published = api.workspace_evidence_wiki_publish({"board_id": board["board_id"], "publish_status": "published"}, context=context)
    assert published["ok"] is True
    assert published["publish_status"] == "published"
    assert published["review_gate"]["review_items"] == [{"review_item_id": "rev_brief", "status": "approved"}]
    related_board = api.workspace_writing_create_board(
        {
            "title": "Related Brief: citation practice",
            "goal": "Related page for cross-page Evidence Wiki organization.",
            "knowledge_base_ids": [knowledge_base["knowledge_base_id"]],
            "metadata": {
                "kind": "evidence_wiki_brief",
                "status": "published",
                "publish_status": "published",
                "published_at": "2026-01-01T00:00:00+00:00",
                "wiki_taxonomy": {"tags": ["citation"], "categories": ["governance"]},
                "source_refs": [{"source_item_id": source.source_item_id, "knowledge_base_ids": [knowledge_base["knowledge_base_id"]]}],
                "lineage": {"source_refs": [{"source_item_id": source.source_item_id}], "knowledge_base_ids": [knowledge_base["knowledge_base_id"]]},
            },
        },
        context=context,
    )["board"]
    taxonomy_update = api.workspace_evidence_wiki_update_taxonomy(
        board["board_id"],
        {
            "taxonomy": {
                "tags": ["citation", "source grounded"],
                "categories": ["governance"],
                "topics": ["Evidence Wiki"],
            }
        },
        context=context,
    )
    assert taxonomy_update["ok"] is True
    assert taxonomy_update["taxonomy"]["tags"] == ["citation", "source grounded"]
    assert taxonomy_update["page"]["taxonomy"]["categories"] == ["governance"]
    content_update = api.workspace_evidence_wiki_update_content(
        board["board_id"],
        {
            "body_markdown": "Edited Evidence Wiki page copy keeps Evidence brief drafts grounded in citations.",
            "summary": "Edited citation governance summary.",
        },
        context=context,
    )
    assert content_update["ok"] is True
    assert content_update["content_node"]["metadata"]["artifact_type"] == "evidence_wiki_page_body"
    assert content_update["content_node"]["source_refs"][0]["source_item_id"] == source.source_item_id
    assert content_update["page"]["summary"] == "Edited citation governance summary."
    assert "Edited Evidence Wiki page copy" in content_update["page"]["body_markdown"]
    assert content_update["page"]["content_revisions"][0]["revision"] == 1
    assert content_update["page"]["content_review"]["status"] == "needs_review"
    assert content_update["page"]["content_review"]["needs_review"] is True
    assert content_update["page"]["content_review"]["current_revision"] == 1
    assert content_update["page"]["content_review"]["published_revision"] == 0
    second_content_update = api.workspace_evidence_wiki_update_content(
        board["board_id"],
        {
            "body_markdown": "Second Evidence Wiki page copy before restore.",
            "summary": "Second citation governance summary.",
        },
        context=context,
    )
    assert second_content_update["ok"] is True
    assert second_content_update["page"]["wiki_content_revision"] == 2
    assert second_content_update["page"]["content_review"]["status"] == "needs_review"
    assert second_content_update["page"]["content_review"]["current_revision"] == 2
    assert "Second Evidence Wiki page copy" in second_content_update["page"]["body_markdown"]
    restored_content = api.workspace_evidence_wiki_restore_content(
        board["board_id"],
        {"revision": 1},
        context=context,
    )
    assert restored_content["ok"] is True
    assert restored_content["page"]["wiki_content_revision"] == 3
    assert restored_content["page"]["content_revision_count"] == 3
    assert restored_content["page"]["content_review"]["status"] == "needs_review"
    assert restored_content["page"]["content_review"]["current_revision"] == 3
    assert restored_content["page"]["content_revisions"][0]["restored_from_revision_id"] == content_update["page"]["content_revisions"][0]["revision_id"]
    assert "Edited Evidence Wiki page copy" in restored_content["page"]["body_markdown"]
    search = api.workspace_evidence_wiki_search(
        {
            "query": "Edited Evidence Wiki page copy",
            "knowledge_base_ids": [knowledge_base["knowledge_base_id"]],
        },
        context=context,
    )
    assert search["count"] == 1
    assert search["scope_applied"]["knowledge_base_ids"] == [knowledge_base["knowledge_base_id"]]
    assert search["results"][0]["board"]["board_id"] == board["board_id"]
    assert "Edited Evidence Wiki page copy" in search["results"][0]["snippet"]
    assert search["results"][0]["source_refs"][0]["source_item_id"] == source.source_item_id
    assert search["results"][0]["access"] == {"visibility": "owner", "tenant_id": "tenant_a", "owner_user_id": "user_primary"}
    assert search["results"][0]["taxonomy"]["tags"] == ["citation", "source grounded"]
    assert search["results"][0]["content_review"]["status"] == "needs_review"
    republished = api.workspace_evidence_wiki_publish({"board_id": board["board_id"], "publish_status": "published"}, context=context)
    assert republished["ok"] is True
    assert republished["board"]["metadata"]["wiki_published_content_revision"] == 3
    assert republished["board"]["metadata"]["wiki_content_review_status"] == "published"
    tagged_search = api.workspace_evidence_wiki_search(
        {
            "query": "",
            "knowledge_base_ids": [knowledge_base["knowledge_base_id"]],
            "tags": ["source grounded"],
        },
        context=context,
    )
    assert tagged_search["count"] == 1
    assert tagged_search["results"][0]["board"]["board_id"] == board["board_id"]
    assert tagged_search["results"][0]["content_review"]["status"] == "published"
    assert tagged_search["results"][0]["content_review"]["needs_review"] is False
    assert tagged_search["taxonomy_filters"] == {"tags": ["source grounded"]}
    assert tagged_search["taxonomy_facets"]["categories"][0] == {"value": "governance", "count": 1}
    assert api.workspace_evidence_wiki_search({"query": "", "tags": ["unrelated"]}, context=context)["results"] == []
    published_list = api.workspace_evidence_wiki_search({"query": "", "knowledge_base_ids": [knowledge_base["knowledge_base_id"]]}, context=context)
    assert published_list["count"] == 2
    assert {result["board"]["board_id"] for result in published_list["results"]} == {board["board_id"], related_board["board_id"]}
    page = api.workspace_evidence_wiki_page(board["board_id"], context=context)
    assert page["ok"] is True
    assert page["page"]["board_id"] == board["board_id"]
    assert page["page"]["publish_status"] == "published"
    assert page["page"]["content_review"]["status"] == "published"
    assert page["page"]["content_review"]["current_revision"] == 3
    assert page["page"]["content_review"]["published_revision"] == 3
    assert "Edited Evidence Wiki page copy" in page["page"]["body_markdown"]
    assert page["page"]["content_node_id"] == content_update["content_node"]["node_id"]
    assert page["page"]["source_refs"][0]["source_item_id"] == source.source_item_id
    assert page["page"]["lineage"]["review_item_ids"] == ["rev_brief"]
    assert page["page"]["access"] == {"visibility": "owner", "tenant_id": "tenant_a", "owner_user_id": "user_primary"}
    assert page["page"]["taxonomy"]["topics"] == ["Evidence Wiki"]
    assert page["page"]["related_pages"][0]["board"]["board_id"] == related_board["board_id"]
    assert page["page"]["related_pages"][0]["shared_source_item_ids"] == [source.source_item_id]
    assert page["page"]["related_pages"][0]["shared_taxonomy"]["tags"] == ["citation"]
    assert "共享 1 个来源" in page["page"]["related_pages"][0]["reason"]


def test_writing_ask_scope_uses_connected_node_context_in_quick_trace() -> None:
    api = _api()
    context = context_from_headers({"X-PSKA-Tenant-Id": "tenant_default", "X-PSKA-User-Id": "user_primary"}, {})
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "connected-context-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "tenant_id": "tenant_default",
            "title": "Connected Context Note",
            "content": {"text": "RareContextNeedle appears only in the connected writing node context."},
        }
    )

    response = api.workspace_ask(
        {
            "query": "What evidence exists for this follow-up?",
            "intent": "quick",
            "scope": {
                "board_id": "board_test",
                "node_id": "question_test",
                "session_id": "writing:board_test:question_test",
                "context_nodes": [
                    {
                        "node_id": "parent_answer",
                        "node_type": "answer",
                        "title": "Parent answer",
                        "body_markdown": "The follow-up is about RareContextNeedle.",
                    }
                ],
            },
        },
        context=context,
    )

    assert response["route"]["scope_context_nodes"] == 1
    assert "RareContextNeedle" in response["trace"]["retrieval_query"]
    assert response["trace"]["scope"]["context_node_count"] == 1


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


def test_workspace_ask_quick_returns_report_ready_answer_and_evidence() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-quick-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Ask Quick Note",
            "content": {"text": "Acme Example status is active and the owner is Alice Example."},
        }
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "Acme Example 的状态和负责人是什么？",
                "intent": "quick",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["route"]["retrieval_owner"] == "pska"
    assert payload["route"]["tool_policy"] == {"mode": "none"}
    assert payload["route"]["routing_owner"] == "pska_planner"
    assert "acme" in [term.lower() for term in payload["route"]["query_terms"]]
    assert [step["phase"] for step in payload["agent_steps"]] == ["understand", "route", "search", "read", "answer"]
    assert payload["agent_steps"][2]["title"] == "检索知识库与图谱"
    assert payload["answer"].startswith("关键结论")
    assert "status is active" in payload["answer"]
    assert "Ask Quick Note" not in payload["answer"]
    assert "---" not in payload["answer"]
    assert payload["citations"][0]["title"] == "Ask Quick Note"
    assert payload["evidence"]["source_refs"] == payload["citations"]
    assert payload["timing"]["time_to_first_answer_ms"] >= 0
    assert payload["quality_signals"]["schema"] == "pska.ask_quality_signals.v1"
    assert payload["quality_signals"]["quality_band"] == "grounded"
    assert payload["quality_signals"]["report_readiness"] == "ready_with_citations"
    assert payload["quality_signals"]["citation_count"] >= 1
    assert payload["quality_signals"]["evidence_result_count"] >= 1
    assert payload["quality_signals"]["retrieval_owner"] == "pska"
    assert payload["quality_signals"]["time_to_first_agent_event_ms"] >= 0


def test_workspace_ask_quick_explains_no_visible_evidence() -> None:
    api = _api()

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "What does the empty workspace know?",
                "intent": "quick",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    diagnostics = payload["quality_signals"]["no_answer_diagnostics"]
    by_dimension = {item["dimension"]: item for item in diagnostics["dimensions"]}
    assert status == 200
    assert payload["quality_signals"]["quality_band"] == "no_answerable_evidence"
    assert diagnostics["primary_reason"] in {"no_visible_evidence", "no_relevant_chunks", "not_enough_signal"}
    assert by_dimension["evidence"]["status"] == "no_visible_evidence"
    assert by_dimension["retrieval"]["status"] == "no_relevant_chunks"
    assert by_dimension["permissions"]["status"] == "possibly_filtered_or_unindexed"


def test_workspace_ask_deep_reports_mcp_tool_errors_in_no_answer_diagnostics() -> None:
    class MCPErrorAgenticService:
        def ready(self):
            return {"ok": True, "provider": "test", "adapter": "fake"}

        def search_event_stream(
            self,
            query,
            user,
            *,
            represented_user_id=None,
            max_iterations=3,
            skills=None,
            tool_policy=None,
            session_id=None,
        ):
            yield {"type": "session_start", "content": query, "session_id": session_id or "mcp-error", "event_id": "mcp:0"}
            yield {
                "type": "tool_call",
                "tool_name": "pska_pska_search",
                "tool_args": {"query": query, "top_k": 5},
                "tool_call_id": "call-mcp",
                "session_id": session_id or "mcp-error",
                "event_id": "mcp:1",
            }
            yield {
                "type": "tool_result",
                "tool_name": "pska_pska_search",
                "content": "[MCP_ERROR] ConnectionResetError: Connection lost",
                "tool_call_id": "call-mcp",
                "session_id": session_id or "mcp-error",
                "event_id": "mcp:2",
            }
            yield {
                "type": "session_end",
                "content": json.dumps({"answer": "知识检索服务当前不可用。", "source_refs": []}, ensure_ascii=False),
                "session_id": session_id or "mcp-error",
                "event_id": "mcp:3",
            }

    api = _api()
    api.agentic_service = MCPErrorAgenticService()

    payload = api.workspace_ask(
        {
            "query": "What does the deep path know?",
            "intent": "deep",
            "user_id": "user_primary",
            "represented_user_id": "user_primary",
        }
    )

    diagnostics = payload["quality_signals"]["no_answer_diagnostics"]
    by_dimension = {item["dimension"]: item for item in diagnostics["dimensions"]}
    tool_result = next(event for event in payload["trace"]["events"] if event["type"] == "tool_result")
    assert tool_result["result_summary"]["error_count"] == 1
    assert by_dimension["fastreact"]["status"] == "tool_channel_error"
    assert by_dimension["mcp"]["status"] == "tool_error"
    assert "ConnectionResetError" in by_dimension["mcp"]["detail"]


def test_workspace_ask_quick_marks_raw_dump_answers_as_needing_review() -> None:
    api = _api()
    payload = {
        "ok": True,
        "query": "raw",
        "answer": "---\ntitle: Raw Note\n---\n# Raw Note\n| a | b |\n| --- | --- |\n| 1 | 2 |",
        "route": {"selected_intent": "quick", "retrieval_owner": "pska", "surface": "today"},
        "evidence": {"citations": [{"source_item_id": "src_1", "title": "Raw Note"}], "results": [{"title": "Raw Note"}]},
        "citations": [{"source_item_id": "src_1", "title": "Raw Note"}],
        "source_refs": [{"source_item_id": "src_1", "title": "Raw Note"}],
        "trace": {},
        "timing": {"total_ms": 1, "time_to_first_answer_ms": 1},
    }

    enriched = __import__("pska_core.api", fromlist=["_ask_with_quality_signals"])._ask_with_quality_signals(payload)

    assert "raw_evidence_dump" in enriched["quality_signals"]["flags"]
    assert "answer_needs_rewrite" in enriched["quality_signals"]["flags"]
    assert enriched["quality_signals"]["quality_band"] == "needs_review"
    assert enriched["quality_signals"]["report_readiness"] == "needs_human_review"


def test_workspace_ask_deep_uses_fastreact_readonly_tool_policy() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-deep-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Ask Deep Note",
            "content": {"text": "Atlas reporting needs a risk summary grounded in PSKA evidence."},
        }
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "请分析 Atlas reporting 的风险并给出报告可用结论。",
                "intent": "deep",
                "surface": "today",
                "session_id": "ask-session-1",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert payload["ok"] is True
    assert payload["route"]["retrieval_owner"] == "fastreact_pska_mcp"
    assert payload["route"]["tool_policy"] == {
        "mode": "allowlist",
        "allowed_tools": [
            "pska_pska_search",
            "pska_pska_index_status",
            "pska_pska_read_evidence_context",
            "pska_pska_graph_context",
            "pska_pska_digest_context",
        ],
    }
    assert payload["route"]["tool_profile"] == "ask_read"
    assert payload["answer"] == "Fake external agentic answer."
    assert [step["phase"] for step in payload["agent_steps"][:4]] == ["understand", "think", "search", "read"]
    assert payload["timing"]["time_to_first_agent_event_ms"] >= 0
    call = api.agentic_service.calls[0]
    assert call["skills"] == ["pska_answer_with_citations"]
    assert call["tool_policy"] == payload["route"]["tool_policy"]
    assert call["session_id"] == "ask-session-1"
    assert "User question: 请分析 Atlas reporting" in call["query"]


def test_workspace_ask_deep_passes_knowledge_base_scope_to_mcp_policy() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-deep-alpha",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Deep Alpha",
            "content": {"text": "deep scopedsharedtoken alpha-only evidence"},
        }
    )
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-deep-beta",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Deep Beta",
            "content": {"text": "deep scopedsharedtoken beta-only evidence"},
        }
    )
    alpha_source_id = next(item.source_item_id for item in api.store.list_source_items() if item.source_id == "workspace-ask-deep-alpha")
    beta_source_id = next(item.source_item_id for item in api.store.list_source_items() if item.source_id == "workspace-ask-deep-beta")
    alpha_kb = KnowledgeBase("kb_deep_alpha", "user_primary", "Deep Alpha KB")
    beta_kb = KnowledgeBase("kb_deep_beta", "user_primary", "Deep Beta KB")
    api.store.upsert_knowledge_base(alpha_kb)
    api.store.upsert_knowledge_base(beta_kb)
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=alpha_kb.knowledge_base_id,
            source_item_id=alpha_source_id,
            owner_user_id="user_primary",
            added_by_user_id="user_primary",
        )
    )
    api.store.add_knowledge_base_source_item(
        KnowledgeBaseSourceItem(
            knowledge_base_id=beta_kb.knowledge_base_id,
            source_item_id=beta_source_id,
            owner_user_id="user_primary",
            added_by_user_id="user_primary",
        )
    )

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "请深入分析 scopedsharedtoken 的证据。",
                "intent": "deep",
                "surface": "today",
                "session_id": "ask-session-scope",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
                "scope": {"mode": "hard", "knowledge_base_ids": [alpha_kb.knowledge_base_id]},
            },
        )

    call = api.agentic_service.calls[0]
    policy_scope = call["tool_policy"]["scope"]
    evidence = payload["evidence"]
    source_ids = {
        item.get("source_item_id")
        for bucket in ("citations", "source_refs", "results", "source_windows")
        for item in evidence.get(bucket, [])
        if isinstance(item, dict) and item.get("source_item_id")
    }

    assert status == 200
    assert payload["route"]["scope_applied"]["knowledge_base_ids"] == [alpha_kb.knowledge_base_id]
    assert payload["route"]["tool_policy"]["scope"]["knowledge_base_ids"] == [alpha_kb.knowledge_base_id]
    assert policy_scope["knowledge_base_ids"] == [alpha_kb.knowledge_base_id]
    assert policy_scope["source_item_ids"] == [alpha_source_id]
    assert policy_scope["scope_mode"] == "hard"
    assert beta_source_id not in source_ids
    assert source_ids <= {alpha_source_id}


def test_ask_public_trace_event_preserves_scope_audit_fields() -> None:
    event = {
        "schema": "fastreact.event.v1",
        "type": "tool_call",
        "event_id": "run_1:3",
        "run_id": "run_1",
        "sequence": 3,
        "tool_name": "pska_pska_search",
        "tool_args": {
            "query": "scopedsharedtoken",
            "knowledge_base_ids": ["kb_alpha"],
            "scope_mode": "hard",
            "source_item_ids": ["src_alpha"],
            "scope": {
                "mode": "hard",
                "scope_mode": "hard",
                "knowledge_base_ids": ["kb_alpha"],
                "source_item_ids": ["src_alpha"],
                "internal_note": "not public",
            },
            "unrelated": "not public",
        },
        "metadata": {
            "action": "calling",
            "tool_policy_scope_applied": True,
            "tool_policy": {
                "mode": "allowlist",
                "allowed_tools": ["pska_pska_search"],
                "scope": {
                    "mode": "hard",
                    "scope_mode": "hard",
                    "knowledge_base_ids": ["kb_alpha"],
                    "source_item_ids": ["src_alpha"],
                    "internal_note": "not public",
                },
            },
            "raw_args": {"not": "public"},
        },
    }

    public = _ask_public_trace_event(event)

    assert public["tool_args"] == {
        "query": "scopedsharedtoken",
        "knowledge_base_ids": ["kb_alpha"],
        "scope_mode": "hard",
        "source_item_ids": ["src_alpha"],
        "scope": {
            "mode": "hard",
            "scope_mode": "hard",
            "knowledge_base_ids": ["kb_alpha"],
            "source_item_ids": ["src_alpha"],
        },
    }
    assert public["metadata"]["tool_policy_scope_applied"] is True
    assert public["metadata"]["tool_policy"] == {
        "mode": "allowlist",
        "allowed_tools": ["pska_pska_search"],
        "scope": {
            "mode": "hard",
            "scope_mode": "hard",
            "knowledge_base_ids": ["kb_alpha"],
            "source_item_ids": ["src_alpha"],
        },
    }


def test_workspace_ask_http_inaccessible_knowledge_base_scope_does_not_leak_id() -> None:
    api = _api()
    secret_kb_id = "kb_secret_should_not_leak"

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "sharedtoken",
                "intent": "quick",
                "scope": {"knowledge_base_ids": [secret_kb_id]},
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 403
    assert payload["error"] == "knowledge base is not accessible"
    assert secret_kb_id not in json.dumps(payload)


def test_workspace_knowledge_base_http_inaccessible_id_does_not_leak_id() -> None:
    api = _api()
    secret_kb_id = "kb_secret_direct_should_not_leak"

    with _http_server(api) as base_url:
        responses = [
            _http_json(base_url, "GET", f"/workspace/knowledge-bases/{secret_kb_id}"),
            _http_json(base_url, "PATCH", f"/workspace/knowledge-bases/{secret_kb_id}", {"status": "archived"}),
            _http_json(base_url, "DELETE", f"/workspace/knowledge-bases/{secret_kb_id}", {}),
        ]

    for status, payload in responses:
        assert status == 403
        assert payload["error"] == "knowledge base is not accessible"
        assert secret_kb_id not in json.dumps(payload)


def test_workspace_ask_deep_drops_source_refs_outside_current_owner_scope() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-owned-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Owned Ask Note",
            "content": {"text": "Owned source ref is allowed in Ask deep answers."},
        }
    )
    owned_source_id = api.store.list_source_items()[0].source_item_id

    class RefReturningAgenticService:
        def ready(self):
            return {"ok": True}

        def search(self, *_args, **_kwargs):
            return {
                "answer": "Deep answer with refs.",
                "retrieval": {},
                "source_refs": [
                    {"source_item_id": owned_source_id, "title": "Owned Ask Note"},
                    {"source_item_id": "src_other_tenant", "title": "Other tenant"},
                    {"title": "No source id"},
                ],
                "trace": {},
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = RefReturningAgenticService()

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "请分析 owned source refs。",
                "intent": "deep",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert [ref["source_item_id"] for ref in payload["source_refs"]] == [owned_source_id]
    assert payload["trace"]["dropped_source_refs"] == [
        {"source_item_id": "src_other_tenant", "reason": "tenant_or_owner_mismatch"},
        {"reason": "missing_source_item_id"},
    ]


def test_workspace_ask_deep_uses_final_declared_refs_and_redacts_trace_events() -> None:
    api = _api()
    ingest = IngestService(api.store)
    ingest.ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "ask-deep-irrelevant-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Irrelevant Deep Note",
            "content": {"text": "Irrelevant evidence should remain only in the retrieval trace."},
        }
    )
    ingest.ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "ask-deep-relevant-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Relevant Deep Note",
            "content": {"text": "Relevant evidence is the only final citation for the deep answer."},
        }
    )
    sources = {item.title: item for item in api.store.list_source_items()}
    irrelevant = sources["Irrelevant Deep Note"]
    relevant = sources["Relevant Deep Note"]

    class FinalRefAgenticService:
        def ready(self):
            return {"ok": True}

        def search(self, *_args, **_kwargs):
            retrieval = {
                "results": [
                    {
                        "source_item_id": irrelevant.source_item_id,
                        "title": irrelevant.title,
                        "snippet": irrelevant.content_text,
                        "citation": {"source_item_id": irrelevant.source_item_id, "title": irrelevant.title},
                    },
                    {
                        "source_item_id": relevant.source_item_id,
                        "title": relevant.title,
                        "snippet": relevant.content_text,
                        "citation": {"source_item_id": relevant.source_item_id, "title": relevant.title},
                    },
                ],
                "citations": [{"source_item_id": irrelevant.source_item_id, "title": irrelevant.title}],
            }
            return {
                "answer": "Deep answer uses the relevant final citation.",
                "retrieval": retrieval,
                "source_refs": [{"source_item_id": relevant.source_item_id}],
                "citations": ["Relevant dossier title alias"],
                "trace": {
                    "events": [
                        {"type": "session_start", "content": "SECRET_ROUTING_PROMPT", "event_id": "evt_0"},
                        {
                            "type": "tool_result",
                            "tool_name": "pska_pska_search",
                            "content": json.dumps(retrieval, ensure_ascii=False),
                            "event_id": "evt_1",
                        },
                    ]
                },
                "agentic_service": {"provider": "test"},
            }

    api.agentic_service = FinalRefAgenticService()

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "请深入分析 final refs。",
                "intent": "deep",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert [ref["source_item_id"] for ref in payload["source_refs"]] == [relevant.source_item_id]
    assert payload["source_refs"][0]["title"] == "Relevant Deep Note"
    assert "Relevant evidence" in payload["source_refs"][0]["snippet"]
    assert payload["evidence"]["results"][0]["source_item_id"] == relevant.source_item_id
    assert "dropped_source_refs" not in payload["quality_signals"]["flags"]
    payload_text = json.dumps(payload, ensure_ascii=False)
    assert "SECRET_ROUTING_PROMPT" not in payload_text
    assert "Irrelevant evidence should remain only in the retrieval trace." not in payload_text


def test_workspace_ask_deep_falls_back_to_quick_when_fastreact_is_unavailable() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-fallback-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Ask Fallback Note",
            "content": {"text": "Fallback should still answer from PSKA evidence when FastReAct is offline."},
        }
    )

    class BrokenAgenticService:
        def ready(self):
            return {"ok": False}

        def search(self, *_args, **_kwargs):
            raise AgenticServiceError("FastReAct offline")

    api.agentic_service = BrokenAgenticService()

    with _http_server(api) as base_url:
        status, payload = _http_json(
            base_url,
            "POST",
            "/workspace/ask",
            {
                "query": "请分析 FastReAct offline 时的 fallback 风险报告。",
                "intent": "deep",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert payload["ok"] is False
    assert payload["route"]["retrieval_owner"] == "pska"
    assert payload["route"]["fallback_from"] == "deep"
    assert payload["trace"]["fallback_reason"] == "agentic_service_unavailable"
    assert "PSKA evidence" in payload["answer"]
    assert "Ask Fallback Note" not in payload["answer"]
    assert payload["citations"][0]["title"] == "Ask Fallback Note"
    assert payload["quality_signals"]["quality_band"] == "needs_review"
    assert payload["quality_signals"]["report_readiness"] == "needs_human_review"
    assert "fallback" in payload["quality_signals"]["flags"]


def test_workspace_ask_stream_emits_product_events() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-stream-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Ask Stream Note",
            "content": {"text": "Ask stream should emit route, evidence, answer, trace, and done events."},
        }
    )

    with _http_server(api) as base_url:
        status, headers, body = _http_text(
            base_url,
            "POST",
            "/workspace/ask/stream",
            payload={
                "query": "请分析 Ask stream 事件有哪些？",
                "intent": "deep",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert "event: route" in body
    assert "event: agent_step" in body
    assert "event: evidence" in body
    assert "event: answer_delta" in body
    assert "event: trace" in body
    assert "event: done" in body
    assert "time_to_first_answer_ms" in body
    assert "time_to_first_agent_event_ms" in body
    assert "quality_signals" in body
    assert "pska_pska_search" in body


def test_workspace_ask_stream_quick_emits_planner_and_graphrag_steps() -> None:
    api = _api()
    IngestService(api.store).ingest_channel_payload(
        {
            "schema_version": "pska.channel_ingest.v1",
            "source_channel": "manual",
            "record_type": "note",
            "source_id": "workspace-ask-stream-quick-note",
            "owner_user_id": "user_primary",
            "space_id": "private_primary",
            "visibility": "private",
            "title": "Ask Stream Quick Note",
            "content": {"text": "Acme quick stream status is active."},
        }
    )

    with _http_server(api) as base_url:
        status, headers, body = _http_text(
            base_url,
            "POST",
            "/workspace/ask/stream",
            payload={
                "query": "Acme quick stream 状态是什么？",
                "intent": "quick",
                "user_id": "user_primary",
                "represented_user_id": "user_primary",
            },
        )

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert "event: route" in body
    assert "event: agent_step" in body
    assert "检索知识库与图谱" in body
    assert "lexical/vector/graph" not in body
    assert "\"retrieval_owner\": \"pska\"" in body
    assert "\"routing_owner\": \"pska_planner\"" in body
    assert "time_to_first_agent_event_ms" in body
    assert "pska_pska_search" not in body


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

        def search(self, query, user, *, represented_user_id=None, max_iterations=3, skills=None, tool_policy=None):
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
        self.calls = []

    def ready(self):
        return {"ok": True, "provider": "test", "adapter": "fake"}

    def search(
        self,
        query,
        user,
        *,
        represented_user_id=None,
        max_iterations=3,
        skills=None,
        tool_policy=None,
        session_id=None,
    ):
        self.calls.append(
            {
                "query": query,
                "user_id": user.user_id,
                "tenant_id": user.tenant_id,
                "represented_user_id": represented_user_id,
                "max_iterations": max_iterations,
                "skills": skills,
                "tool_policy": tool_policy,
                "session_id": session_id,
            }
        )
        retrieval = self.retrieval.search(query, user, represented_user_id=represented_user_id)
        events = self._events(query, retrieval)
        return {
            "answer": "Fake external agentic answer.",
            "retrieval": to_jsonable(retrieval),
            "trace": {
                "events": events,
                "query_understanding": {"intent": "test", "privacy_boundary": "acl_first"},
                "retrieval_plan": ["external_agentic_service", "pska_search"],
                "iterations": [{"iteration": "1", "query": query}],
                "evidence_check": "has_citations" if retrieval.citations else "insufficient_evidence",
            },
            "agentic_service": {"provider": "test", "adapter": "fake"},
        }

    def search_event_stream(
        self,
        query,
        user,
        *,
        represented_user_id=None,
        max_iterations=3,
        skills=None,
        tool_policy=None,
        session_id=None,
    ):
        self.calls.append(
            {
                "query": query,
                "user_id": user.user_id,
                "tenant_id": user.tenant_id,
                "represented_user_id": represented_user_id,
                "max_iterations": max_iterations,
                "skills": skills,
                "tool_policy": tool_policy,
                "session_id": session_id,
            }
        )
        retrieval = self.retrieval.search(query, user, represented_user_id=represented_user_id)
        yield from self._events(query, retrieval, session_id=session_id)
        yield {"type": "done", "content": "[DONE]"}

    def _events(self, query, retrieval, *, session_id=None):
        return [
            {"type": "session_start", "content": query, "session_id": session_id or "fake-session", "event_id": "fake:0"},
            {"type": "think", "content": "Plan the search", "session_id": session_id or "fake-session", "event_id": "fake:1"},
            {
                "type": "tool_call",
                "tool_name": "pska_pska_search",
                "tool_args": {"query": query, "top_k": 8},
                "tool_call_id": "call-1",
                "session_id": session_id or "fake-session",
                "event_id": "fake:2",
            },
            {
                "type": "tool_result",
                "tool_name": "pska_pska_search",
                "content": json.dumps(to_jsonable(retrieval), ensure_ascii=False),
                "tool_call_id": "call-1",
                "session_id": session_id or "fake-session",
                "event_id": "fake:3",
            },
            {
                "type": "session_end",
                "content": json.dumps({"answer": "Fake external agentic answer.", "source_refs": to_jsonable(retrieval.citations)}, ensure_ascii=False),
                "session_id": session_id or "fake-session",
                "event_id": "fake:4",
            },
        ]


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


def _http_text(base_url: str, method: str, path: str, headers: dict | None = None, payload: dict | None = None):
    conn = HTTPConnection(base_url, timeout=5)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if body:
        request_headers.setdefault("content-type", "application/json")
    conn.request(method, path, body=body, headers=request_headers)
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    conn.close()
    return response.status, response_headers, data
