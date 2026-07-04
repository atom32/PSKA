from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

from psycopg.types.json import Jsonb

from pska_core.acl import ACLService
from pska_core.agent_capture import capture_agent_conversation
from pska_core.agentic_service import PSKA_QA_SKILL, AgenticServiceError, build_agentic_service_client, normalize_agentic_event_response
from pska_core.auth import AuthError, RequestContext, authenticate_headers, context_from_headers, service_token_required
from pska_core.candidates import CandidateWriteService
from pska_core.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_FILES_MAX_BYTES,
    DEFAULT_SPREADSHEET_MAX_COLUMNS,
    DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET,
    DatabaseConfig,
    DocumentParserConfig,
    PSKAConfig,
    ServiceConfig,
)
from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.discovery import DISCOVERY_TODAY_SCORE_THRESHOLD, DiscoveryService
from pska_core.embeddings import EmbeddingConfig, build_embedding_provider
from pska_core.enums import ReviewType, UserRole, Visibility
from pska_core.extraction import ExtractionService
from pska_core.fastreact_protocol import compact_trace_for_context
from pska_core.files_connector import extract_text_from_bytes, scan_files
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.ingest import IngestService, postgres_safe_json, postgres_safe_text
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.knowledge_sources import KnowledgeSourceService, knowledge_source_id
from pska_core.memory import MemoryService
from pska_core.mcp_server import MCPServer, PROTOCOL_VERSION
from pska_core.models import (
    DEFAULT_TENANT_ID,
    ArtifactSupport,
    AskConversation,
    AskMessage,
    AskRun,
    ChannelIngestPayload,
    KnowledgeBase,
    KnowledgeBaseSource,
    KnowledgeBaseSourceItem,
    KnowledgeSource,
    KnowledgeTopic,
    PassageWindow,
    PromptProfile,
    ReviewItem,
    SourceRef,
    TopicMention,
    WorkspaceActivityEvent,
    WritingBoard,
    WritingEdge,
    WritingNode,
)
from pska_core.offline_index import OfflineIndexService
from pska_core.chunking import preview_chunking
from pska_core.processing import resolve_processing_config
from pska_core.retrieval import RetrievalService, query_focused_evidence_snippet
from pska_core.review import ReviewService
from pska_core.serde import to_jsonable
from pska_core.source_adapters import build_source_adapter, supported_source_adapters
from pska_core.store_postgres import PostgresKnowledgeStore


ASK_READ_TOOL_PROFILE = "ask_read"
ASK_READ_ONLY_TOOLS = [
    "pska_pska_search",
    "pska_pska_index_status",
    "pska_pska_read_evidence_context",
    "pska_pska_graph_context",
    "pska_pska_digest_context",
]

ASK_EXECUTION_INTENTS = {"auto", "quick", "deep"}
ASK_INTENTS = {
    "greeting",
    "chitchat",
    "product_help",
    "kb_search",
    "doc_only",
    "follow_up",
    "clarification",
    "graph_research",
    "writing",
}
ASK_RETRIEVAL_INTENTS = {"kb_search", "doc_only", "follow_up", "graph_research", "writing"}
ASK_NON_RETRIEVAL_INTENTS = {"greeting", "chitchat", "product_help", "clarification"}

PROMPT_PROFILE_TYPES = {"ask", "digest", "review", "writing"}
DEFAULT_PROMPT_PROFILE_CONFIGS: dict[str, dict[str, Any]] = {
    "ask": {
        "answer_language": "zh-CN",
        "style": "conclusion_first",
        "citation_policy": "required_when_evidence_exists",
        "no_answer_policy": "explain_missing_evidence",
    },
    "digest": {
        "candidate_policy": "source_refs_required",
        "low_confidence_policy": "route_to_review",
        "outputs": ["digest_note", "knowledge_claim", "review_item", "memory_candidate", "relationship_candidate"],
    },
    "review": {
        "default_queue": "pending",
        "high_impact_policy": "manual_approval",
    },
    "writing": {
        "tone": "clear_evidence_brief",
        "citation_policy": "preserve_source_refs",
    },
}


class PSKAApi:
    def __init__(self, database_url: str | None = None, *, config: PSKAConfig | None = None) -> None:
        if config is None:
            config = PSKAConfig(database=DatabaseConfig(url=database_url or DEFAULT_DATABASE_URL))
        self.config = config
        self.store = PostgresKnowledgeStore(database_url or config.database.url)
        embedding_provider = build_embedding_provider(config.embedding_runtime_config())
        self.retrieval = RetrievalService(self.store, ACLService(self.store), embedding_provider=embedding_provider)
        self.agentic_service = build_agentic_service_client(config.agentic_service_runtime_config())
        self.ingest = IngestService(self.store, embedding_provider=embedding_provider, **config.ingest_kwargs())
        self.extraction = ExtractionService(self.store, llm_config=config.llm)
        self.jobs = JobService(self.store, workspace_root=config.workspace.root, embedding_config=config.embedding_runtime_config())
        self.reviews = ReviewService(self.store)
        self.memory = MemoryService(self.store)
        self.candidates = CandidateWriteService(self.store)
        self.mcp = MCPServer(database_url or config.database.url, store=self.store, config=config, embedding_provider=embedding_provider)

    def ensure_context_identity(self, context: RequestContext) -> None:
        ensure_identity = getattr(self.store, "ensure_identity", None)
        if not callable(ensure_identity):
            return
        role = UserRole.AGENT_SERVICE if context.caller == "agent_service" or context.user_id == "agent_service" else UserRole.USER
        ensure_identity(tenant_id=context.tenant_id, user_id=context.user_id, role=role)
        if context.represented_user_id and context.represented_user_id != context.user_id:
            ensure_identity(tenant_id=context.tenant_id, user_id=context.represented_user_id, role=UserRole.USER)

    def health(self) -> dict[str, Any]:
        return {"ok": True, "database": getattr(self.store, "database_url", "in_memory")}

    def ready(self) -> dict[str, Any]:
        config = _api_config(self)
        embedding_config = config.embedding_runtime_config()
        checks: dict[str, Any] = {
            "database": self._database_ready(),
            "schema": self._schema_ready(),
            "index": self._index_ready(),
            "embedding": {
                "provider": embedding_config.provider,
                "model": embedding_config.model,
                "configured": embedding_config.provider.strip().lower() not in {"", "disabled", "none", "off"},
            },
            "llm": {
                "api_key_file_configured": bool(config.llm.api_key_file),
                "model": config.llm.model,
                "base_url": config.llm.base_url,
            },
            "jobs": self._jobs_ready(),
            "metrics": self._metrics_ready(),
            "agentic_service": self._agentic_service_ready(),
            "mcp": self._mcp_ready(),
        }
        required_ok = checks["database"]["ok"] and checks["schema"]["ok"] and checks["mcp"]["ok"]
        return {"ok": required_ok, "checks": checks}

    def workspace_readiness(self, context: RequestContext | None = None) -> dict[str, Any]:
        tenant_id = context.tenant_id if context else DEFAULT_TENANT_ID
        ready = self.ready()
        checks = dict(ready.get("checks") or {})
        stats = self.job_stats(tenant_id=tenant_id)["stats"]
        agentic_pska_mcp_ok = _agentic_pska_mcp_ok(checks.get("agentic_service"))
        return {
            "ok": bool(ready.get("ok")),
            "tenant_id": tenant_id,
            "checks": {
                **checks,
                "index": {
                    **dict(checks.get("index") or {}),
                    "counts": self.index_status(tenant_id=tenant_id),
                    "offline_index": OfflineIndexService(self.store).freshness(tenant_id=tenant_id),
                },
                "digest_worker": {
                    "ok": True,
                    "backlog": stats.get("digest_backlog") or {},
                    "recent_failed": stats.get("recent_failed") or [],
                    "running_jobs": stats.get("running_jobs") or [],
                },
            },
            "summary": {
                "database_ok": bool((checks.get("database") or {}).get("ok")),
                "schema_ok": bool((checks.get("schema") or {}).get("ok")),
                "mcp_ok": bool((checks.get("mcp") or {}).get("ok")),
                "fastreact_ok": bool((checks.get("agentic_service") or {}).get("ok")) and agentic_pska_mcp_ok,
                "fastreact_pska_mcp_ok": agentic_pska_mcp_ok,
                "digest_backlog_jobs": int(((stats.get("digest_backlog") or {}).get("jobs")) or 0),
            },
        }

    def workspace_prompt_profiles(self, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        profiles = self.store.list_prompt_profiles(tenant_id=tenant_id, owner_user_id=owner_user_id)
        effective = _effective_prompt_profiles(self.store, tenant_id=tenant_id, owner_user_id=owner_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "profiles": [_prompt_profile_payload(profile) for profile in profiles],
            "effective": effective,
            "defaults": _default_prompt_profiles_payload(tenant_id=tenant_id),
        }

    def update_workspace_prompt_profiles(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        raw_profiles = payload.get("profiles")
        if isinstance(raw_profiles, dict):
            profile_inputs = [{"profile_type": key, "config": value} for key, value in raw_profiles.items()]
        elif isinstance(raw_profiles, list):
            profile_inputs = [item for item in raw_profiles if isinstance(item, dict)]
        else:
            profile_inputs = [payload]
        stored: list[PromptProfile] = []
        for item in profile_inputs:
            profile_type = str(item.get("profile_type") or item.get("type") or "ask").strip().lower()
            if profile_type not in PROMPT_PROFILE_TYPES:
                raise ValueError(f"unsupported prompt profile type: {profile_type}")
            scope = str(item.get("scope") or "user").strip().lower()
            if scope not in {"tenant", "user"}:
                raise ValueError("prompt profile scope must be tenant or user")
            profile_owner = None if scope == "tenant" else owner_user_id
            config = item.get("config") if isinstance(item.get("config"), dict) else {}
            profile = PromptProfile(
                prompt_profile_id=_prompt_profile_id(tenant_id=tenant_id, scope=scope, owner_user_id=profile_owner, profile_type=profile_type),
                tenant_id=tenant_id,
                owner_user_id=profile_owner,
                profile_type=profile_type,
                scope=scope,
                name=str(item.get("name") or _prompt_profile_default_name(profile_type, scope)),
                config=config,
            )
            stored.append(self.store.upsert_prompt_profile(profile))
        effective = _effective_prompt_profiles(self.store, tenant_id=tenant_id, owner_user_id=owner_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "profiles": [_prompt_profile_payload(profile) for profile in stored],
            "effective": effective,
        }

    def workspace_knowledge_bases(self, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        default_space_id = str(payload.get("default_space_id") or payload.get("space_id") or "").strip() or None
        include_deleted = _truthy(payload.get("include_deleted")) or _truthy(payload.get("include_archived"))
        default_knowledge_base = self.store.ensure_default_knowledge_base(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_by_user_id=_actor_user_id(context, payload),
            default_space_id=default_space_id,
        )
        knowledge_bases = self.store.list_knowledge_bases(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            include_deleted=include_deleted,
        )
        if default_knowledge_base.knowledge_base_id not in {knowledge_base.knowledge_base_id for knowledge_base in knowledge_bases}:
            knowledge_bases = [default_knowledge_base, *knowledge_bases]
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_bases": [_knowledge_base_payload(self.store, knowledge_base) for knowledge_base in knowledge_bases],
            "default_knowledge_base_id": default_knowledge_base.knowledge_base_id,
            "include_deleted": include_deleted,
        }

    def workspace_knowledge_base(
        self,
        knowledge_base_id: str,
        payload: dict[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        knowledge_base = _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base": _knowledge_base_payload(self.store, knowledge_base, include_source_item_ids=True),
        }

    def create_workspace_knowledge_base(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("knowledge base name is required")
        visibility = Visibility(str(payload.get("visibility") or Visibility.PRIVATE.value))
        knowledge_base = KnowledgeBase(
            knowledge_base_id=str(payload.get("knowledge_base_id") or f"kb_{uuid4().hex}"),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_by_user_id=_actor_user_id(context, payload),
            slug=_knowledge_base_slug(str(payload.get("slug") or name)),
            name=name,
            description=str(payload.get("description") or ""),
            kb_type=str(payload.get("kb_type") or payload.get("type") or "document"),
            status=str(payload.get("status") or "active"),
            visibility=visibility,
            visible_team_ids=_string_list(payload.get("visible_team_ids")),
            default_space_id=str(payload.get("default_space_id") or payload.get("space_id") or "").strip() or None,
            is_default=_truthy(payload.get("is_default")),
            config=dict(payload.get("config") or {}) if isinstance(payload.get("config"), dict) else {},
            readiness=dict(payload.get("readiness") or {}) if isinstance(payload.get("readiness"), dict) else {},
        )
        stored = self.store.upsert_knowledge_base(knowledge_base)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base": _knowledge_base_payload(self.store, stored),
        }

    def update_workspace_knowledge_base(
        self,
        knowledge_base_id: str,
        payload: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        status = str(payload.get("status") or "").strip().lower()
        if status in {"archived", "deleted"}:
            _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            knowledge_base = self.store.archive_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            return {
                "ok": True,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "knowledge_base": _knowledge_base_payload(self.store, knowledge_base),
            }

        knowledge_base = _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("knowledge base name is required")
            knowledge_base.name = name
        if "slug" in payload:
            knowledge_base.slug = _knowledge_base_slug(str(payload.get("slug") or knowledge_base.name))
        if "description" in payload:
            knowledge_base.description = str(payload.get("description") or "")
        if "kb_type" in payload or "type" in payload:
            knowledge_base.kb_type = str(payload.get("kb_type") or payload.get("type") or knowledge_base.kb_type)
        if status:
            knowledge_base.status = status
            if status == "active":
                knowledge_base.deleted_at = None
        if "visibility" in payload:
            knowledge_base.visibility = Visibility(str(payload.get("visibility") or Visibility.PRIVATE.value))
        if "visible_team_ids" in payload:
            knowledge_base.visible_team_ids = _string_list(payload.get("visible_team_ids"))
        if "default_space_id" in payload or "space_id" in payload:
            knowledge_base.default_space_id = str(payload.get("default_space_id") or payload.get("space_id") or "").strip() or None
        if "pinned" in payload:
            knowledge_base.pinned_at = datetime.now(UTC) if _truthy(payload.get("pinned")) else None
        if isinstance(payload.get("config"), dict):
            knowledge_base.config = dict(payload["config"])
        if isinstance(payload.get("readiness"), dict):
            knowledge_base.readiness = dict(payload["readiness"])
        stored = self.store.upsert_knowledge_base(knowledge_base)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base": _knowledge_base_payload(self.store, stored),
        }

    def delete_workspace_knowledge_base(
        self,
        knowledge_base_id: str,
        payload: dict[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        knowledge_base = self.store.archive_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base": _knowledge_base_payload(self.store, knowledge_base),
        }

    def restore_workspace_knowledge_base(
        self,
        knowledge_base_id: str,
        payload: dict[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        knowledge_base = self.store.restore_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base": _knowledge_base_payload(self.store, knowledge_base, include_source_item_ids=True),
        }

    def pin_workspace_knowledge_base(
        self,
        knowledge_base_id: str,
        payload: dict[str, Any] | None = None,
        context: RequestContext | None = None,
        *,
        pinned: bool = True,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        knowledge_base = _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        knowledge_base.pinned_at = datetime.now(UTC) if pinned else None
        stored = self.store.upsert_knowledge_base(knowledge_base)
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base": _knowledge_base_payload(self.store, stored),
        }

    def index_status(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "source_items": self.store.count_table("source_items", tenant_id=tenant_id),
            "knowledge_bases": self.store.count_table("knowledge_bases", tenant_id=tenant_id),
            "knowledge_base_sources": self.store.count_table("knowledge_base_sources", tenant_id=tenant_id),
            "knowledge_base_source_items": self.store.count_table("knowledge_base_source_items", tenant_id=tenant_id),
            "documents": self.store.count_table("documents", tenant_id=tenant_id),
            "chunks": self.store.count_table("chunks", tenant_id=tenant_id),
            "entities": self.store.count_table("entities", tenant_id=tenant_id),
            "hyperedges": self.store.count_table("hyperedges", tenant_id=tenant_id),
            "knowledge_claims": self.store.count_table("knowledge_claims", tenant_id=tenant_id),
            "digest_notes": self.store.count_table("digest_notes", tenant_id=tenant_id),
            "graph_nodes": self.store.count_table("graph_nodes", tenant_id=tenant_id),
            "graph_edges": self.store.count_table("graph_edges", tenant_id=tenant_id),
            "review_items": self.store.count_table("review_items", tenant_id=tenant_id),
            "jobs": self.store.count_table("jobs", tenant_id=tenant_id),
            "offline_index_states": self.store.count_table("offline_index_states", tenant_id=tenant_id),
            "processing_spans": self.store.count_table("processing_spans", tenant_id=tenant_id),
            "writing_boards": self.store.count_table("writing_boards", tenant_id=tenant_id),
            "writing_nodes": self.store.count_table("writing_nodes", tenant_id=tenant_id),
            "writing_edges": self.store.count_table("writing_edges", tenant_id=tenant_id),
        }

    def _database_ready(self) -> dict[str, Any]:
        try:
            return {"ok": True, "source_items": self.store.count_table("source_items")}
        except Exception as exc:  # noqa: BLE001 - readiness reports dependency failures.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _schema_ready(self) -> dict[str, Any]:
        tables = [
            "source_items",
            "documents",
            "chunks",
            "passage_windows",
            "graph_nodes",
            "graph_edges",
            "entities",
            "hyperedges",
            "knowledge_claims",
            "digest_notes",
            "knowledge_claim_links",
            "digest_note_links",
            "review_items",
            "jobs",
            "connector_states",
            "knowledge_sources",
            "knowledge_bases",
            "knowledge_base_sources",
            "knowledge_base_source_items",
            "sync_runs",
            "processing_spans",
            "offline_index_states",
            "writing_boards",
            "writing_nodes",
            "writing_edges",
        ]
        counts: dict[str, int] = {}
        missing: list[str] = []
        for table in tables:
            try:
                counts[table] = self.store.count_table(table)
            except Exception:  # noqa: BLE001 - report all missing/broken tables together.
                missing.append(table)
        return {"ok": not missing, "tables": counts, "missing": missing}

    def _index_ready(self) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "counts": self.index_status(),
                "offline_index": OfflineIndexService(self.store).freshness(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _jobs_ready(self) -> dict[str, Any]:
        try:
            return {"ok": True, **self.job_stats()["stats"]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _metrics_ready(self) -> dict[str, Any]:
        try:
            return {"ok": True, **self.metrics()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _agentic_service_ready(self) -> dict[str, Any]:
        try:
            return self.agentic_service.ready()
        except AgenticServiceError as exc:
            return {"ok": False, "provider": _api_config(self).agentic_service.provider, "error": str(exc)}

    def _mcp_ready(self) -> dict[str, Any]:
        response = self.mcp.handle({"jsonrpc": "2.0", "id": "ready", "method": "tools/list", "params": {}})
        tools = ((response or {}).get("result") or {}).get("tools") or []
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        required = [
            "pska_search",
            "pska_index_status",
            "pska_read_evidence_context",
            "pska_graph_context",
            "pska_digest_context",
            "pska_job_context",
            "pska_write_candidates",
        ]
        missing = [name for name in required if name not in names]
        return {
            "ok": not missing,
            "protocol_version": PROTOCOL_VERSION,
            "tool_count": len(names),
            "tools": names,
            "required_tools": required,
            "missing_required_tools": missing,
        }

    def mcp_jsonrpc(self, request: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any] | None:
        try:
            return self.mcp.handle(request, context=context)
        except Exception as exc:  # noqa: BLE001 - JSON-RPC transports return protocol errors.
            request_id = request.get("id") if isinstance(request, dict) else None
            return self.mcp.error(request_id, -32000, f"{type(exc).__name__}: {exc}")

    def ingest_payload(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        item = self.ingest.ingest_channel_payload(ChannelIngestPayload.from_mapping(payload))
        return to_jsonable(item)

    def ingest_connector_record(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        record_payload = context.apply_to_payload(payload) if context else payload
        channel_payload = connector_record_to_payload(record_payload)
        item = self.ingest.ingest_channel_payload(channel_payload)
        return {
            "source_item": to_jsonable(item),
            "channel_payload": to_jsonable(channel_payload),
        }

    def upsert_connector_state(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        state = connector_state_from_mapping(payload)
        if payload.get("last_success_at") == "now":
            state.last_success_at = datetime.now(UTC)
            state.sync_status = str(payload.get("sync_status") or "succeeded")
            state.last_error = None
            state.last_error_at = None
        if payload.get("last_error_at") == "now" or payload.get("last_error"):
            state.last_error_at = datetime.now(UTC)
            state.sync_status = str(payload.get("sync_status") or "failed")
        return {"connector_state": to_jsonable(self.store.upsert_connector_state(state))}

    def connector_states(
        self,
        *,
        tenant_id: str | None = None,
        owner_user_id: str | None = None,
        connector_id: str | None = None,
        connector_state_id: str | None = None,
    ) -> dict[str, Any]:
        if connector_state_id:
            state = self.store.get_connector_state(connector_state_id)
            if tenant_id and state.tenant_id != tenant_id:
                raise PermissionError("connector state tenant mismatch")
            return {"connector_state": to_jsonable(state)}
        return {
            "connector_states": to_jsonable(
                self.store.list_connector_states(tenant_id=tenant_id, owner_user_id=owner_user_id, connector_id=connector_id)
            )
        }

    def search(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        user = self.store.get_user(payload.get("user_id") or "user_primary", tenant_id=tenant_id)
        represented_user_id = payload.get("represented_user_id")
        owner_user_id = str(represented_user_id or user.user_id)
        scope = _resolve_knowledge_base_scope(
            self.store,
            _scope_from_payload(payload),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        scoped_source_item_ids = _ask_scope_source_item_ids(scope)
        scope_mode = _ask_scope_mode(scope, ask_intent="kb_search")
        result = to_jsonable(
            self.retrieval.search(
                payload["query"],
                user,
                represented_user_id=represented_user_id,
                top_k=int(payload.get("top_k") or 5),
                source_item_ids=_retrieval_source_item_ids_arg(scoped_source_item_ids, scope_mode=scope_mode),
                scope_mode=scope_mode,
            )
        )
        result["scope_applied"] = _ask_scope_applied(scope, ask_intent="kb_search")
        return result

    def agentic_search(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        user = self.store.get_user(payload.get("user_id") or "user_primary", tenant_id=tenant_id)
        return self._agentic_service_search(
            str(payload["query"]),
            user,
            represented_user_id=payload.get("represented_user_id"),
            max_iterations=int(payload.get("max_iterations") or 3),
        )

    def _agentic_service_search(
        self,
        query: str,
        user: Any,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "represented_user_id": represented_user_id,
            "max_iterations": max_iterations,
        }
        if skills is not None:
            kwargs["skills"] = skills
        if tool_policy is not None:
            kwargs["tool_policy"] = tool_policy
        if session_id:
            kwargs["session_id"] = session_id
        try:
            response = self.agentic_service.search(query, user, **kwargs)
        except TypeError as exc:
            if (
                (skills is not None and "skills" in str(exc))
                or (tool_policy is not None and "tool_policy" in str(exc))
                or (session_id is not None and "session_id" in str(exc))
            ):
                kwargs.pop("skills", None)
                kwargs.pop("tool_policy", None)
                kwargs.pop("session_id", None)
            else:
                raise
            response = self.agentic_service.search(query, user, **kwargs)
        retrieval = response.get("retrieval") if isinstance(response.get("retrieval"), dict) else {}
        trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
        if str(trace.get("status") or "").lower() == "error":
            detail = str(trace.get("error") or response.get("answer") or "agentic service returned an error trace")
            raise AgenticServiceError(detail)
        return {
            "ok": True,
            "mode": "agentic",
            "requires_agentic_service_online": True,
            "query": query,
            "answer": str(response.get("answer") or ""),
            "retrieval": retrieval,
            "trace": trace,
            "source_refs": response.get("source_refs") if isinstance(response.get("source_refs"), list) else [],
            "agentic_service": response.get("agentic_service") if isinstance(response.get("agentic_service"), dict) else {},
        }

    def extract_all(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        reports = self.extraction.extract_all_visible(owner_user_id=payload.get("owner_user_id"), tenant_id=tenant_id)
        return {"reports": to_jsonable(reports), "index_status": self.index_status()}

    def review_items(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return {"review_items": to_jsonable(self.store.list_review_items(tenant_id=tenant_id))}

    def propose_profile_update(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        profile_delta = payload.get("profile_delta") or payload.get("profile")
        if not isinstance(profile_delta, dict) or not profile_delta:
            raise ValueError("profile_delta must be a non-empty object")

        result = self.memory.propose_profile_update(
            owner_user_id=str(payload.get("owner_user_id") or "user_primary"),
            profile_delta=profile_delta,
            source_refs=_source_refs_from_payload(payload.get("source_refs")),
            sensitivity=str(payload.get("sensitivity") or "normal"),
            confidence=float(payload.get("confidence", 0.8)),
            tenant_id=tenant_id,
        )
        if isinstance(result, ReviewItem):
            return {"review_item": to_jsonable(result)}
        return {"profile_card": to_jsonable(result)}

    def write_candidates(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        return {"summary": to_jsonable(self.candidates.write_candidates(payload))}

    def approve_review_item(self, review_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "")
        if payload.get("apply"):
            review_item = self.reviews.approve_and_apply(review_item_id, actor_user_id=actor_user_id, reason=reason)
        else:
            review_item = self.reviews.approve(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"review_item": to_jsonable(review_item), "application_result": _review_application_result(self.store, review_item)}

    def reject_review_item(self, review_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "")
        review_item = self.reviews.reject(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"review_item": to_jsonable(review_item), "application_result": _review_application_result(self.store, review_item)}

    def apply_review_item(self, review_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "")
        review_item = self.reviews.apply(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"review_item": to_jsonable(review_item), "application_result": _review_application_result(self.store, review_item)}

    def accept_discovery_item(self, discovery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "accepted from workspace discovery")
        discovery = self.store.update_discovery_item_status(discovery_id, "accepted")
        review_item = _linked_review_item(self.store, discovery)
        review_result = None
        if review_item and review_item.status == "pending":
            review_result = self.reviews.approve(review_item.review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"discovery": to_jsonable(discovery), "review_item": to_jsonable(review_result) if review_result else None}

    def ignore_discovery_item(self, discovery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "ignored from workspace discovery")
        discovery = self.store.update_discovery_item_status(discovery_id, "ignored")
        review_item = _linked_review_item(self.store, discovery)
        review_result = None
        if review_item and review_item.status == "pending":
            review_result = self.reviews.reject(review_item.review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"discovery": to_jsonable(discovery), "review_item": to_jsonable(review_result) if review_result else None}

    def snooze_discovery_item(self, discovery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        discovery = self.store.update_discovery_item_status(discovery_id, "snoozed")
        return {"discovery": to_jsonable(discovery), "review_item": None}

    def submit_job(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        job_payload = {**dict(payload.get("payload") or {}), "tenant_id": payload.get("tenant_id") or DEFAULT_TENANT_ID}
        if payload.get("owner_user_id") and not job_payload.get("owner_user_id"):
            job_payload["owner_user_id"] = payload["owner_user_id"]
        job = self.jobs.submit(
            str(payload["job_type"]),
            job_payload,
            max_attempts=int(payload.get("max_attempts") or 3),
            priority=int(payload.get("priority") or job_payload.get("priority") or 0),
        )
        return {"job": to_jsonable(job)}

    def run_jobs(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        service = JobService(
            self.store,
            workspace_root=self.config.workspace.root,
            embedding_config=self.config.embedding_runtime_config(),
            tenant_id=tenant_id,
        )
        report = service.run_available(limit=int(payload.get("limit") or 1))
        return {"run": to_jsonable(report)}

    def chunking_preview(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        text = str(payload.get("text") or payload.get("content") or "")
        if not text:
            raise ValueError("text is required")
        source_config = payload.get("source_config") if isinstance(payload.get("source_config"), dict) else None
        processing_overrides = payload.get("processing_config") or payload.get("process_config")
        if processing_overrides is None and isinstance(payload.get("chunking"), dict):
            processing_overrides = {"chunking": payload["chunking"]}
        if processing_overrides is None and isinstance(payload.get("config"), dict):
            processing_overrides = {"chunking": payload["config"]}
        if processing_overrides is None:
            chunking_keys = {key: payload[key] for key in ["strategy", "chunk_size", "chunk_overlap", "separators"] if key in payload}
            if chunking_keys:
                processing_overrides = {"chunking": chunking_keys}
        processing_config = resolve_processing_config(source_config, processing_overrides)
        preview = preview_chunking(text, processing_config.get("chunking"))
        return {
            "ok": True,
            "tenant_id": payload.get("tenant_id") or DEFAULT_TENANT_ID,
            "processing_config": processing_config,
            "preview": preview,
        }

    def source_preview(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        source = _draft_knowledge_source_from_payload(payload, context=context)
        adapter = build_source_adapter(self.store, source, processing_config=payload.get("processing_config") if isinstance(payload.get("processing_config"), dict) else None)
        limit = max(1, min(int(payload.get("limit") or 10), 50))
        preview = adapter.preview(limit=limit)
        return {"ok": bool(preview.get("ok", True)), "source": to_jsonable(source), "preview": preview, "adapters": supported_source_adapters()}

    def create_knowledge_source(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        source_service = KnowledgeSourceService(self.store)
        owner_user_id = _owner_user_id_for_write(payload, context)
        tenant_id = str(payload.get("tenant_id") or (context.tenant_id if context else DEFAULT_TENANT_ID))
        source_type = _normal_source_type(payload.get("source_type") or payload.get("kind") or payload.get("type"))
        value = _source_value_from_payload(payload, source_type)
        processing_config = payload.get("processing_config") if isinstance(payload.get("processing_config"), dict) else None
        common = {
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "name": payload.get("name"),
            "space_id": str(payload.get("space_id") or "private_primary"),
            "visibility": Visibility(str(payload.get("visibility") or Visibility.PRIVATE)),
            "visible_team_ids": _string_list(payload.get("visible_team_ids")),
        }
        if source_type == "folder":
            source = source_service.add_folder_source(
                Path(value),
                ignore=_string_list(payload.get("ignore")),
                max_bytes=_optional_positive_int(payload.get("max_bytes")) or self.config.files.max_bytes,
                spreadsheet_max_rows_per_sheet=_optional_positive_int(
                    payload.get("spreadsheet_max_rows_per_sheet") or payload.get("spreadsheet_row_limit_per_sheet")
                )
                or self.config.files.spreadsheet_max_rows_per_sheet,
                spreadsheet_max_columns=_optional_positive_int(
                    payload.get("spreadsheet_max_columns") or payload.get("spreadsheet_column_limit")
                )
                or self.config.files.spreadsheet_max_columns,
                document_parser=self.config.document_parser,
                **common,
            )
        elif source_type == "rss":
            source = source_service.add_rss_source(value, processing_config=processing_config, **common)
        elif source_type == "url":
            source = source_service.add_url_source(value, processing_config=processing_config, **common)
        else:
            raise ValueError(f"Unsupported source_type: {source_type}")
        knowledge_bases = _knowledge_bases_for_payload(
            self.store,
            payload,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            actor_user_id=_actor_user_id(context, payload),
            default_space_id=str(payload.get("default_space_id") or "").strip() or None,
        )
        _bind_source_to_knowledge_bases(
            self.store,
            source,
            knowledge_bases=knowledge_bases,
            source_item_ids=[],
            actor_user_id=_actor_user_id(context, payload),
            membership_type="source",
        )
        preview = None
        if bool(payload.get("preview", False)):
            preview = build_source_adapter(self.store, source, processing_config=processing_config).preview(limit=max(1, min(int(payload.get("limit") or 5), 20)))
        return {
            "ok": True,
            "knowledge_source": to_jsonable(source),
            "knowledge_base_ids": [knowledge_base.knowledge_base_id for knowledge_base in knowledge_bases],
            "preview": preview,
            "adapters": supported_source_adapters(),
        }

    def create_text_source(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        text = str(payload.get("text") or payload.get("content") or payload.get("body") or "").strip()
        if not text:
            raise ValueError("text source requires non-empty text")
        title = str(payload.get("title") or payload.get("name") or _default_inline_title(text, fallback="Pasted text")).strip()
        source = _inline_knowledge_source_from_payload(payload, context=context, source_type="text", title=title, text=text)
        self.store.upsert_knowledge_source(source)
        return self._sync_inline_source(source, payload, context=context, action="text")

    def create_upload_source(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        filename = str(payload.get("filename") or payload.get("name") or "upload.txt").strip() or "upload.txt"
        text, content_type, size_bytes, extraction = _upload_text_from_payload(
            payload,
            max_bytes=self.config.files.max_bytes,
            spreadsheet_max_rows_per_sheet=self.config.files.spreadsheet_max_rows_per_sheet,
            spreadsheet_max_columns=self.config.files.spreadsheet_max_columns,
            document_parser=self.config.document_parser,
        )
        if not text.strip():
            raise ValueError("uploaded file has no readable text")
        title = str(payload.get("title") or Path(filename).name or _default_inline_title(text, fallback="Uploaded file")).strip()
        source = _inline_knowledge_source_from_payload(
            {
                **payload,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "metadata": {**dict(payload.get("metadata") or {}), "extraction": extraction},
            },
            context=context,
            source_type="upload",
            title=title,
            text=text,
        )
        self.store.upsert_knowledge_source(source)
        return self._sync_inline_source(source, payload, context=context, action="upload")

    def _sync_inline_source(
        self,
        source: KnowledgeSource,
        payload: dict[str, Any],
        *,
        context: RequestContext | None,
        action: str,
    ) -> dict[str, Any]:
        processing_config = payload.get("processing_config") if isinstance(payload.get("processing_config"), dict) else None
        adapter = build_source_adapter(
            self.store,
            source,
            embedding_provider=build_embedding_provider(EmbeddingConfig(provider="disabled")),
            processing_config=processing_config,
        )
        preview = adapter.preview(limit=1)
        report = adapter.sync(limit=1)
        run = KnowledgeSourceService(self.store).record_sync_report(source, report)
        knowledge_bases = _knowledge_bases_for_source_or_payload(
            self.store,
            payload,
            source,
            actor_user_id=_actor_user_id(context, payload),
        )
        _bind_source_to_knowledge_bases(
            self.store,
            source,
            knowledge_bases=knowledge_bases,
            source_item_ids=report.source_item_ids,
            actor_user_id=_actor_user_id(context, payload),
            membership_type=action,
        )
        digest_mode = str(payload.get("digest_mode") or "after_upload").strip().lower()
        digest = None
        if digest_mode in {"after_upload", "auto", "immediate"} and report.source_item_ids:
            digest = self.schedule_digest(
                {
                    "tenant_id": source.tenant_id,
                    "owner_user_id": source.owner_user_id,
                    "source_item_ids": report.source_item_ids,
                    "force": bool(payload.get("force_digest", False)),
                    "limit": len(report.source_item_ids),
                    "batch_size": max(1, min(len(report.source_item_ids), int(payload.get("digest_batch_size") or 1))),
                    "priority": int(payload.get("digest_priority") or 5),
                    "reason": f"{action} source {digest_mode}",
                    "triggered_by": _actor_user_id(context, payload),
                },
                context=context,
            )
        chunks = self.store.list_chunks_for_sources(set(report.source_item_ids))
        documents = self.store.list_documents_for_sources(set(report.source_item_ids))
        return {
            "ok": True,
            "action": action,
            "tenant_id": source.tenant_id,
            "owner_user_id": source.owner_user_id,
            "knowledge_source": to_jsonable(source),
            "knowledge_base_ids": [knowledge_base.knowledge_base_id for knowledge_base in knowledge_bases],
            "source_item_ids": report.source_item_ids,
            "documents": to_jsonable(documents),
            "chunk_stats": _chunk_stats(chunks),
            "preview": preview,
            "sync_run": to_jsonable(run),
            "sync_report": to_jsonable(report),
            "digest_mode": digest_mode,
            "digest": digest,
            "adapters": supported_source_adapters(),
        }

    def sync_knowledge_sources(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or (context.tenant_id if context else DEFAULT_TENANT_ID))
        owner_user_id = _owner_user_id_for_write(payload, context)
        source_ids = _string_list(payload.get("knowledge_source_ids") or payload.get("source_ids") or payload.get("knowledge_source_id"))
        source_type = _normal_source_type(payload.get("source_type")) if payload.get("source_type") else None
        source_types = {_normal_source_type(value) for value in _string_list(payload.get("source_types"))}
        source_service = KnowledgeSourceService(self.store)
        if source_ids:
            sources = [self.store.get_knowledge_source(source_id) for source_id in source_ids]
            sources = [source for source in sources if source.tenant_id == tenant_id and source.owner_user_id == owner_user_id]
        else:
            sources = source_service.list_sources(tenant_id=tenant_id, owner_user_id=owner_user_id)
        if source_type:
            sources = [source for source in sources if _normal_source_type(source.source_type) == source_type]
        if source_types:
            sources = [source for source in sources if _normal_source_type(source.source_type) in source_types]
        sources = [source for source in sources if source.mode != "paused" and source.status != "paused"]
        reports: list[Any] = []
        sync_runs = []
        failed: list[dict[str, Any]] = []
        embedding_provider = build_embedding_provider(EmbeddingConfig(provider="disabled"))
        limit = _optional_positive_int(payload.get("limit"))
        processing_overrides = payload.get("processing_config") if isinstance(payload.get("processing_config"), dict) else None
        for source in sources:
            try:
                adapter = build_source_adapter(self.store, source, embedding_provider=embedding_provider, processing_config=processing_overrides)
                report = adapter.sync(limit=limit)
                reports.append(report)
                failed.extend(getattr(report, "failed", []) or [])
                sync_runs.append(source_service.record_sync_report(source, report))
                knowledge_bases = _knowledge_bases_for_source_or_payload(
                    self.store,
                    payload,
                    source,
                    actor_user_id=_actor_user_id(context, payload),
                )
                _bind_source_to_knowledge_bases(
                    self.store,
                    source,
                    knowledge_bases=knowledge_bases,
                    source_item_ids=list(getattr(report, "source_item_ids", []) or []),
                    actor_user_id=_actor_user_id(context, payload),
                    membership_type="sync",
                )
            except Exception as exc:  # noqa: BLE001 - keep syncing other sources.
                error = f"{type(exc).__name__}: {exc}"
                failed.append({"knowledge_source_id": source.knowledge_source_id, "uri": source.uri, "error": error})
                sync_runs.append(source_service.record_sync_error(source, error))
        return to_jsonable(
            {
                "ok": not failed,
                "knowledge_sources": sources,
                "reports": reports,
                "sync_runs": sync_runs,
                "failed": failed,
                "totals": {
                    "sources": len(sources),
                    "scanned": sum(int(getattr(report, "scanned", 0) or 0) for report in reports),
                    "ingested": sum(int(getattr(report, "ingested", 0) or 0) for report in reports),
                    "new_files": sum(int(getattr(report, "new_files", 0) or 0) for report in reports),
                    "changed_files": sum(int(getattr(report, "changed_files", 0) or 0) for report in reports),
                    "unchanged_files": sum(int(getattr(report, "unchanged_files", 0) or 0) for report in reports),
                    "skipped": sum(len(getattr(report, "skipped", []) or []) for report in reports),
                    "failed": len(failed),
                },
            }
        )

    def files_sync(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        source_service = KnowledgeSourceService(self.store)
        tenant_id = str(payload.get("tenant_id") or self.config.files.tenant_id or DEFAULT_TENANT_ID)
        owner_user_id = (
            _owner_user_id_for_write(payload, context)
            if context or payload.get("owner_user_id")
            else str(self.config.files.owner_user_id)
        )
        requested_roots = [Path(str(root)).expanduser().resolve() for root in _string_list(payload.get("roots") or payload.get("root"))]
        ignore = _string_list(payload.get("ignore"))
        requested_max_bytes = _optional_positive_int(payload.get("max_bytes"))
        requested_spreadsheet_rows = _optional_positive_int(
            payload.get("spreadsheet_max_rows_per_sheet") or payload.get("spreadsheet_row_limit_per_sheet")
        )
        requested_spreadsheet_columns = _optional_positive_int(
            payload.get("spreadsheet_max_columns") or payload.get("spreadsheet_column_limit")
        )
        max_bytes = requested_max_bytes or self.config.files.max_bytes
        spreadsheet_max_rows_per_sheet = requested_spreadsheet_rows or self.config.files.spreadsheet_max_rows_per_sheet
        spreadsheet_max_columns = requested_spreadsheet_columns or self.config.files.spreadsheet_max_columns
        try:
            seeded = source_service.seed_from_config(self.config)
            configured_roots = [root.expanduser().resolve() for root in self.config.files.roots]
            for root in requested_roots:
                seeded.append(
                    source_service.add_folder_source(
                        root,
                        owner_user_id=owner_user_id,
                        tenant_id=tenant_id,
                        space_id=str(payload.get("space_id") or self.config.files.space_id),
                        visibility=Visibility(str(payload.get("visibility") or self.config.files.visibility)),
                        ignore=[*self.config.files.ignore, *ignore],
                        max_bytes=max_bytes,
                        spreadsheet_max_rows_per_sheet=spreadsheet_max_rows_per_sheet,
                        spreadsheet_max_columns=spreadsheet_max_columns,
                        document_parser=self.config.document_parser,
                    )
                )
            active_uris = {root.as_uri() for root in [*configured_roots, *requested_roots]}
            sources = [
                source
                for source in source_service.list_sources(tenant_id=tenant_id, owner_user_id=owner_user_id, source_type="folder")
                if source.mode != "paused" and source.status != "paused" and source.uri in active_uris
            ]
        except Exception as exc:  # noqa: BLE001 - report local setup failures to the UI.
            if requested_roots or self.config.files.roots:
                raise
            return {
                "ok": False,
                "error": "当前账号还没有可同步的本地文件夹资料源。普通用户请在资料库上传文件、粘贴文本或添加 URL/RSS；文件夹同步仅用于管理员、本地开发或迁移场景。",
                "database_error": f"{type(exc).__name__}: {exc}",
                "reports": [],
                "knowledge_sources": [],
            }
        if not sources:
            return {
                "ok": False,
                "error": "当前账号还没有可同步的本地文件夹资料源。普通用户请在资料库上传文件、粘贴文本或添加 URL/RSS；文件夹同步仅用于管理员、本地开发或迁移场景。",
                "reports": [],
                "knowledge_sources": [],
            }

        reports = []
        sync_runs = []
        failed = []
        embedding_provider = build_embedding_provider(EmbeddingConfig(provider="disabled"))
        processing_overrides = payload.get("processing_config") or payload.get("process_config")
        for source in sources:
            root = source_service.source_path(source)
            try:
                processing_config = resolve_processing_config(source.config, processing_overrides)
                source_max_bytes = requested_max_bytes or int(source.config.get("max_bytes") or max_bytes)
                source_spreadsheet_rows = requested_spreadsheet_rows or int(
                    source.config.get("spreadsheet_max_rows_per_sheet")
                    or source.config.get("spreadsheet_row_limit_per_sheet")
                    or spreadsheet_max_rows_per_sheet
                )
                source_spreadsheet_columns = requested_spreadsheet_columns or int(
                    source.config.get("spreadsheet_max_columns")
                    or source.config.get("spreadsheet_column_limit")
                    or spreadsheet_max_columns
                )
                report = scan_files(
                    self.store,
                    root=root,
                    owner_user_id=source.owner_user_id,
                    tenant_id=source.tenant_id,
                    space_id=source.space_id,
                    visibility=source.visibility,
                    visible_team_ids=source.visible_team_ids,
                    ignore=[*list(source.config.get("ignore") or []), *ignore],
                    max_bytes=source_max_bytes,
                    spreadsheet_max_rows_per_sheet=source_spreadsheet_rows,
                    spreadsheet_max_columns=source_spreadsheet_columns,
                    document_parser=self.config.document_parser,
                    embedding_provider=embedding_provider,
                    processing_config=processing_config,
                )
                reports.append(report)
                failed.extend(report.failed)
                sync_runs.append(source_service.record_sync_report(source, report))
                knowledge_bases = _knowledge_bases_for_source_or_payload(
                    self.store,
                    payload,
                    source,
                    actor_user_id=_actor_user_id(context, payload),
                )
                _bind_source_to_knowledge_bases(
                    self.store,
                    source,
                    knowledge_bases=knowledge_bases,
                    source_item_ids=list(getattr(report, "source_item_ids", []) or []),
                    actor_user_id=_actor_user_id(context, payload),
                    membership_type="files_sync",
                )
            except Exception as exc:  # noqa: BLE001 - report all roots together.
                error = f"{type(exc).__name__}: {exc}"
                failed.append({"root": str(root), "knowledge_source_id": source.knowledge_source_id, "error": error})
                sync_runs.append(source_service.record_sync_error(source, error))

        twitter_archives = _files_sync_twitter_archives(self.store, self.config, payload)
        failed.extend(twitter_archives.get("failed") or [])
        return to_jsonable(
            {
                "ok": not failed,
                "database_url": getattr(self.store, "database_url", None),
                "seeded_knowledge_sources": seeded,
                "knowledge_sources": sources,
                "roots": [str(source_service.source_path(source).expanduser()) for source in sources],
                "reports": reports,
                "sync_runs": sync_runs,
                "twitter_archives": twitter_archives,
                "totals": {
                    "roots": len(sources),
                    "scanned": sum(report.scanned for report in reports),
                    "ingested": sum(report.ingested for report in reports),
                    "twitter_zip_count": int(twitter_archives.get("zip_count") or 0),
                    "twitter_imported": int(twitter_archives.get("imported") or 0),
                    "twitter_skipped": int(twitter_archives.get("skipped") or 0),
                    "new_files": sum(report.new_files for report in reports),
                    "changed_files": sum(report.changed_files for report in reports),
                    "unchanged_files": sum(report.unchanged_files for report in reports),
                    "moved_files": sum(report.moved_files for report in reports),
                    "missing_files": sum(report.missing_files for report in reports),
                    "skipped": sum(len(report.skipped) for report in reports),
                    "failed": len(failed),
                },
                "failed": failed,
            }
        )

    def digest_now(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        from pska_core.cli import (
            _digest_now_candidate_summary,
            _digest_now_diagnostics,
            _digest_now_diagnostics_with_persisted_candidates,
            _digest_now_fallback_review,
            _digest_schedule_payload,
            _review_items_payload,
            _run_fastreact_digest_worker,
        )

        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _owner_user_id_for_write(payload, context)
        args = argparse.Namespace(
            database_url=getattr(self.store, "database_url", self.config.database.url),
            tenant_id=tenant_id,
            config=None,
            pska_config=self.config,
            owner_user_id=owner_user_id,
            source_item_ids=_string_list(payload.get("source_item_ids")),
            limit=_batch_limit(payload.get("limit") or 20),
            batch_size=_batch_limit(payload.get("batch_size") or payload.get("limit") or 1),
            priority=int(payload.get("priority") or 0),
            max_attempts=int(payload.get("max_attempts") or 3),
            retry_backoff_seconds=int(payload.get("retry_backoff_seconds") or payload.get("backoff_seconds") or 60),
            quota_window_seconds=0,
            max_jobs_per_window=0,
            force=bool(payload.get("force", False)),
            reason=str(payload.get("reason") or "manual frontend digest-now"),
            skip_sync=bool(payload.get("skip_sync", False)),
            root=[Path(str(root)) for root in _string_list(payload.get("roots") or payload.get("root"))],
            space_id=payload.get("space_id"),
            visibility=payload.get("visibility"),
            ignore=_string_list(payload.get("ignore")),
            max_bytes=_optional_positive_int(payload.get("max_bytes")),
            twitter_archive=Path(str(payload["twitter_archive"])) if payload.get("twitter_archive") else None,
            archive_root=Path(str(payload["archive_root"])) if payload.get("archive_root") else None,
            skip_twitter_archives=bool(payload.get("skip_twitter_archives", False)),
            fastreact_root=Path.home() / "Fastreact" / "fastreact-nano",
            python="python3",
            pska_url=None,
            fastreact_url=None,
            max_worker_runs=max(0, min(int(payload.get("max_worker_runs") if payload.get("max_worker_runs") is not None else 1), 10)),
            embedding_provider="disabled",
            embedding_model=None,
            embedding_dimensions=None,
        )
        sync_payload = None
        if not args.skip_sync:
            sync_payload = self.files_sync(payload, context=context)
            if not sync_payload.get("ok"):
                return {"ok": False, "stage": "files_sync", "sync": sync_payload}

        scheduled = self.schedule_digest(_digest_schedule_payload(args), context=context)
        scheduled_job = scheduled.get("job") if isinstance(scheduled.get("job"), dict) else None
        args.job_id = str(scheduled_job.get("job_id")) if scheduled_job and scheduled_job.get("job_id") else None
        worker_runs = _run_fastreact_digest_worker(args, self.config) if args.max_worker_runs > 0 else []
        diagnostics = _digest_now_diagnostics(worker_runs)
        candidate_summary = _digest_now_candidate_summary(
            worker_runs,
            store=self.store,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
        )
        diagnostics = _digest_now_diagnostics_with_persisted_candidates(diagnostics, candidate_summary)
        fallback_review = _digest_now_fallback_review(
            self.store,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            scheduled_source_item_ids=scheduled.get("scheduled_source_item_ids") or [],
            diagnostics=diagnostics,
            worker_runs=worker_runs,
        )
        stats = self.job_stats(tenant_id=tenant_id)["stats"]
        discoveries = self.workspace_discoveries(owner_user_id=owner_user_id, limit=50, context=context)
        all_new_discoveries = self.workspace_discoveries(owner_user_id=owner_user_id, limit=50, min_score=0, context=context)
        pending_reviews = _review_items_payload(
            self.store.list_review_items(tenant_id=tenant_id),
            status="pending",
            owner_user_id=owner_user_id,
            limit=50,
            summary=True,
        )
        failed_digest_jobs = [
            to_jsonable(job)
            for job in self.store.list_jobs(tenant_id=tenant_id, status="failed", job_type=DIGEST_VIA_FASTREACT, limit=10)
        ]
        candidate_summary["review_items"] += int(fallback_review.get("review_items") or 0)
        result = {
            "ok": not any(run.get("ok") is False for run in worker_runs) and not failed_digest_jobs,
            "sync": sync_payload,
            "digest": scheduled,
            "worker_runs": worker_runs,
            "summary": {
                "synced": None if sync_payload is None else sync_payload.get("totals"),
                "scheduled_source_items": len(scheduled.get("scheduled_source_item_ids") or []),
                "worker_processed": sum(int(run.get("processed") or 0) for run in worker_runs if isinstance(run, dict)),
                "candidate_write": candidate_summary,
                "diagnostics": diagnostics,
                "fallback_review": fallback_review,
                "discoveries_visible_count": int(discoveries.get("count") or 0),
                "discoveries_total_new": int(all_new_discoveries.get("total_new") or 0),
                "discoveries_min_score": discoveries.get("min_score"),
                "low_score_discovery_count": max(0, int(all_new_discoveries.get("total_new") or 0) - int(discoveries.get("count") or 0)),
                "digest_backlog": stats.get("digest_backlog") or {},
                "pending_review_count": int(pending_reviews.get("count") or 0),
                "failed_digest_jobs": len(failed_digest_jobs),
            },
            "discoveries": discoveries,
            "low_score_discoveries": all_new_discoveries,
            "pending_reviews": pending_reviews,
            "failed_digest_jobs": failed_digest_jobs,
        }
        return to_jsonable(result)

    def job_status(
        self,
        job_id: str | None = None,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if job_id:
            job = self.store.get_job(job_id)
            if tenant_id and job.tenant_id != tenant_id:
                raise PermissionError("job tenant mismatch")
            return {
                "job": to_jsonable(job),
                "events": to_jsonable(self.store.list_job_events(job_id)),
            }
        return {"jobs": to_jsonable(self.store.list_jobs(tenant_id=tenant_id, status=status, job_type=job_type, limit=limit))}

    def digest_logs(
        self,
        *,
        owner_user_id: str = "user_primary",
        tenant_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        limit = max(1, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        scope = _knowledge_base_scope_for_ids(self.store, knowledge_base_ids or [], tenant_id=tenant_id, owner_user_id=owner_user_id)
        scoped_source_item_ids = set(_string_list(scope.get("source_item_ids")))
        has_kb_scope = bool(_knowledge_base_ids_from_scope(scope))
        jobs = [
            job
            for job in self.store.list_jobs(
                tenant_id=tenant_id,
                job_type=DIGEST_VIA_FASTREACT,
                limit=10_000 if has_kb_scope else max(limit * 3, limit),
            )
            if str(job.payload.get("owner_user_id") or owner_user_id) == owner_user_id
            and (not has_kb_scope or bool(_job_source_item_ids(job) & scoped_source_item_ids))
        ][:limit]
        entries = []
        selected_knowledge_base_ids = _knowledge_base_ids_from_scope(scope)
        for job in jobs:
            source_ids = _job_source_item_ids(job)
            events = self.store.list_job_events(job.job_id)
            scoped_job_source_ids = source_ids & scoped_source_item_ids if has_kb_scope else source_ids
            claims = self.store.list_knowledge_claims(
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                source_item_ids=scoped_job_source_ids or None,
                job_id=job.job_id,
                limit=20,
            )
            notes = self.store.list_digest_notes(
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                source_item_ids=scoped_job_source_ids or None,
                job_id=job.job_id,
                limit=10,
            )
            claim_payloads = _enrich_understanding_artifacts_knowledge_bases(
                self.store,
                to_jsonable(claims),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                selected_knowledge_base_ids=selected_knowledge_base_ids,
            )
            note_payloads = _enrich_understanding_artifacts_knowledge_bases(
                self.store,
                to_jsonable(notes),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                selected_knowledge_base_ids=selected_knowledge_base_ids,
            )
            source_refs = _enrich_source_refs_knowledge_bases(
                self.store,
                [{"source_item_id": source_id} for source_id in sorted(source_ids)],
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                selected_knowledge_base_ids=selected_knowledge_base_ids,
            )
            entries.append(_digest_log_entry(job, events, claim_payloads, note_payloads, source_ids, source_refs=source_refs))
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "scope_applied": _knowledge_scope_applied(scope),
            "summary": _digest_logs_summary(entries),
            "logs": to_jsonable(entries),
            "count": len(entries),
        }

    def workspace_digest_data(
        self,
        *,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 50,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        tenant_id = tenant_id or (context.tenant_id if context else DEFAULT_TENANT_ID)
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        limit = max(1, min(limit, 100))
        scope = _knowledge_base_scope_for_ids(self.store, knowledge_base_ids or [], tenant_id=tenant_id, owner_user_id=owner_user_id)
        scoped_source_item_ids = set(_string_list(scope.get("source_item_ids")))
        has_kb_scope = bool(_knowledge_base_ids_from_scope(scope))
        notes = self.store.list_digest_notes(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            source_item_ids=scoped_source_item_ids if has_kb_scope else None,
            limit=limit,
        )
        claims = self.store.list_knowledge_claims(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            source_item_ids=scoped_source_item_ids if has_kb_scope else None,
            limit=limit,
        )
        review_items = [
            item
            for item in self.store.list_review_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id
            and (
                not has_kb_scope
                or _review_item_matches_source_item_ids(item, scoped_source_item_ids)
            )
        ]
        review_items = sorted(review_items, key=lambda item: item.created_at, reverse=True)[:limit]
        discoveries = self.store.list_discovery_items(owner_user_id=owner_user_id, tenant_id=tenant_id, limit=limit)
        stats = self.job_stats(tenant_id=tenant_id)["stats"]
        selected_knowledge_base_ids = _knowledge_base_ids_from_scope(scope)
        note_payloads = _enrich_understanding_artifacts_knowledge_bases(
            self.store,
            to_jsonable(notes),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=selected_knowledge_base_ids,
        )
        claim_payloads = _enrich_understanding_artifacts_knowledge_bases(
            self.store,
            to_jsonable(claims),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=selected_knowledge_base_ids,
        )
        review_payloads = _enrich_review_candidate_payloads_knowledge_bases(
            self.store,
            to_jsonable(review_items),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=selected_knowledge_base_ids,
        )
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "scope_applied": _knowledge_scope_applied(scope),
            "digest_notes": note_payloads,
            "knowledge_claims": claim_payloads,
            "review_candidates": review_payloads,
            "discovery_summaries": to_jsonable(discoveries),
            "summary": {
                "digest_notes": len(notes),
                "knowledge_claims": len(claims),
                "pending_review_candidates": len([item for item in review_items if item.status == "pending"]),
                "discoveries": len(discoveries),
                "digest_backlog": stats.get("digest_backlog") or {},
            },
        }

    def workspace_digest_run(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or (context.tenant_id if context else DEFAULT_TENANT_ID))
        owner_user_id = _owner_user_id_for_write(payload, context)
        source_item_ids = _string_list(payload.get("source_item_ids"))
        knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
        knowledge_source_ids = _string_list(payload.get("knowledge_source_ids") or payload.get("sources"))
        if not source_item_ids and knowledge_source_ids:
            source_item_ids = _source_item_ids_for_knowledge_sources(self.store, tenant_id=tenant_id, knowledge_source_ids=knowledge_source_ids)
        scope = _resolve_knowledge_base_scope(
            self.store,
            _scope_from_payload({**payload, "source_item_ids": source_item_ids}),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        source_item_ids = _string_list(scope.get("source_item_ids"))
        scheduled = self.schedule_digest(
            {
                **payload,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "source_item_ids": source_item_ids,
                "scope": scope,
                "reason": payload.get("reason") or "workspace digest run",
            },
            context=context,
        )
        worker_runs: list[dict[str, Any]] = []
        worker_status: dict[str, Any] = {
            "requested": False,
            "ok": True,
            "processed": 0,
            "failed_runs": 0,
            "diagnostics": ["queued_for_background_worker"],
        }
        if _truthy(payload.get("run_worker"), default=False):
            worker_runs = _workspace_digest_worker_runs(
                self,
                payload,
                scheduled=scheduled,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            worker_status = _workspace_digest_worker_status(worker_runs)
        data = self.workspace_digest_data(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            knowledge_base_ids=_knowledge_base_ids_from_scope(scope),
            context=context,
        )
        return {
            "ok": bool(worker_status.get("ok", True)),
            "scheduled": scheduled,
            "scope_applied": _knowledge_scope_applied(scope),
            "mode": "sync_worker" if worker_status.get("requested") else "queued",
            "queued": not bool(worker_status.get("requested")),
            "job": scheduled.get("job"),
            "worker_runs": worker_runs,
            "worker_status": worker_status,
            "data": data,
            "summary": {
                "scheduled_source_items": len(scheduled.get("scheduled_source_item_ids") or []),
                "queued_jobs": 1 if scheduled.get("job") else 0,
                "skipped_source_items": len(scheduled.get("skipped_source_item_ids") or []),
                "worker_processed": int(worker_status.get("processed") or 0),
                "worker_diagnostics": worker_status.get("diagnostics") or [],
                **dict(data.get("summary") or {}),
            },
        }

    def metrics(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        source_items = self.store.list_source_items(tenant_id=tenant_id)
        chunks = self.store.list_chunks_for_sources({item.source_item_id for item in source_items})
        return {
            "index": self.index_status(tenant_id=tenant_id),
            "tenant_id": tenant_id,
            "offline_index": OfflineIndexService(self.store).freshness(tenant_id=tenant_id),
            "embedding": _embedding_metrics(chunks, _api_config(self)),
            "connectors": _connector_metrics(source_items, self.store.list_connector_states(tenant_id=tenant_id)),
            "jobs": self.job_stats(tenant_id=tenant_id)["stats"],
        }

    def console_dashboard(self, *, owner_user_id: str = "user_primary", tenant_id: str | None = None, limit: int = 5) -> dict[str, Any]:
        limit = max(0, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        try:
            ready = self.ready()
        except Exception as exc:  # noqa: BLE001 - console should explain local service failures.
            ready = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "checks": {}}
        try:
            metrics = self.metrics(tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            metrics = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "index": {}, "connectors": {}}
        try:
            stats = self.job_stats(tenant_id=tenant_id)["stats"]
        except Exception as exc:  # noqa: BLE001
            stats = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "by_status": {}, "digest_backlog": {}}
        try:
            reviews = self.review_items(tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            reviews = {"error": f"{type(exc).__name__}: {exc}", "items": []}

        checks = ready.get("checks") or {}
        pending_reviews = _console_review_items(
            reviews.get("review_items", []),
            status="pending",
            owner_user_id=owner_user_id,
            limit=limit,
        )
        failed_jobs = [job for job in stats.get("recent_failed", []) if isinstance(job, dict)][:limit]
        pending_count = len(pending_reviews)
        failed_count = int((stats.get("by_status") or {}).get("failed") or len(failed_jobs))
        digest_jobs = int((stats.get("digest_backlog") or {}).get("jobs") or 0)
        return {
            "ok": bool(ready.get("ok")),
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "requires_agentic_service_online": False,
            "service_readiness": {
                "database_ok": bool((checks.get("database") or {}).get("ok")),
                "schema_ok": bool((checks.get("schema") or {}).get("ok")),
                "mcp_ok": bool((checks.get("mcp") or {}).get("ok")),
                "jobs_ok": bool((checks.get("jobs") or {}).get("ok")),
                "metrics_ok": bool((checks.get("metrics") or {}).get("ok")),
                "agentic_service_ok": bool((checks.get("agentic_service") or {}).get("ok")),
                "agentic_service_provider": (checks.get("agentic_service") or {}).get("provider"),
                "agentic_service_adapter": (checks.get("agentic_service") or {}).get("adapter"),
                "agentic_service_optional_for_console": True,
            },
            "source_counts": {
                "source_items": int((metrics.get("index") or {}).get("source_items") or 0),
                "chunks": int((metrics.get("index") or {}).get("chunks") or 0),
            },
            "digest_backlog": stats.get("digest_backlog") or {},
            "pending_reviews": {"total_matching": pending_count, "recent": pending_reviews[:limit]},
            "failed_jobs": {"count": failed_count, "recent": failed_jobs},
            "source_summary": {
                "recent_sources": _console_recent_sources(self.store.list_source_items(tenant_id=tenant_id), owner_user_id=owner_user_id, limit=limit),
                "connector_state": metrics.get("connectors") or {},
            },
            "recommended_commands": _console_recommended_commands(
                pending_review_count=pending_count,
                failed_job_count=failed_count,
                digest_backlog_count=digest_jobs,
            ),
            "deterministic_next_actions": _console_next_actions(
                pending_review_count=pending_count,
                failed_job_count=failed_count,
                digest_backlog_count=digest_jobs,
            ),
        }

    def job_stats(self, *, tenant_id: str | None = None, limit: int = 1000) -> dict[str, Any]:
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        jobs = self.store.list_jobs(tenant_id=tenant_id, limit=limit)
        by_status = {status: 0 for status in ["queued", "running", "failed", "succeeded", "canceled"]}
        by_type: dict[str, int] = {}
        worker_ids: set[str] = set()
        stale_running: list[dict[str, Any]] = []
        digest_backlog_jobs = 0
        digest_backlog_source_items: set[str] = set()
        now = datetime.now(UTC)
        for job in jobs:
            by_status[job.status] = by_status.get(job.status, 0) + 1
            by_type[job.job_type] = by_type.get(job.job_type, 0) + 1
            if job.worker_id:
                worker_ids.add(job.worker_id)
            if job.job_type == DIGEST_VIA_FASTREACT and job.status in {"queued", "running"}:
                digest_backlog_jobs += 1
                digest_backlog_source_items.update(_job_source_item_ids(job))
            if job.status == "running" and job.leased_until and _as_aware(job.leased_until) < now:
                stale_running.append(
                    {
                        "job_id": job.job_id,
                        "job_type": job.job_type,
                        "worker_id": job.worker_id,
                        "leased_until": job.leased_until,
                        "external_run_id": job.external_run_id,
                    }
                )
        recent_failed = [
            {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "error": job.error,
                "updated_at": job.updated_at,
                "external_run_id": job.external_run_id,
            }
            for job in jobs
            if job.status == "failed"
        ][:5]
        return {
            "stats": {
                "sample_size": len(jobs),
                "tenant_id": tenant_id,
                "by_status": by_status,
                "by_type": by_type,
                "active_worker_ids": sorted(worker_ids),
                "running_stale_count": len(stale_running),
                "stale_running": stale_running[:10],
                "recent_failed": recent_failed,
                "digest_backlog": {
                    "jobs": digest_backlog_jobs,
                    "source_items": len(digest_backlog_source_items),
                },
            }
        }

    def console_reviews(
        self,
        *,
        status: str = "pending",
        owner_user_id: str = "user_primary",
        tenant_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(0, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        scope = _knowledge_base_scope_for_ids(self.store, knowledge_base_ids or [], tenant_id=tenant_id, owner_user_id=owner_user_id)
        scoped_source_item_ids = set(_string_list(scope.get("source_item_ids")))
        has_kb_scope = bool(_knowledge_base_ids_from_scope(scope))
        all_items = _console_review_items(
            to_jsonable(self.store.list_review_items(tenant_id=tenant_id)),
            status=status,
            owner_user_id=owner_user_id,
            limit=10_000,
            store=self.store,
        )
        all_items = sorted(all_items, key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if has_kb_scope:
            all_items = [
                item
                for item in all_items
                if _source_refs_match_source_item_ids(_list_of_dicts(item.get("source_refs")), scoped_source_item_ids)
            ]
        all_items = _enrich_review_items_knowledge_bases(
            self.store,
            all_items,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=_knowledge_base_ids_from_scope(scope),
        )
        items = all_items[:limit]
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "status": status,
            "review_items": items,
            "count": len(items),
            "total_matching": len(all_items),
            "scope_applied": _knowledge_scope_applied(scope),
            "supports_single_item_actions": True,
        }

    def console_search(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        user_id = str(payload.get("user_id") or "user_primary")
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        represented_user_id = payload.get("represented_user_id")
        mode = str(payload.get("mode") or "direct")
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        owner_user_id = str(represented_user_id or user_id)
        scope = _resolve_knowledge_base_scope(
            self.store,
            _scope_from_payload(payload),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        scoped_source_item_ids = _ask_scope_source_item_ids(scope)
        scope_mode = _ask_scope_mode(scope, ask_intent="kb_search")
        if mode == "agentic":
            try:
                result = self._agentic_service_search(
                    query,
                    user,
                    represented_user_id=represented_user_id,
                    max_iterations=int(payload.get("max_iterations") or 3),
                )
            except AgenticServiceError as exc:
                fallback = self.retrieval.search(
                    query,
                    user,
                    represented_user_id=represented_user_id,
                    top_k=int(payload.get("top_k") or 5),
                    source_item_ids=_retrieval_source_item_ids_arg(scoped_source_item_ids, scope_mode=scope_mode),
                    scope_mode=scope_mode,
                )
                fallback_retrieval = _console_search_summary(to_jsonable(fallback))
                return {
                    "ok": False,
                    "mode": "agentic",
                    "display_mode": "direct_fallback",
                    "requires_agentic_service_online": True,
                    "query": query,
                    "answer": _direct_retrieval_fallback_answer(query, fallback_retrieval),
                    "retrieval": fallback_retrieval,
                    "scope_applied": _ask_scope_applied(scope, ask_intent="kb_search"),
                    "citations": fallback_retrieval.get("citations") or [],
                    "source_refs": fallback_retrieval.get("citations") or [],
                    "fallback_reason": "agentic_service_unavailable",
                    "error": {
                        "type": "agentic_service_unavailable",
                        "message": "Agentic service is unavailable. Direct retrieval fallback is shown.",
                        "detail": str(exc),
                    },
                    "fallback": {
                        "mode": "direct",
                        "display_mode": "direct_fallback",
                        "retrieval": fallback_retrieval,
                    },
                }
            retrieval_payload = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
            result["retrieval"] = _console_search_summary(retrieval_payload)
            if payload.get("capture"):
                captured = capture_agent_conversation(
                    self.store,
                    owner_user_id=str(represented_user_id or user_id),
                    tenant_id=tenant_id,
                    represented_user_id=str(represented_user_id or user_id),
                    purpose="agentic_search",
                    prompt=query,
                    answer=str(result.get("answer") or ""),
                    source_refs=retrieval_payload.get("citations") or result.get("source_refs") or [],
                    trace_summary=compact_trace_for_context(result.get("trace") if isinstance(result.get("trace"), dict) else {}),
                    title=f"PSKA agentic search: {query[:80]}",
                    source_channel="pska_agent",
                )
                result["capture"] = {
                    "action": captured.action,
                    "explanation": captured.explanation,
                    "source_item_id": captured.source_item_id,
                    "review_item_id": captured.review_item_id,
                    "policy": captured.policy,
                }
            return result
        if mode != "direct":
            raise ValueError(f"unsupported console search mode: {mode}")
        response = self.retrieval.search(
            query,
            user,
            represented_user_id=represented_user_id,
            top_k=int(payload.get("top_k") or 5),
            source_item_ids=_retrieval_source_item_ids_arg(scoped_source_item_ids, scope_mode=scope_mode),
            scope_mode=scope_mode,
        )
        return {
            "ok": True,
            "mode": "direct",
            "requires_agentic_service_online": False,
            "query": query,
            "retrieval": _console_search_summary(to_jsonable(response)),
            "scope_applied": _ask_scope_applied(scope, ask_intent="kb_search"),
        }

    def workspace_search(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        result = self.console_search(payload, context=context)
        retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
        fallback = result.get("fallback") if isinstance(result.get("fallback"), dict) else {}
        fallback_retrieval = fallback.get("retrieval") if isinstance(fallback.get("retrieval"), dict) else {}
        active_retrieval = retrieval or fallback_retrieval
        diagnostics = active_retrieval.get("diagnostics") if isinstance(active_retrieval.get("diagnostics"), dict) else {}
        evidence = {
            "citations": active_retrieval.get("citations") or [],
            "source_refs": active_retrieval.get("citations") or [],
            "graph_paths": active_retrieval.get("graph_paths") or [],
            "memory_context": active_retrieval.get("memory_context") or [],
            "profile_context": active_retrieval.get("profile_context") or [],
            "gaps": diagnostics.get("gaps") or [],
            "conflicts": diagnostics.get("conflicts") or [],
        }
        return {
            **result,
            "workspace": {
                "surface": "user_workspace",
                "chat_status": _workspace_chat_status(result),
                "evidence": evidence,
                "writer_available": True,
                "corpus_available": True,
                "raw_json_hidden_by_default": True,
            },
        }

    def workspace_knowledge_base_search(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        tenant_id = str(payload.get("tenant_id") or (context.tenant_id if context else DEFAULT_TENANT_ID))
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
        if not knowledge_base_ids:
            raise ValueError("knowledge_base_ids is required")
        scope = _resolve_knowledge_base_scope(
            self.store,
            {**_scope_from_payload(payload), "knowledge_base_ids": knowledge_base_ids, "mode": str(payload.get("mode") or "hard")},
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        search_payload = {
            **payload,
            "query": query,
            "mode": "direct",
            "scope": scope,
            "knowledge_base_ids": _knowledge_base_ids_from_scope(scope),
            "top_k": max(1, min(int(payload.get("top_k") or 8), 50)),
            "capture": False,
        }
        result = self.workspace_search(search_payload, context=context)
        retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
        enriched_retrieval = _enrich_search_retrieval_knowledge_bases(
            self.store,
            retrieval,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=_knowledge_base_ids_from_scope(scope),
        )
        scope_applied = _ask_scope_applied(scope, ask_intent="kb_search")
        knowledge_bases = [
            _knowledge_base_payload(self.store, knowledge_base)
            for knowledge_base in (
                _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
                for knowledge_base_id in _knowledge_base_ids_from_scope(scope)
            )
        ]
        return {
            "ok": True,
            "query": query,
            "mode": "knowledge_base_search",
            "search_mode": str(payload.get("mode") or "hybrid"),
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base_ids": _knowledge_base_ids_from_scope(scope),
            "knowledge_bases": knowledge_bases,
            "scope_applied": scope_applied,
            "retrieval": enriched_retrieval,
            "results": enriched_retrieval.get("results") or [],
            "citations": enriched_retrieval.get("citations") or [],
            "source_refs": enriched_retrieval.get("citations") or [],
            "diagnostics": enriched_retrieval.get("diagnostics") or {},
            "workspace": {
                **dict(result.get("workspace") or {}),
                "surface": "knowledge_base_search",
                "evidence": {
                    **dict((result.get("workspace") or {}).get("evidence") or {}),
                    "citations": enriched_retrieval.get("citations") or [],
                    "source_refs": enriched_retrieval.get("citations") or [],
                },
            },
        }

    def workspace_ask_understand(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        raw_intent = str(payload.get("intent") or "auto").strip().lower()
        execution_intent, forced_ask_intent = _ask_requested_intents(raw_intent, payload.get("routing_mode"))
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        user_id = str(payload.get("user_id") or owner_user_id)
        represented_user_id = str(payload.get("represented_user_id") or owner_user_id)
        scope = _resolve_knowledge_base_scope(
            self.store,
            _scope_from_payload(payload),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        surface = str(payload.get("surface") or "ask").strip() or "ask"
        session_id = str(payload.get("session_id") or "").strip() or None
        skip_intent_classifier = _truthy(payload.get("skip_intent_classifier"))
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        understand = self._workspace_ask_understand_payload(
            query=query,
            raw_intent=raw_intent,
            execution_intent=execution_intent,
            forced_ask_intent=forced_ask_intent,
            skip_intent_classifier=skip_intent_classifier,
            scope=scope,
            surface=surface,
            user=user,
            represented_user_id=represented_user_id,
            session_id=session_id,
        )
        return {"ok": True, "understand": understand}

    def _workspace_ask_understand_payload(
        self,
        *,
        query: str,
        raw_intent: str,
        execution_intent: str,
        forced_ask_intent: str | None,
        skip_intent_classifier: bool,
        scope: dict[str, Any],
        surface: str,
        user: Any,
        represented_user_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        decision = _ask_direct_intent_decision(
            query=query,
            execution_intent=execution_intent,
            forced_ask_intent=forced_ask_intent,
            skip_intent_classifier=skip_intent_classifier,
            scope=scope,
        )
        if decision is None:
            decision = self._workspace_ask_agentic_intent_decision(
                query=query,
                raw_intent=raw_intent,
                forced_ask_intent=forced_ask_intent,
                scope=scope,
                surface=surface,
                user=user,
                represented_user_id=represented_user_id,
                session_id=session_id,
            )
        understand = _ask_understand_payload(
            query=query,
            intent=raw_intent,
            forced_ask_intent=forced_ask_intent,
            scope=scope,
            surface=surface,
            decision=decision,
        )
        ask_intent = str(understand.get("intent") or "kb_search")
        understand["execution_intent"] = execution_intent
        understand["selected_intent"] = _ask_route_intent(
            understand.get("rewrite_query") or query,
            intent=execution_intent,
            ask_intent=ask_intent,
            scope=scope,
            agentic_selected_intent=str(decision.get("selected_intent") or ""),
        )
        understand["intent_contract"] = _ask_intent_contract(
            ask_intent=ask_intent,
            selected_intent=str(understand.get("selected_intent") or "quick"),
            requested_intent=raw_intent,
            execution_intent=execution_intent,
            scope=scope,
            surface=surface,
            routing_owner=str(understand.get("routing_owner") or ""),
            reasons=_string_list(understand.get("reasons")),
        )
        return understand

    def _workspace_ask_agentic_intent_decision(
        self,
        *,
        query: str,
        raw_intent: str,
        forced_ask_intent: str | None,
        scope: dict[str, Any],
        surface: str,
        user: Any,
        represented_user_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        if not hasattr(self.agentic_service, "search"):
            return _ask_agentic_intent_fallback_decision(
                reason="agentic_intent_classifier_unavailable",
                forced_ask_intent=forced_ask_intent,
            )
        prompt = _ask_agentic_intent_prompt(
            query=query,
            raw_intent=raw_intent,
            forced_ask_intent=forced_ask_intent,
            scope=scope,
            surface=surface,
        )
        try:
            agentic = self._agentic_service_search(
                prompt,
                user,
                represented_user_id=represented_user_id,
                max_iterations=1,
                skills=[],
                tool_policy={"mode": "none"},
                session_id=session_id,
            )
        except Exception as exc:
            return _ask_agentic_intent_fallback_decision(
                reason=f"{type(exc).__name__}: {exc}",
                forced_ask_intent=forced_ask_intent,
            )
        parsed = _ask_json_object_from_text(str(agentic.get("answer") or ""))
        if not isinstance(parsed, dict):
            return _ask_agentic_intent_fallback_decision(
                reason="invalid_classifier_json",
                forced_ask_intent=forced_ask_intent,
                raw_answer=str(agentic.get("answer") or ""),
            )
        return _ask_normalize_agentic_intent_decision(
            parsed,
            forced_ask_intent=forced_ask_intent,
            agentic_service=agentic.get("agentic_service") if isinstance(agentic.get("agentic_service"), dict) else {},
        )

    def workspace_ask(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        started_at = time.perf_counter()
        payload = context.apply_to_payload(payload) if context else payload
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        intent = str(payload.get("intent") or "auto").strip().lower()
        execution_intent, forced_ask_intent = _ask_requested_intents(intent, payload.get("routing_mode"))
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        user_id = str(payload.get("user_id") or owner_user_id)
        represented_user_id = str(payload.get("represented_user_id") or owner_user_id)
        surface = str(payload.get("surface") or "ask").strip() or "ask"
        scope = _resolve_knowledge_base_scope(
            self.store,
            _scope_from_payload(payload),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        top_k = max(1, min(int(payload.get("top_k") or 8), 20))
        session_id = str(payload.get("session_id") or "").strip() or None
        skip_intent_classifier = _truthy(payload.get("skip_intent_classifier"))
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        understand = self._workspace_ask_understand_payload(
            query=query,
            raw_intent=intent,
            execution_intent=execution_intent,
            forced_ask_intent=forced_ask_intent,
            skip_intent_classifier=skip_intent_classifier,
            scope=scope,
            surface=surface,
            user=user,
            represented_user_id=represented_user_id,
            session_id=session_id,
        )
        ask_intent = str(understand.get("intent") or "kb_search")
        scope = _ask_scope_for_intent(scope, ask_intent=ask_intent)
        understand = {
            **understand,
            "scope_applied": _ask_scope_applied(scope, ask_intent=ask_intent),
        }
        selected_intent = str(understand.get("selected_intent") or "quick")
        if selected_intent == "deep":
            tool_policy = _ask_read_tool_policy(understand.get("scope_applied") if isinstance(understand.get("scope_applied"), dict) else {})
            try:
                deep_query = _ask_deep_query(query=query, surface=surface, scope={**scope, "understand": understand})
                deep = self._workspace_ask_deep_agentic(
                    deep_query,
                    user,
                    represented_user_id=represented_user_id,
                    max_iterations=max(1, min(int(payload.get("max_iterations") or 4), 8)),
                    skills=[PSKA_QA_SKILL],
                    tool_policy=tool_policy,
                    session_id=session_id,
                )
                return _ask_with_quality_signals(
                    _ask_deep_response(
                        query=query,
                        intent=intent,
                        surface=surface,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        selected_intent=selected_intent,
                        understand=understand,
                        agentic=deep,
                        started_at=started_at,
                        allowed_tools=ASK_READ_ONLY_TOOLS,
                        tool_policy=tool_policy,
                        store=self.store,
                    )
                )
            except AgenticServiceError as exc:
                quick = self._workspace_ask_quick(
                    query=query,
                    scope=scope,
                    intent=intent,
                    surface=surface,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    represented_user_id=represented_user_id,
                    user=user,
                    top_k=top_k,
                    started_at=started_at,
                    understand=understand,
                    session_id=session_id,
                )
                quick["ok"] = False
                quick["route"]["selected_intent"] = "quick"
                quick["route"]["fallback_from"] = "deep"
                quick["trace"]["fallback_reason"] = "agentic_service_unavailable"
                quick["trace"]["error"] = str(exc)
                return _ask_with_quality_signals(quick)
        return _ask_with_quality_signals(
            self._workspace_ask_quick(
                query=query,
                scope=scope,
                intent=intent,
                surface=surface,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                represented_user_id=represented_user_id,
                user=user,
                top_k=top_k,
                started_at=started_at,
                understand=understand,
                session_id=session_id,
            )
        )

    def _workspace_ask_deep_agentic(
        self,
        query: str,
        user: Any,
        *,
        represented_user_id: str | None,
        max_iterations: int,
        skills: list[str],
        tool_policy: dict[str, Any],
        session_id: str | None,
    ) -> dict[str, Any]:
        if hasattr(self.agentic_service, "search_event_stream"):
            raw_events: list[dict[str, Any]] = []
            event_stream = self.agentic_service.search_event_stream(
                query,
                user,
                represented_user_id=represented_user_id,
                max_iterations=max_iterations,
                skills=skills,
                tool_policy=tool_policy,
                session_id=session_id,
            )
            for raw_event in event_stream:
                if not isinstance(raw_event, dict):
                    continue
                if _ask_is_stream_done_event(raw_event):
                    continue
                raw_events.append(raw_event)
            return normalize_agentic_event_response(
                raw_events,
                provider=getattr(getattr(self.agentic_service, "config", None), "provider", "fastreact"),
                adapter="fastreact",
                url=getattr(getattr(self.agentic_service, "config", None), "url", ""),
                metadata={"event_count": len(raw_events), "collected_by": "workspace_ask"},
            )
        return self._agentic_service_search(
            query,
            user,
            represented_user_id=represented_user_id,
            max_iterations=max_iterations,
            skills=skills,
            tool_policy=tool_policy,
            session_id=session_id,
        )

    def workspace_ask_event_stream(self, payload: dict[str, Any], context: RequestContext | None = None):
        started_at = time.perf_counter()
        payload = context.apply_to_payload(payload) if context else payload
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        intent = str(payload.get("intent") or "auto").strip().lower()
        execution_intent, forced_ask_intent = _ask_requested_intents(intent, payload.get("routing_mode"))
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        user_id = str(payload.get("user_id") or owner_user_id)
        represented_user_id = str(payload.get("represented_user_id") or owner_user_id)
        surface = str(payload.get("surface") or "ask").strip() or "ask"
        scope = _resolve_knowledge_base_scope(
            self.store,
            _scope_from_payload(payload),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        top_k = max(1, min(int(payload.get("top_k") or 8), 20))
        session_id = str(payload.get("session_id") or "").strip() or None
        skip_intent_classifier = _truthy(payload.get("skip_intent_classifier"))
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        understand = self._workspace_ask_understand_payload(
            query=query,
            raw_intent=intent,
            execution_intent=execution_intent,
            forced_ask_intent=forced_ask_intent,
            skip_intent_classifier=skip_intent_classifier,
            scope=scope,
            surface=surface,
            user=user,
            represented_user_id=represented_user_id,
            session_id=session_id,
        )
        ask_intent = str(understand.get("intent") or "kb_search")
        scope = _ask_scope_for_intent(scope, ask_intent=ask_intent)
        understand = {
            **understand,
            "scope_applied": _ask_scope_applied(scope, ask_intent=ask_intent),
        }
        selected_intent = str(understand.get("selected_intent") or "quick")
        requires_retrieval = bool(understand.get("requires_retrieval", _ask_requires_retrieval(ask_intent)))
        if selected_intent != "deep" or not hasattr(self.agentic_service, "search_event_stream"):
            if selected_intent != "deep":
                route = _ask_route_payload(
                    intent=str(understand.get("intent") or "kb_search"),
                    selected_intent="quick",
                    retrieval_owner="pska" if requires_retrieval else "none",
                    surface=surface,
                    requires_agentic_service_online=False,
                    tool_policy={"mode": "none"},
                    query=query,
                    requested_intent=intent,
                    understand=understand,
                )
                yield ("route", {"route": route, "timing": {}})
                query_terms = _ask_query_terms(str(understand.get("rewrite_query") or query))
                emitted_steps = _ask_route_planner_steps(
                    query=query,
                    intent=str(understand.get("intent") or "kb_search"),
                    selected_intent="quick",
                    query_terms=query_terms,
                    started_at=started_at,
                    start_sequence=1,
                    include_understand=True,
                )
                if requires_retrieval:
                    emitted_steps.append(
                        _ask_quick_search_step(
                            sequence=len(emitted_steps) + 1,
                            query_terms=query_terms,
                            top_k=top_k,
                            started_at=started_at,
                        )
                    )
                time_to_first_agent_event_ms = emitted_steps[0].get("elapsed_ms") if emitted_steps else None
                for step in emitted_steps:
                    timing = {"time_to_first_agent_event_ms": time_to_first_agent_event_ms}
                    yield ("progress", {"progress": _ask_progress_from_step(step), "timing": timing})
                    yield ("agent_step", {"step": step, "timing": timing})
                final_payload = _ask_with_quality_signals(
                    self._workspace_ask_quick(
                        query=query,
                        scope=scope,
                        intent=intent,
                        surface=surface,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        represented_user_id=represented_user_id,
                        user=user,
                        top_k=top_k,
                        started_at=started_at,
                        agent_steps=emitted_steps,
                        query_terms=query_terms,
                        understand=understand,
                        session_id=session_id,
                    )
                )
                final_payload["timing"]["time_to_first_agent_event_ms"] = time_to_first_agent_event_ms
                for step in _list_of_dicts(final_payload.get("agent_steps"))[len(emitted_steps) :]:
                    timing = final_payload.get("timing") or {}
                    yield ("progress", {"progress": _ask_progress_from_step(step), "timing": timing})
                    yield ("agent_step", {"step": step, "timing": timing})
                for event_name, event_payload in _ask_sse_events(final_payload):
                    if event_name in {"route", "agent_step"}:
                        continue
                    if event_name == "progress" and ((event_payload.get("progress") or {}).get("step_id") != "evidence_check"):
                        continue
                    yield (event_name, event_payload)
                return
            final_payload = self.workspace_ask(payload, context=context)
            yield from _ask_sse_events(final_payload)
            return

        tool_policy = _ask_read_tool_policy(understand.get("scope_applied") if isinstance(understand.get("scope_applied"), dict) else {})
        route = {
            "intent": str(understand.get("intent") or "kb_search"),
            "requested_intent": intent,
            "selected_intent": selected_intent,
            "retrieval_owner": "fastreact_pska_mcp",
            "surface": surface,
            "requires_agentic_service_online": True,
            "tool_policy": tool_policy,
            "tool_profile": ASK_READ_TOOL_PROFILE,
            "routing_owner": "pska_planner",
            "query_terms": _ask_query_terms(str(understand.get("rewrite_query") or query)),
            "rewrite_query": understand.get("rewrite_query") or query,
            "scope_applied": understand.get("scope_applied") or {},
            "understand": understand,
            "intent_contract": understand.get("intent_contract") if isinstance(understand.get("intent_contract"), dict) else {},
        }
        yield ("route", {"route": route, "timing": {}})
        raw_events: list[dict[str, Any]] = []
        agent_steps: list[dict[str, Any]] = _ask_route_planner_steps(
            query=query,
            intent=str(understand.get("intent") or "kb_search"),
            selected_intent=selected_intent,
            query_terms=_ask_query_terms(str(understand.get("rewrite_query") or query)),
            started_at=started_at,
            start_sequence=1,
            include_understand=False,
        )
        time_to_first_agent_event_ms: float | None = None
        if agent_steps:
            time_to_first_agent_event_ms = agent_steps[0].get("elapsed_ms")
            for step in agent_steps:
                timing = {"time_to_first_agent_event_ms": time_to_first_agent_event_ms}
                yield ("progress", {"progress": _ask_progress_from_step(step), "timing": timing})
                yield ("agent_step", {"step": step, "timing": timing})
        try:
            event_stream = self.agentic_service.search_event_stream(
                _ask_deep_query(query=query, surface=surface, scope={**scope, "understand": understand}),
                user,
                represented_user_id=represented_user_id,
                max_iterations=max(1, min(int(payload.get("max_iterations") or 4), 8)),
                skills=[PSKA_QA_SKILL],
                tool_policy=tool_policy,
                session_id=session_id,
            )
            for raw_event in event_stream:
                if not isinstance(raw_event, dict):
                    continue
                if _ask_is_stream_done_event(raw_event):
                    continue
                raw_events.append(raw_event)
                step = _ask_agent_step_from_event(raw_event, sequence=len(agent_steps) + 1, started_at=started_at)
                if step:
                    if time_to_first_agent_event_ms is None:
                        time_to_first_agent_event_ms = _elapsed_ms(started_at)
                    agent_steps.append(step)
                    timing = {"time_to_first_agent_event_ms": time_to_first_agent_event_ms}
                    yield (
                        "progress",
                        {
                            "progress": _ask_progress_from_step(step),
                            "timing": timing,
                        },
                    )
                    yield (
                        "agent_step",
                        {
                            "step": step,
                            "timing": timing,
                        },
                    )
        except AgenticServiceError as exc:
            quick = self._workspace_ask_quick(
                query=query,
                scope=scope,
                intent=intent,
                surface=surface,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                represented_user_id=represented_user_id,
                user=user,
                top_k=top_k,
                started_at=started_at,
                understand=understand,
                session_id=session_id,
            )
            quick["ok"] = False
            quick["route"]["selected_intent"] = "quick"
            quick["route"]["fallback_from"] = "deep"
            quick["trace"]["fallback_reason"] = "agentic_service_unavailable"
            quick["trace"]["error"] = str(exc)
            quick["agent_steps"] = [
                *agent_steps,
                _ask_agent_step(
                    sequence=len(agent_steps) + 1,
                    phase="error",
                    status="error",
                    title="深入分析不可用",
                    detail="已切换到快速回答。",
                    started_at=started_at,
                ),
            ]
            quick["timing"]["time_to_first_agent_event_ms"] = time_to_first_agent_event_ms
            yield from _ask_sse_events(_ask_with_quality_signals(quick))
            return

        agentic = normalize_agentic_event_response(
            raw_events,
            provider=getattr(getattr(self.agentic_service, "config", None), "provider", "fastreact"),
            adapter="fastreact",
            url=getattr(getattr(self.agentic_service, "config", None), "url", ""),
            metadata={"event_count": len(raw_events)},
        )
        final_payload = _ask_deep_response(
            query=query,
            intent=intent,
            surface=surface,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_intent=selected_intent,
            understand=understand,
            agentic=agentic,
            started_at=started_at,
            allowed_tools=ASK_READ_ONLY_TOOLS,
            tool_policy=tool_policy,
            store=self.store,
        )
        final_payload["agent_steps"] = agent_steps or _ask_agent_steps_from_events(raw_events)
        final_payload["timing"]["time_to_first_agent_event_ms"] = time_to_first_agent_event_ms
        final_payload = _ask_with_quality_signals(final_payload)
        for event_name, event_payload in _ask_sse_events(final_payload):
            if event_name in {"route", "agent_step"}:
                continue
            yield (event_name, event_payload)

    def workspace_ask_conversations(self, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        conversations = self.store.list_ask_conversations(tenant_id=tenant_id, owner_user_id=owner_user_id, limit=int(payload.get("limit") or 50))
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "conversations": [_ask_conversation_payload(conversation) for conversation in conversations],
        }

    def create_workspace_ask_conversation(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        title = str(payload.get("title") or "Ask PSKA conversation").strip()
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = _ask_conversation_metadata_with_scope(
            metadata,
            _ask_scope_applied_from_payload(self.store, payload, tenant_id=tenant_id, owner_user_id=owner_user_id),
        )
        conversation = AskConversation(
            conversation_id=str(payload.get("conversation_id") or f"ask_{uuid4().hex}"),
            owner_user_id=owner_user_id,
            title=title,
            summary=str(payload.get("summary") or ""),
            metadata=metadata,
            tenant_id=tenant_id,
        )
        stored = self.store.create_ask_conversation(conversation)
        return {"ok": True, "conversation": _ask_conversation_payload(stored)}

    def workspace_ask_conversation(self, conversation_id: str, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        conversation = self.store.get_ask_conversation(conversation_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        messages = self.store.list_ask_messages(conversation_id, tenant_id=tenant_id, owner_user_id=owner_user_id, limit=int(payload.get("message_limit") or 100))
        runs = self.store.list_ask_runs(conversation_id, tenant_id=tenant_id, owner_user_id=owner_user_id, limit=int(payload.get("run_limit") or 50))
        return {
            "ok": True,
            "conversation": _ask_conversation_payload(conversation),
            "messages": [_ask_message_payload(message) for message in messages],
            "runs": [_ask_run_payload(run) for run in runs],
        }

    def delete_workspace_ask_conversation(self, conversation_id: str, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        archived = self.store.archive_ask_conversation(conversation_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        return {"ok": True, "deleted": {"conversation_id": conversation_id}, "conversation": _ask_conversation_payload(archived)}

    def workspace_ask_conversation_event_stream(self, conversation_id: str, payload: dict[str, Any], context: RequestContext | None = None):
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        query = str(payload.get("query") or payload.get("content") or "").strip()
        if not query:
            raise ValueError("query is required")
        try:
            conversation = self.store.get_ask_conversation(conversation_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        except KeyError:
            conversation = self.store.create_ask_conversation(
                AskConversation(
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                    title=str(payload.get("title") or _default_inline_title(query, fallback="Ask PSKA conversation")),
                    tenant_id=tenant_id,
                )
            )
        prompt_lineage = _prompt_profile_lineage(self.store, tenant_id=tenant_id, owner_user_id=owner_user_id, profile_type="ask")
        initial_scope_applied = _ask_scope_applied_from_payload(self.store, payload, tenant_id=tenant_id, owner_user_id=owner_user_id)
        if initial_scope_applied:
            conversation.metadata = _ask_conversation_metadata_with_scope(conversation.metadata, initial_scope_applied)
            conversation = self.store.create_ask_conversation(conversation)
        initial_route = {
            "intent": str(payload.get("intent") or "auto"),
            "requested_intent": str(payload.get("intent") or "auto"),
            "selected_intent": "pending",
            "surface": str(payload.get("surface") or "ask"),
            "scope_applied": initial_scope_applied,
            "routing_owner": "pska_planner",
        }
        run = self.store.add_ask_run(
            AskRun(
                run_id=str(payload.get("run_id") or f"askrun_{uuid4().hex}"),
                conversation_id=conversation.conversation_id,
                owner_user_id=owner_user_id,
                query=query,
                route=initial_route,
                result={"query": query, "status": "running", "scope_applied": initial_scope_applied, "route": initial_route},
                prompt_profile_id=prompt_lineage.get("prompt_profile_id"),
                prompt_profile_version=prompt_lineage.get("prompt_profile_version"),
                tenant_id=tenant_id,
            )
        )
        history = self.store.list_ask_messages(conversation.conversation_id, tenant_id=tenant_id, owner_user_id=owner_user_id, limit=12)
        self.store.add_ask_message(
            AskMessage(
                message_id=str(payload.get("message_id") or f"askmsg_{uuid4().hex}"),
                conversation_id=conversation.conversation_id,
                owner_user_id=owner_user_id,
                role="user",
                content=query,
                run_id=run.run_id,
                metadata={"prompt_profile": prompt_lineage, "ask_scope": initial_scope_applied, "knowledge_base_ids": initial_scope_applied.get("knowledge_base_ids") or []},
                tenant_id=tenant_id,
            )
        )
        ask_payload = {
            **payload,
            "query": query,
            "session_id": conversation.conversation_id,
            "surface": payload.get("surface") or "ask",
            "scope": {
                **(payload.get("scope") if isinstance(payload.get("scope"), dict) else {}),
                "conversation_id": conversation.conversation_id,
                "conversation_summary": conversation.summary,
                "recent_messages": [_ask_message_scope(message) for message in history[-8:]],
                "prompt_profile": prompt_lineage,
            },
        }
        result = _empty_ask_stream_result(query=query, conversation_id=conversation.conversation_id, run_id=run.run_id, prompt_lineage=prompt_lineage)
        finished = False
        persisted_answer_chars = 0
        try:
            yield ("conversation", {"conversation": _ask_conversation_payload(conversation), "run": _ask_run_payload(run)})
            for event_name, event_payload in self.workspace_ask_event_stream(ask_payload, context=context):
                _accumulate_ask_stream_result(result, event_name, event_payload)
                if event_name == "done":
                    result["status"] = "succeeded" if result.get("ok") is not False else "failed"
                    final_scope_applied = _ask_scope_applied_from_result(result) or initial_scope_applied
                    if final_scope_applied:
                        result["scope_applied"] = final_scope_applied
                        conversation.metadata = _ask_conversation_metadata_with_scope(conversation.metadata, final_scope_applied)
                        conversation = self.store.create_ask_conversation(conversation)
                    self.store.finish_ask_run(run.run_id, status="succeeded" if result.get("ok") is not False else "failed", result=result)
                    finished = True
                    self.store.add_ask_message(
                        AskMessage(
                            message_id=f"askmsg_{uuid4().hex}",
                            conversation_id=conversation.conversation_id,
                            owner_user_id=owner_user_id,
                            role="assistant",
                            content=str(result.get("answer") or ""),
                            run_id=run.run_id,
                            citations=_list_of_dicts(result.get("citations")),
                            source_refs=_list_of_dicts(result.get("source_refs")),
                            metadata={"quality_signals": result.get("quality_signals") or {}, "prompt_profile": prompt_lineage, "ask_scope": final_scope_applied, "knowledge_base_ids": final_scope_applied.get("knowledge_base_ids") or []},
                            tenant_id=tenant_id,
                        )
                    )
                    event_payload = {**dict(event_payload), "conversation_id": conversation.conversation_id, "run_id": run.run_id}
                elif event_name == "answer_delta":
                    answer_length = len(str(result.get("answer") or ""))
                    if answer_length - persisted_answer_chars >= 1000:
                        _safe_update_ask_run_progress(self.store, run.run_id, result=result)
                        persisted_answer_chars = answer_length
                else:
                    _safe_update_ask_run_progress(self.store, run.run_id, result=result)
                yield (event_name, event_payload)
        except Exception as exc:
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["status"] = "failed"
            self.store.finish_ask_run(run.run_id, status="failed", result=result)
            _add_ask_failure_message(
                self.store,
                conversation=conversation,
                run=run,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                result=result,
                prompt_lineage=prompt_lineage,
            )
            finished = True
            yield ("error", {"error": result["error"], "conversation_id": conversation.conversation_id, "run_id": run.run_id})
            return
        finally:
            if not finished:
                result["ok"] = False
                result["error"] = "stream_closed_before_done"
                result["status"] = "failed"
                try:
                    self.store.finish_ask_run(run.run_id, status="failed", result=result)
                    _add_ask_failure_message(
                        self.store,
                        conversation=conversation,
                        run=run,
                        owner_user_id=owner_user_id,
                        tenant_id=tenant_id,
                        result=result,
                        prompt_lineage=prompt_lineage,
                    )
                except Exception:
                    pass

    def _workspace_ask_quick(
        self,
        *,
        query: str,
        scope: dict[str, Any] | None = None,
        intent: str,
        surface: str,
        tenant_id: str,
        owner_user_id: str,
        represented_user_id: str,
        user: Any,
        top_k: int,
        started_at: float,
        agent_steps: list[dict[str, Any]] | None = None,
        query_terms: list[str] | None = None,
        understand: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        scope = _resolve_knowledge_base_scope(
            self.store,
            scope or {},
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        understand = understand or _ask_understand_payload(
            query=query,
            intent=intent,
            forced_ask_intent=None,
            scope=scope,
            surface=surface,
        )
        ask_intent = str(understand.get("intent") or "kb_search")
        scope = _ask_scope_for_intent(scope, ask_intent=ask_intent)
        rewrite_query = str(understand.get("rewrite_query") or query)
        query_terms = query_terms or _ask_query_terms(rewrite_query)
        requires_retrieval = bool(understand.get("requires_retrieval", _ask_requires_retrieval(ask_intent)))
        steps = list(agent_steps or [])
        if not steps:
            steps.extend(
                _ask_route_planner_steps(
                    query=query,
                    intent=ask_intent,
                    selected_intent="quick",
                    query_terms=query_terms,
                    started_at=started_at,
                    start_sequence=1,
                    include_understand=True,
                )
            )
            if requires_retrieval:
                steps.append(
                    _ask_quick_search_step(
                        sequence=len(steps) + 1,
                        query_terms=query_terms,
                        top_k=top_k,
                        started_at=started_at,
                    )
                )
        if not requires_retrieval:
            answer, answer_type = _ask_no_retrieval_answer(ask_intent, query)
            steps.append(
                _ask_agent_step(
                    sequence=len(steps) + 1,
                    phase="answer",
                    status="complete",
                    title="形成回答",
                    detail="无需检索用户资料，已直接回答。",
                    started_at=started_at,
                )
            )
            elapsed_ms = _elapsed_ms(started_at)
            evidence_check = {
                "schema": "pska.ask_evidence_check.v1",
                "status": "not_applicable",
                "scope_mode": _ask_scope_mode(scope, ask_intent=ask_intent),
                "used_citations": [],
                "dropped_citations": [],
                "evidence_claims": [],
                "no_answer_reasons": [],
            }
            return {
                "ok": True,
                "query": query,
                "intent": ask_intent,
                "rewrite_query": rewrite_query,
                "answer": answer,
                "answer_type": answer_type,
                "route": _ask_route_payload(
                    intent=ask_intent,
                    selected_intent="quick",
                    retrieval_owner="none",
                    surface=surface,
                    requires_agentic_service_online=False,
                    tool_policy={"mode": "none"},
                    query=query,
                    requested_intent=intent,
                    understand=understand,
                ),
                "evidence": {
                    "citations": [],
                    "source_refs": [],
                    "results": [],
                    "source_windows": [],
                    "graph_paths": [],
                    "memory_context": [],
                    "profile_context": [],
                    "gaps": [],
                    "conflicts": [],
                    "evidence_claims": [],
                    "no_answer_reasons": [],
                },
                "citations": [],
                "source_refs": [],
                "citation_audit": {"used": [], "dropped": []},
                "evidence_check": evidence_check,
                "evidence_claims": [],
                "no_answer_reasons": [],
                "agent_steps": steps,
                "trace": {
                    "mode": "quick",
                    "query_terms": query_terms,
                    "retrieval_query": None,
                    "scope": _ask_scope_trace(scope),
                    "retrieval_owner": "none",
                    "diagnostics": {"non_retrieval_intent": ask_intent},
                },
                "timing": {
                    "total_ms": elapsed_ms,
                    "time_to_first_answer_ms": elapsed_ms,
                    "time_to_first_agent_event_ms": steps[0].get("elapsed_ms") if steps else None,
                },
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
            }
        scoped_source_item_ids = _ask_scope_source_item_ids(scope)
        scope_mode = _ask_scope_mode(scope, ask_intent=ask_intent)
        retrieval_query = _ask_query_with_scope(rewrite_query, scope)
        retrieval_result = self.retrieval.search(
            retrieval_query,
            user,
            represented_user_id=represented_user_id,
            top_k=top_k,
            source_item_ids=_retrieval_source_item_ids_arg(scoped_source_item_ids, scope_mode=scope_mode),
            scope_mode=scope_mode,
        )
        retrieval = _console_search_summary(to_jsonable(retrieval_result))
        retrieval = _ask_hydrate_retrieval_source_windows(
            self.store,
            retrieval,
            query=query,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        retrieval = _ask_enrich_retrieval_knowledge_bases(
            self.store,
            retrieval,
            scope=scope,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        evidence = _ask_evidence_from_retrieval(retrieval)
        evidence_check = _ask_verify_evidence(query=rewrite_query, evidence=evidence, scope=scope, ask_intent=ask_intent)
        evidence = _ask_apply_evidence_check(evidence, evidence_check)
        retrieval = _ask_apply_evidence_check_to_retrieval(retrieval, evidence_check)
        steps.append(_ask_quick_read_step(sequence=len(steps) + 1, evidence=evidence, started_at=started_at))
        final_synthesis: dict[str, Any] | None = None
        if evidence_check.get("status") == "supported":
            deterministic_answer = _ask_quick_answer(query, retrieval, ask_intent=ask_intent)
            final_synthesis = self._workspace_ask_quick_agentic_synthesis(
                query=query,
                rewrite_query=rewrite_query,
                ask_intent=ask_intent,
                evidence=evidence,
                evidence_check=evidence_check,
                user=user,
                represented_user_id=represented_user_id,
                session_id=session_id,
            )
            answer = str((final_synthesis or {}).get("answer") or "").strip() or deterministic_answer
            if _ask_should_use_deterministic_coverage_guard(query, answer, deterministic_answer, ask_intent=ask_intent):
                final_synthesis = {
                    **(final_synthesis or {}),
                    "status": "fallback",
                    "owner": "deterministic_fallback",
                    "reason": "agentic_synthesis_missing_evidence_values",
                    "agentic_answer": answer,
                }
                answer = deterministic_answer
            answer = _ask_polish_quick_supported_answer(answer, ask_intent=ask_intent)
            answer_type = "kb_answer"
        else:
            answer = _ask_no_answer_from_evidence_check(query, evidence_check)
            answer_type = "no_answer"
        answer_detail = "已完成证据归纳和引用校验。"
        if final_synthesis and final_synthesis.get("status") == "succeeded":
            answer_detail = "已由 agentic service 基于通过校验的证据归纳成回答。"
        elif final_synthesis and final_synthesis.get("status") == "fallback":
            answer_detail = "Agentic 归纳不可用，已使用确定性证据摘要兜底。"
        steps.append(
            _ask_agent_step(
                sequence=len(steps) + 1,
                phase="answer",
                status="complete",
                title="形成回答",
                detail=answer_detail,
                started_at=started_at,
            )
        )
        elapsed_ms = _elapsed_ms(started_at)
        return {
            "ok": True,
            "query": query,
            "intent": ask_intent,
            "rewrite_query": rewrite_query,
            "answer": answer,
            "answer_type": answer_type,
            "route": {
                "intent": ask_intent,
                "requested_intent": intent,
                "selected_intent": "quick",
                "retrieval_owner": "pska",
                "surface": surface,
                "requires_agentic_service_online": False,
                "tool_policy": {"mode": "none"},
                "final_synthesis_owner": (final_synthesis or {}).get("owner") or "deterministic_fallback",
                "routing_owner": "pska_planner",
                "query_terms": query_terms,
                "rewrite_query": rewrite_query,
                "scope_applied": understand.get("scope_applied") or _ask_scope_applied(scope, ask_intent=ask_intent),
                "understand": understand,
                "intent_contract": understand.get("intent_contract") if isinstance(understand.get("intent_contract"), dict) else {},
                "scope_context_nodes": len(_list_of_dicts((scope or {}).get("context_nodes"))),
            },
            "evidence": evidence,
            "citations": evidence["citations"],
            "source_refs": evidence["source_refs"],
            "citation_audit": {
                "used": evidence["citations"],
                "dropped": _list_of_dicts(evidence_check.get("dropped_citations")),
            },
            "evidence_check": evidence_check,
            "evidence_claims": list(evidence_check.get("evidence_claims") or []),
            "no_answer_reasons": list(evidence_check.get("no_answer_reasons") or []),
            "agent_steps": steps,
            "trace": {
                "mode": "quick",
                "query_terms": query_terms,
                "retrieval_query": retrieval_query,
                "scope": _ask_scope_trace(scope or {}),
                "retrieval_owner": "pska",
                "retrieval": retrieval,
                "final_synthesis": final_synthesis or {"status": "not_attempted"},
                "diagnostics": retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {},
                "evidence_check": evidence_check,
            },
            "timing": {
                "total_ms": elapsed_ms,
                "time_to_first_answer_ms": elapsed_ms,
                "time_to_first_agent_event_ms": steps[0].get("elapsed_ms") if steps else None,
            },
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
        }

    def _workspace_ask_quick_agentic_synthesis(
        self,
        *,
        query: str,
        rewrite_query: str,
        ask_intent: str,
        evidence: dict[str, Any],
        evidence_check: dict[str, Any],
        user: Any,
        represented_user_id: str,
        session_id: str | None,
    ) -> dict[str, Any] | None:
        if not hasattr(self.agentic_service, "search"):
            return {"status": "fallback", "owner": "deterministic_fallback", "reason": "agentic_service_unavailable"}
        prompt = _ask_quick_synthesis_prompt(query=query, rewrite_query=rewrite_query, ask_intent=ask_intent, evidence=evidence, evidence_check=evidence_check)
        try:
            agentic = self._agentic_service_search(
                prompt,
                user,
                represented_user_id=represented_user_id,
                max_iterations=1,
                skills=[],
                tool_policy={"mode": "none"},
                session_id=session_id,
            )
        except Exception as exc:
            return {"status": "fallback", "owner": "deterministic_fallback", "reason": f"{type(exc).__name__}: {exc}"}
        answer = _ask_answer_from_agentic_synthesis(agentic.get("answer"))
        if not answer:
            return {
                "status": "fallback",
                "owner": "deterministic_fallback",
                "reason": "invalid_agentic_synthesis_json",
                "provider": (agentic.get("agentic_service") or {}).get("provider") if isinstance(agentic.get("agentic_service"), dict) else None,
            }
        return {
            "status": "succeeded",
            "owner": "fastreact_agentic_service",
            "answer": answer,
            "provider": (agentic.get("agentic_service") or {}).get("provider") if isinstance(agentic.get("agentic_service"), dict) else None,
            "adapter": (agentic.get("agentic_service") or {}).get("adapter") if isinstance(agentic.get("agentic_service"), dict) else None,
            "evidence_count": len(_list_of_dicts(evidence.get("results"))),
            "citation_count": len(_list_of_dicts(evidence.get("citations"))),
            "evidence_status": evidence_check.get("status"),
        }

    def workspace_today(
        self,
        *,
        owner_user_id: str | None = None,
        limit: int = 10,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        limit = max(1, min(limit, 50))
        dashboard = self.console_dashboard(owner_user_id=owner_user_id, tenant_id=tenant_id, limit=limit)
        reviews = self.console_reviews(status="pending", owner_user_id=owner_user_id, tenant_id=tenant_id, limit=limit)
        corpus = self.workspace_corpus(owner_user_id=owner_user_id, limit=limit, context=context)
        activity = self.workspace_activity(owner_user_id=owner_user_id, limit=limit, context=context)
        discoveries = self.workspace_discoveries(owner_user_id=owner_user_id, limit=limit, context=context)
        stats = self.job_stats(tenant_id=tenant_id)["stats"]
        review_items = reviews.get("review_items") if isinstance(reviews.get("review_items"), list) else []
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "read_only": True,
            "surface": "today",
            "continue_working": [_today_continue_item_from_activity(item) for item in activity["continue_working"][: min(limit, 6)]],
            "discoveries": discoveries["discoveries"][:limit],
            "needs_review": [_today_review_item(item) for item in review_items[:limit]],
            "system": {
                "source_counts": dashboard.get("source_counts") or {},
                "digest_backlog": stats.get("digest_backlog") or dashboard.get("digest_backlog") or {},
                "pending_reviews": {
                    "total_matching": reviews.get("total_matching") or 0,
                    "count": reviews.get("count") or 0,
                },
                "failed_jobs": dashboard.get("failed_jobs") or {},
                "service_readiness": dashboard.get("service_readiness") or {},
                "deterministic_next_actions": dashboard.get("deterministic_next_actions") or [],
            },
            "source": {
                "composed_from": [
                    "console_dashboard",
                    "console_reviews",
                    "workspace_corpus",
                    "workspace_activity",
                    "workspace_discoveries",
                    "job_stats",
                ],
                "uses_workspace_activity": True,
                "uses_dedicated_discovery_feed": True,
            },
        }

    def workspace_discoveries(
        self,
        *,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
        min_score: float = DISCOVERY_TODAY_SCORE_THRESHOLD,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context, tenant_id)
        DiscoveryService(self.store, owner_user_id=owner_user_id, tenant_id=tenant_id).produce()
        since = datetime.now(UTC) - timedelta(days=7)
        items = self.store.list_discovery_items(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            status="new",
            since=since,
            limit=100,
        )
        threshold = max(0.0, min(float(min_score), 1.0))
        filtered = [item for item in items if float(getattr(item, "discovery_score", 0.0) or 0.0) >= threshold]
        filtered = filtered[: max(1, min(limit, 100))]
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "window_days": 7,
            "min_score": threshold,
            "discoveries": [_discovery_item_payload(item) for item in filtered],
            "count": len(filtered),
            "total_new": len(items),
        }

    def record_workspace_activity(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id"))
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        actor_user_id = str(payload.get("actor_user_id") or owner_user_id)
        activity_type = str(payload.get("activity_type") or "").strip().lower()
        if activity_type not in {"opened", "edited", "viewed", "pinned"}:
            raise ValueError("activity_type must be one of opened, edited, viewed, pinned")
        target_type = str(payload.get("target_type") or "workspace_surface").strip() or "workspace_surface"
        target_id = str(payload.get("target_id") or payload.get("surface") or activity_type).strip()
        if not target_id:
            raise ValueError("target_id is required")
        surface = str(payload.get("surface") or target_type).strip() or target_type
        event = WorkspaceActivityEvent(
            workspace_activity_event_id=f"wact_{uuid4().hex}",
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            activity_type=activity_type,
            target_type=target_type,
            target_id=target_id,
            surface=surface,
            title=str(payload.get("title") or _workspace_activity_default_title(surface, target_id)),
            summary=str(payload.get("summary") or ""),
            metadata=dict(payload.get("metadata") or {}),
            tenant_id=tenant_id,
        )
        return {"ok": True, "activity": to_jsonable(self.store.add_workspace_activity_event(event))}

    def workspace_activity(
        self,
        *,
        owner_user_id: str | None = None,
        limit: int = 50,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        events = self.store.list_workspace_activity_events(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            activity_types={"opened", "edited", "viewed", "pinned"},
            limit=max(1, min(limit, 100)),
        )
        activity_items = [_workspace_activity_item(event) for event in events]
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "activity": activity_items,
            "continue_working": _workspace_continue_working(activity_items, limit=limit),
            "count": len(activity_items),
        }

    def workspace_corpus(
        self,
        *,
        owner_user_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        source_channel: str | None = None,
        query: str | None = None,
        limit: int = 20,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        limit = max(1, min(limit, 100))
        query_text = str(query or "").strip().lower()
        channel = str(source_channel or "").strip()
        all_sources = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == owner_user_id and _is_active_lifecycle(item)]
        scoped_source_item_ids = self.store.list_knowledge_base_source_item_ids(
            set(knowledge_base_ids or []),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        if knowledge_base_ids:
            all_sources = [item for item in all_sources if item.source_item_id in scoped_source_item_ids]
        all_source_ids = {item.source_item_id for item in all_sources}
        all_chunks = self.store.list_chunks_for_sources(all_source_ids)
        chunks_by_source: dict[str, list[Any]] = {}
        for chunk in all_chunks:
            chunks_by_source.setdefault(chunk.source_item_id, []).append(chunk)

        filtered_sources = [
            item
            for item in all_sources
            if _workspace_source_matches(item, chunks_by_source.get(item.source_item_id, []), source_channel=channel, query=query_text)
        ]
        filtered_sources.sort(key=lambda item: getattr(item, "created_at", datetime.min.replace(tzinfo=UTC)), reverse=True)
        limited_sources = filtered_sources[:limit]
        limited_source_ids = {item.source_item_id for item in limited_sources}
        filtered_chunks = [
            chunk
            for chunk in all_chunks
            if chunk.source_item_id in limited_source_ids and _workspace_chunk_matches(chunk, query=query_text)
        ][: limit * 3]
        memories = sorted(
            self.store.list_agent_memories(owner_user_id=owner_user_id, tenant_id=tenant_id),
            key=lambda memory: (float(getattr(memory, "confidence", 0.0) or 0.0), getattr(memory, "agent_memory_id", "")),
            reverse=True,
        )[:limit]
        profiles = sorted(
            self.store.list_profile_cards(owner_user_id=owner_user_id, tenant_id=tenant_id),
            key=lambda card: (float(getattr(card, "confidence", 0.0) or 0.0), getattr(card, "profile_card_id", "")),
            reverse=True,
        )[:limit]
        entities = [entity for entity in self.store.list_entities(tenant_id=tenant_id) if getattr(entity, "owner_user_id", "") == owner_user_id]
        entity_by_id = {entity.entity_id: entity for entity in entities}
        edge_pairs = self.store.list_hyperedges_for_entities(set(entity_by_id))
        edge_summaries = [
            _workspace_hyperedge(edge, members, entity_by_id)
            for edge, members in edge_pairs
            if getattr(edge, "owner_user_id", "") == owner_user_id
        ][:limit]
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "read_only": True,
            "filters": {
                "source_channel": channel or None,
                "query": query or "",
                "limit": limit,
                "knowledge_base_ids": list(knowledge_base_ids or []),
                "available_source_channels": sorted({item.source_channel for item in all_sources}),
            },
            "counts": {
                "sources_total": len(all_sources),
                "sources_matching": len(filtered_sources),
                "chunks_matching": len(filtered_chunks),
                "documents": len({chunk.document_id for chunk in all_chunks}),
                "entities": len(entities),
                "hyperedges": len(edge_summaries),
                "memories": len(memories),
                "profiles": len(profiles),
            },
            "sources": [_workspace_source(item, chunks_by_source.get(item.source_item_id, [])) for item in limited_sources],
            "chunks": [_workspace_chunk(chunk) for chunk in filtered_chunks],
            "documents": _workspace_documents(filtered_chunks),
            "entities": [_workspace_entity(entity) for entity in entities[:limit]],
            "hyperedges": edge_summaries,
            "memories": [_console_agent_memory(memory) for memory in memories],
            "profiles": [_console_profile_card(card) for card in profiles],
        }

    def workspace_documents_data(
        self,
        payload: dict[str, Any] | None = None,
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        include_deleted = bool(payload.get("include_deleted", True))
        limit = max(1, min(int(payload.get("limit") or 100), 500))
        knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
        active_scoped_source_item_ids = self.store.list_knowledge_base_source_item_ids(
            set(knowledge_base_ids),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            active_only=True,
        )
        deleted_scoped_source_item_ids = (
            self.store.list_knowledge_base_source_item_ids(
                set(knowledge_base_ids),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                active_only=False,
            )
            if include_deleted
            else set()
        )
        source_items = [
            item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id and (include_deleted or _is_active_lifecycle(item))
        ]
        if knowledge_base_ids:
            source_items = [
                item
                for item in source_items
                if item.source_item_id in active_scoped_source_item_ids
                or (include_deleted and not _is_active_lifecycle(item) and item.source_item_id in deleted_scoped_source_item_ids)
            ]
        source_items.sort(key=lambda item: (getattr(item, "updated_at", None) or getattr(item, "created_at", datetime.min.replace(tzinfo=UTC))), reverse=True)
        source_items = source_items[:limit]
        source_ids = {item.source_item_id for item in source_items}
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        docs_by_source: dict[str, list[Any]] = {}
        chunks_by_source: dict[str, list[Any]] = {}
        for document in documents:
            docs_by_source.setdefault(document.source_item_id, []).append(document)
        for chunk in chunks:
            chunks_by_source.setdefault(chunk.source_item_id, []).append(chunk)
        rows = []
        for item in source_items:
            source_id = item.source_item_id
            knowledge_base_lineage = _source_item_knowledge_base_lineage(
                self.store,
                source_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                active_only=_is_active_lifecycle(item),
            )
            rows.append(
                {
                    **_workspace_source(item, chunks_by_source.get(source_id, [])),
                    **knowledge_base_lineage,
                    "lifecycle_status": _lifecycle_status(item),
                    "deleted_at": getattr(item, "deleted_at", None),
                    "deleted_by": getattr(item, "deleted_by", None),
                    "delete_reason": getattr(item, "delete_reason", None),
                    "document_count": len(docs_by_source.get(source_id, [])),
                    "chunk_count": len(chunks_by_source.get(source_id, [])),
                    "impact": _document_delete_impact(self.store, tenant_id=tenant_id, source_item_ids=[source_id]),
                }
            )
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "include_deleted": include_deleted,
            "knowledge_base_ids": knowledge_base_ids,
            "documents": rows,
            "counts": {
                "documents": len(rows),
                "active": sum(1 for item in source_items if _is_active_lifecycle(item)),
                "deleted": sum(1 for item in source_items if _lifecycle_status(item) == "deleted"),
                "stale": sum(1 for item in source_items if _lifecycle_status(item) == "stale"),
            },
        }

    def workspace_reader_source(
        self,
        payload: dict[str, Any] | None = None,
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = context.apply_to_payload(payload or {}) if context else dict(payload or {})
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        source_item_id = str(payload.get("source_item_id") or "").strip()
        if not source_item_id:
            raise ValueError("source_item_id is required")
        knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
        source_item = next(
            (
                item
                for item in self.store.list_source_items(tenant_id=tenant_id)
                if item.source_item_id == source_item_id
                and item.owner_user_id == owner_user_id
                and _is_active_lifecycle(item)
            ),
            None,
        )
        if source_item is None:
            raise PermissionError("source item not found or not accessible")
        if knowledge_base_ids:
            scoped_ids = self.store.list_knowledge_base_source_item_ids(
                set(knowledge_base_ids),
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                active_only=True,
            )
            if source_item_id not in scoped_ids:
                raise PermissionError("source item is outside the selected knowledge base scope")
        documents = [
            document
            for document in self.store.list_documents_for_sources({source_item_id})
            if getattr(document, "owner_user_id", "") == owner_user_id and _is_active_lifecycle(document)
        ]
        chunks = [
            chunk
            for chunk in self.store.list_chunks_for_sources({source_item_id})
            if getattr(chunk, "owner_user_id", "") == owner_user_id and _is_active_lifecycle(chunk)
        ]
        documents.sort(key=lambda document: str(getattr(document, "document_id", "") or ""))
        chunks.sort(key=lambda chunk: (str(getattr(chunk, "document_id", "") or ""), int(getattr(chunk, "ordinal", 0) or 0)))
        passage_windows = _passage_windows_for_documents(documents, chunks, target_tokens=12000)
        max_document_chars = max(1000, min(int(payload.get("max_document_chars") or 60000), 200000))
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "source_item": {
                **_workspace_source(source_item, chunks),
                **_source_item_knowledge_base_lineage(
                    self.store,
                    source_item_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    active_only=True,
                ),
                "lifecycle_status": _lifecycle_status(source_item),
            },
            "documents": [_reader_document_payload(document, max_chars=max_document_chars) for document in documents],
            "chunks": [_reader_chunk_payload(chunk) for chunk in chunks],
            "passage_windows": [_reader_passage_window_payload(window) for window in passage_windows],
            "scope_applied": {
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_ids": [source_item_id],
                "scope_mode": "hard" if knowledge_base_ids else "source",
            },
            "counts": {
                "documents": len(documents),
                "chunks": len(chunks),
                "passage_windows": len(passage_windows),
            },
        }

    def workspace_documents_delete(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        requested_ids = _string_list(payload.get("source_item_ids") or payload.get("source_item_id") or payload.get("document_ids"))
        if not requested_ids:
            raise ValueError("source_item_ids is required")
        owned_ids = [
            item.source_item_id
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id and item.source_item_id in set(requested_ids)
        ]
        if not owned_ids:
            raise PermissionError("no owned document entries matched")
        execute = bool(payload.get("execute", False))
        restore = bool(payload.get("restore", False))
        delete_mode = str(payload.get("mode") or payload.get("delete_mode") or "").strip().lower()
        hard_delete = delete_mode in {"hard", "purge", "hard_purge"} or bool(payload.get("hard_delete", False))
        knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
        if delete_mode in {"membership", "scope", "knowledge_base", "kb"} and not knowledge_base_ids:
            raise ValueError("knowledge_base_id is required for membership delete")
        membership_delete = bool(
            not restore
            and not hard_delete
            and knowledge_base_ids
            and delete_mode not in {"source", "soft", "lifecycle", "global"}
        )
        membership_source_ids: list[str] = []
        orphan_source_ids: list[str] = []
        if membership_delete:
            for knowledge_base_id in knowledge_base_ids:
                _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            membership_plan = _document_membership_delete_plan(
                self.store,
                source_item_ids=owned_ids,
                knowledge_base_ids=knowledge_base_ids,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            membership_source_ids = membership_plan["membership_source_item_ids"]
            orphan_source_ids = membership_plan["orphan_source_item_ids"]
        reason = str(payload.get("reason") or ("restore document" if restore else "workspace document lifecycle update"))
        impact_source_ids = orphan_source_ids if membership_delete else owned_ids
        impact = _document_delete_impact(self.store, tenant_id=tenant_id, source_item_ids=impact_source_ids)
        if membership_delete:
            impact = {
                **impact,
                "knowledge_base_source_items": len(membership_source_ids),
                "orphan_source_items": len(orphan_source_ids),
            }
        if not execute:
            return {
                "ok": True,
                "dry_run": True,
                "execute": False,
                "restore": restore,
                "hard_delete": hard_delete,
                "delete_mode": "membership" if membership_delete else ("hard" if hard_delete else "source"),
                "knowledge_base_ids": knowledge_base_ids,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "source_item_ids": membership_source_ids if membership_delete else owned_ids,
                "counts": impact,
                "notes": _document_delete_notes(restore=restore, hard_delete=hard_delete, membership_delete=membership_delete),
            }
        actor_user_id = _actor_user_id(context, payload)
        deleted: dict[str, int] = {}
        if restore:
            deleted.update(
                self.store.update_source_lifecycle(
                    owned_ids,
                    lifecycle_status="active",
                    actor_user_id=actor_user_id,
                    reason=reason,
                    tenant_id=tenant_id,
                )
            )
            updater = getattr(self.store, "update_artifact_support_status_for_sources", None)
            if callable(updater):
                deleted["artifact_supports_restored"] = updater(set(owned_ids), tenant_id=tenant_id, status="active")
        elif hard_delete:
            deleted.update(_hard_delete_source_derivatives(self.store, owned_ids))
            deleted.update(
                self.store.update_source_lifecycle(
                    owned_ids,
                    lifecycle_status="purged",
                    actor_user_id=actor_user_id,
                    reason=reason,
                    tenant_id=tenant_id,
                    hard_delete=True,
                )
            )
        elif membership_delete:
            for knowledge_base_id in knowledge_base_ids:
                membership_deleted = self.store.archive_knowledge_base_source_items(
                    knowledge_base_id,
                    owned_ids,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    actor_user_id=actor_user_id,
                    reason=reason,
                )
                for key, value in membership_deleted.items():
                    deleted[key] = deleted.get(key, 0) + value
            if orphan_source_ids:
                deleted["orphan_source_items"] = len(orphan_source_ids)
                deleted.update(
                    self.store.update_source_lifecycle(
                        orphan_source_ids,
                        lifecycle_status="deleted",
                        actor_user_id=actor_user_id,
                        reason=reason,
                        tenant_id=tenant_id,
                    )
                )
                stale = _mark_source_derivatives_stale(self.store, orphan_source_ids, tenant_id=tenant_id, actor_user_id=actor_user_id, reason=reason)
                deleted.update({f"stale_{key}": value for key, value in stale.items()})
        else:
            deleted.update(
                self.store.update_source_lifecycle(
                    owned_ids,
                    lifecycle_status="deleted",
                    actor_user_id=actor_user_id,
                    reason=reason,
                    tenant_id=tenant_id,
                )
            )
            stale = _mark_source_derivatives_stale(self.store, owned_ids, tenant_id=tenant_id, actor_user_id=actor_user_id, reason=reason)
            deleted.update({f"stale_{key}": value for key, value in stale.items()})
        return {
            "ok": True,
            "dry_run": False,
            "execute": True,
            "restore": restore,
            "hard_delete": hard_delete,
            "delete_mode": "membership" if membership_delete else ("hard" if hard_delete else "source"),
            "knowledge_base_ids": knowledge_base_ids,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "source_item_ids": membership_source_ids if membership_delete else owned_ids,
            "counts": impact,
            "deleted": deleted,
            "notes": _document_delete_notes(restore=restore, hard_delete=hard_delete, membership_delete=membership_delete),
        }

    def workspace_documents_link(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        requested_ids = _string_list(payload.get("source_item_ids") or payload.get("source_item_id") or payload.get("document_ids"))
        if not requested_ids:
            raise ValueError("source_item_ids is required")
        target_knowledge_base_ids = []
        target_knowledge_base_ids.extend(_string_list(payload.get("target_knowledge_base_id")))
        target_knowledge_base_ids.extend(_string_list(payload.get("target_knowledge_base_ids")))
        target_knowledge_base_ids.extend(_knowledge_base_ids_from_payload(payload))
        target_knowledge_base_ids = list(dict.fromkeys(item for item in target_knowledge_base_ids if item))
        if not target_knowledge_base_ids:
            raise ValueError("target_knowledge_base_id is required")
        for knowledge_base_id in target_knowledge_base_ids:
            _get_accessible_knowledge_base(self.store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)

        requested_id_set = set(requested_ids)
        source_items = [
            item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id and item.source_item_id in requested_id_set and _is_active_lifecycle(item)
        ]
        if not source_items:
            raise PermissionError("no active owned document entries matched")
        source_items.sort(key=lambda item: item.source_item_id)
        source_item_ids = [item.source_item_id for item in source_items]
        source_item_id_set = set(source_item_ids)

        active_pairs: set[tuple[str, str]] = set()
        reactivated_pairs: set[tuple[str, str]] = set()
        for knowledge_base_id in target_knowledge_base_ids:
            active_ids = self.store.list_knowledge_base_source_item_ids(
                {knowledge_base_id},
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                active_only=True,
            ) & source_item_id_set
            all_ids = self.store.list_knowledge_base_source_item_ids(
                {knowledge_base_id},
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                active_only=False,
            ) & source_item_id_set
            active_pairs.update((knowledge_base_id, source_item_id) for source_item_id in active_ids)
            reactivated_pairs.update((knowledge_base_id, source_item_id) for source_item_id in all_ids - active_ids)

        requested_pairs = {
            (knowledge_base_id, source_item_id)
            for knowledge_base_id in target_knowledge_base_ids
            for source_item_id in source_item_ids
        }
        new_pairs = requested_pairs - active_pairs - reactivated_pairs
        changed_pairs = new_pairs | reactivated_pairs
        execute = bool(payload.get("execute", False))
        counts = {
            "requested_source_items": len(requested_id_set),
            "source_items": len(source_item_ids),
            "target_knowledge_bases": len(target_knowledge_base_ids),
            "knowledge_base_source_items": len(changed_pairs),
            "already_present": len(active_pairs),
            "reactivated": len(reactivated_pairs),
            "new": len(new_pairs),
        }
        response = {
            "ok": True,
            "dry_run": not execute,
            "execute": execute,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "knowledge_base_ids": target_knowledge_base_ids,
            "target_knowledge_base_ids": target_knowledge_base_ids,
            "source_item_ids": source_item_ids,
            "counts": counts,
            "notes": [
                "This operation links existing active source items to the selected knowledge base by membership.",
                "It does not duplicate documents, chunks, vectors, or source connector configuration.",
            ],
        }
        if not execute:
            return response

        actor_user_id = _actor_user_id(context, payload)
        membership_type = str(payload.get("membership_type") or "manual").strip() or "manual"
        payload_metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        for knowledge_base_id in target_knowledge_base_ids:
            for source_item_id in source_item_ids:
                self.store.add_knowledge_base_source_item(
                    KnowledgeBaseSourceItem(
                        knowledge_base_id=knowledge_base_id,
                        source_item_id=source_item_id,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        added_by_user_id=actor_user_id,
                        membership_type=membership_type,
                        metadata={
                            **payload_metadata,
                            "bound_by": "workspace_api",
                            "membership_action": "link",
                            "target_knowledge_base_id": knowledge_base_id,
                        },
                    )
                )
        return {
            **response,
            "linked": counts,
        }

    def workspace_documents_move(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        requested_ids = _string_list(payload.get("source_item_ids") or payload.get("source_item_id") or payload.get("document_ids"))
        if not requested_ids:
            raise ValueError("source_item_ids is required")
        source_knowledge_base_id = str(
            payload.get("source_knowledge_base_id")
            or payload.get("from_knowledge_base_id")
            or payload.get("current_knowledge_base_id")
            or ""
        ).strip()
        if not source_knowledge_base_id:
            source_ids_from_payload = _knowledge_base_ids_from_payload(payload)
            source_knowledge_base_id = source_ids_from_payload[0] if source_ids_from_payload else ""
        target_knowledge_base_id = str(payload.get("target_knowledge_base_id") or payload.get("to_knowledge_base_id") or "").strip()
        if not source_knowledge_base_id:
            raise ValueError("source_knowledge_base_id is required")
        if not target_knowledge_base_id:
            raise ValueError("target_knowledge_base_id is required")
        if source_knowledge_base_id == target_knowledge_base_id:
            raise ValueError("target_knowledge_base_id must differ from source_knowledge_base_id")
        _get_accessible_knowledge_base(self.store, source_knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        _get_accessible_knowledge_base(self.store, target_knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)

        requested_id_set = set(requested_ids)
        active_owned_ids = {
            item.source_item_id
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id and item.source_item_id in requested_id_set and _is_active_lifecycle(item)
        }
        source_scoped_ids = self.store.list_knowledge_base_source_item_ids(
            {source_knowledge_base_id},
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            active_only=True,
        )
        source_item_ids = sorted(active_owned_ids & source_scoped_ids)
        if not source_item_ids:
            raise PermissionError("no active source membership matched")
        source_item_id_set = set(source_item_ids)

        target_active_ids = self.store.list_knowledge_base_source_item_ids(
            {target_knowledge_base_id},
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            active_only=True,
        ) & source_item_id_set
        target_all_ids = self.store.list_knowledge_base_source_item_ids(
            {target_knowledge_base_id},
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            active_only=False,
        ) & source_item_id_set
        target_reactivated_ids = target_all_ids - target_active_ids
        target_new_ids = source_item_id_set - target_active_ids - target_reactivated_ids
        target_changed = len(target_new_ids) + len(target_reactivated_ids)
        execute = bool(payload.get("execute", False))
        counts = {
            "requested_source_items": len(requested_id_set),
            "source_items": len(source_item_ids),
            "moved": len(source_item_ids),
            "source_knowledge_base_source_items": len(source_item_ids),
            "target_knowledge_base_source_items": target_changed,
            "knowledge_base_source_items": len(source_item_ids) + target_changed,
            "already_present": len(target_active_ids),
            "reactivated": len(target_reactivated_ids),
            "new": len(target_new_ids),
            "orphan_source_items": 0,
        }
        response = {
            "ok": True,
            "dry_run": not execute,
            "execute": execute,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "source_knowledge_base_id": source_knowledge_base_id,
            "target_knowledge_base_id": target_knowledge_base_id,
            "knowledge_base_ids": [source_knowledge_base_id, target_knowledge_base_id],
            "source_item_ids": source_item_ids,
            "counts": counts,
            "notes": [
                "This operation moves existing active source items between knowledge bases by membership.",
                "It activates the target membership before archiving the source membership, so the source item is not soft-deleted as an orphan.",
            ],
        }
        if not execute:
            return response

        actor_user_id = _actor_user_id(context, payload)
        membership_type = str(payload.get("membership_type") or "manual").strip() or "manual"
        payload_metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        for source_item_id in source_item_ids:
            self.store.add_knowledge_base_source_item(
                KnowledgeBaseSourceItem(
                    knowledge_base_id=target_knowledge_base_id,
                    source_item_id=source_item_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    added_by_user_id=actor_user_id,
                    membership_type=membership_type,
                    metadata={
                        **payload_metadata,
                        "bound_by": "workspace_api",
                        "membership_action": "move",
                        "source_knowledge_base_id": source_knowledge_base_id,
                        "target_knowledge_base_id": target_knowledge_base_id,
                    },
                )
            )
        archived = self.store.archive_knowledge_base_source_items(
            source_knowledge_base_id,
            source_item_ids,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            reason=str(payload.get("reason") or "workspace document move"),
        )
        moved = {
            **counts,
            "archived_source_memberships": archived.get("knowledge_base_source_items", 0),
        }
        return {
            **response,
            "moved": moved,
        }

    def workspace_graph_data(
        self,
        *,
        owner_user_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 30,
        node_types: set[str] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        limit = max(1, min(limit, 100))
        scope = _knowledge_base_scope_for_ids(self.store, knowledge_base_ids or [], tenant_id=tenant_id, owner_user_id=owner_user_id)
        scoped_source_item_ids = set(_string_list(scope.get("source_item_ids")))
        has_kb_scope = bool(_knowledge_base_ids_from_scope(scope))
        source_items = [
            item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id
            and _is_active_lifecycle(item)
            and (not has_kb_scope or item.source_item_id in scoped_source_item_ids)
        ][:limit]
        source_ids = {item.source_item_id for item in source_items}
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        passage_windows = _passage_windows_for_documents(documents, chunks)
        claims = self.store.list_knowledge_claims(owner_user_id=owner_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=limit * 4)
        digest_notes = self.store.list_digest_notes(owner_user_id=owner_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=limit)
        topics = self.store.list_knowledge_topics(tenant_id=tenant_id, owner_user_id=owner_user_id, limit=limit * 2)
        topic_ids = {topic.topic_id for topic in topics}
        topic_mentions = self.store.list_topic_mentions(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            topic_ids=topic_ids or None,
            source_item_ids=source_ids or None,
            limit=limit * 10,
        )
        artifact_supports = self.store.list_artifact_supports(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            source_item_ids=source_ids or None,
            status="active",
            limit=limit * 10,
        )
        memories = self.store.list_agent_memories(owner_user_id=owner_user_id, tenant_id=tenant_id)
        if has_kb_scope:
            memories = [
                memory
                for memory in memories
                if _source_refs_match_source_item_ids(_source_refs_payload(getattr(memory, "source_refs", [])), scoped_source_item_ids)
            ]
        memories = memories[:limit]
        review_items = [
            item
            for item in self.store.list_review_items(tenant_id=tenant_id)
            if getattr(item, "owner_user_id", "") == owner_user_id and getattr(item, "status", "") == "pending"
            and (not has_kb_scope or _review_item_matches_source_item_ids(item, scoped_source_item_ids))
        ][:limit]
        entities = [entity for entity in self.store.list_entities(tenant_id=tenant_id) if getattr(entity, "owner_user_id", "") == owner_user_id]
        entity_by_id = {entity.entity_id: entity for entity in entities}
        hyperedges = [
            (edge, members)
            for edge, members in self.store.list_hyperedges_for_entities(set(entity_by_id))
            if getattr(edge, "owner_user_id", "") == owner_user_id
            and (not has_kb_scope or _source_refs_match_source_item_ids(_source_refs_payload(getattr(edge, "source_refs", [])), scoped_source_item_ids))
        ]
        if has_kb_scope:
            entity_ids = {getattr(member, "entity_id", "") for _, members in hyperedges for member in members}
            entities = [entity for entity in entities if getattr(entity, "entity_id", "") in entity_ids]
        nodes, edges = _workspace_graph_nodes_edges(
            source_items=source_items,
            documents=documents,
            passage_windows=passage_windows,
            claims=claims,
            digest_notes=digest_notes,
            memories=memories,
            review_items=review_items,
            entities=entities[: limit * 2],
            hyperedges=hyperedges[: limit * 2],
            topics=topics[: limit * 2],
            topic_mentions=topic_mentions,
            artifact_supports=artifact_supports,
        )
        unfiltered_counts = {"nodes": len(nodes), "edges": len(edges)}
        nodes, edges = _filter_workspace_graph_projection(nodes, edges, node_types=node_types)
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "ontology_version": "pska.graph.v2",
            "scope_applied": _knowledge_scope_applied(scope),
            "nodes": nodes,
            "edges": edges,
            "insights": _workspace_graph_insights(nodes, edges),
            "projection": {
                "nodes": len(nodes),
                "edges": len(edges),
                "unfiltered_nodes": unfiltered_counts["nodes"],
                "unfiltered_edges": unfiltered_counts["edges"],
                "node_types": sorted(node_types) if node_types else None,
            },
            "counts": {
                "sources": len(source_items),
                "documents": len(documents),
                "passages": len(passage_windows),
                "claims": len(claims),
                "digest_notes": len(digest_notes),
                "memories": len(memories),
                "review_items": len(review_items),
                "entities": len(entities),
                "topics": len(topics),
                "topic_mentions": len(topic_mentions),
                "artifact_supports": len(artifact_supports),
                "phrases": sum(1 for node in nodes if node.get("type") == "phrase"),
                "facts": len(hyperedges),
                "hyperedges": len(hyperedges),
            },
            "notes": [
                "Graph v2 treats digest notes and knowledge claims as first-class nodes.",
                "Passage windows are document-first context windows; chunks remain retrieval slices for compatibility.",
            ],
        }

    def workspace_graph_subgraph(
        self,
        *,
        node_id: str,
        owner_user_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 80,
        hops: int = 1,
        node_types: set[str] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        limit = max(1, min(limit, 160))
        hops = max(1, min(hops, 3))
        graph = self.workspace_graph_data(
            owner_user_id=owner_user_id,
            knowledge_base_ids=knowledge_base_ids,
            limit=limit,
            node_types=None,
            context=context,
        )
        nodes = _list_of_dicts(graph.get("nodes"))
        edges = _list_of_dicts(graph.get("edges"))
        sub_nodes, sub_edges = _workspace_graph_subgraph(nodes, edges, node_id=node_id, hops=hops, node_types=node_types)
        return {
            "ok": bool(sub_nodes),
            "owner_user_id": owner_user_id,
            "ontology_version": graph.get("ontology_version") or "pska.graph.v2",
            "scope_applied": graph.get("scope_applied") or {},
            "node_id": node_id,
            "hops": hops,
            "nodes": sub_nodes,
            "edges": sub_edges,
            "insights": _workspace_graph_insights(sub_nodes, sub_edges),
            "evidence_path": _workspace_graph_evidence_path(sub_nodes, sub_edges, node_id),
            "projection": {
                "nodes": len(sub_nodes),
                "edges": len(sub_edges),
                "source_nodes": len(nodes),
                "source_edges": len(edges),
                "node_types": sorted(node_types) if node_types else None,
            },
            "counts": graph.get("counts") or {},
            "notes": [
                "Subgraph is derived from the current Graph v2 projection.",
                "Use it for local expansion instead of loading the full graph into the browser.",
            ],
        }

    def workspace_graph_search_subgraph(
        self,
        *,
        query: str,
        owner_user_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        limit: int = 80,
        hops: int = 1,
        top_k: int = 5,
        node_types: set[str] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        limit = max(1, min(limit, 160))
        hops = max(1, min(hops, 3))
        top_k = max(1, min(top_k, 20))
        graph = self.workspace_graph_data(
            owner_user_id=owner_user_id,
            knowledge_base_ids=knowledge_base_ids,
            limit=limit,
            node_types=None,
            context=context,
        )
        nodes = _list_of_dicts(graph.get("nodes"))
        edges = _list_of_dicts(graph.get("edges"))
        matches = _workspace_graph_search_nodes(nodes, query=query, node_types=node_types, limit=top_k)
        merged_nodes: dict[str, dict[str, Any]] = {}
        merged_edges: dict[str, dict[str, Any]] = {}
        for match in matches:
            sub_nodes, sub_edges = _workspace_graph_subgraph(nodes, edges, node_id=str(match.get("id") or ""), hops=hops, node_types=node_types)
            for node in sub_nodes:
                merged_nodes[str(node.get("id"))] = node
            for edge in sub_edges:
                merged_edges[str(edge.get("id"))] = edge
        sub_nodes = list(merged_nodes.values())
        sub_edges = list(merged_edges.values())
        return {
            "ok": bool(matches),
            "owner_user_id": owner_user_id,
            "ontology_version": graph.get("ontology_version") or "pska.graph.v2",
            "scope_applied": graph.get("scope_applied") or {},
            "query": query,
            "hops": hops,
            "matches": matches,
            "nodes": sub_nodes,
            "edges": sub_edges,
            "insights": _workspace_graph_insights(sub_nodes, sub_edges),
            "projection": {
                "nodes": len(sub_nodes),
                "edges": len(sub_edges),
                "source_nodes": len(nodes),
                "source_edges": len(edges),
                "node_types": sorted(node_types) if node_types else None,
            },
            "counts": graph.get("counts") or {},
            "notes": [
                "Search subgraph is derived from the current Graph v2 projection.",
                "Use it to enter Graph exploration from a keyword without loading the full graph.",
            ],
        }

    def graph_reindex(
        self,
        *,
        owner_user_id: str = "user_primary",
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: int = 100,
    ) -> dict[str, Any]:
        context = RequestContext(tenant_id=tenant_id, user_id=owner_user_id)
        graph = self.workspace_graph_data(owner_user_id=owner_user_id, limit=limit, context=context)
        counts = self.store.replace_graph_projection(
            owner_user_id=owner_user_id,
            nodes=graph.get("nodes") or [],
            edges=graph.get("edges") or [],
            tenant_id=tenant_id,
        )
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "ontology_version": graph.get("ontology_version"),
            "projection": counts,
            "graph_counts": graph.get("counts") or {},
            "limit": limit,
            "notes": [
                "Rebuilt graph_nodes/graph_edges from Graph v2 typed projection.",
                "Fact/Phrase are currently derived projection nodes; source tables remain unchanged.",
            ],
        }

    def workspace_graph_path(
        self,
        *,
        query: str,
        owner_user_id: str | None = None,
        top_k: int = 5,
        mode: str = "agentic",
        max_iterations: int = 3,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        user = self.store.get_user(owner_user_id, tenant_id=tenant_id)
        result = self.retrieval.search(query, user, represented_user_id=owner_user_id, top_k=max(1, min(top_k, 20)))
        deterministic = {
            "ok": True,
            "owner_user_id": owner_user_id,
            "query": query,
            "ontology_version": "pska.graph.v2",
            "mode": "deterministic",
            "requires_agentic_service_online": False,
            "results": to_jsonable(result.results),
            "citations": to_jsonable(result.citations),
            "graph_paths": to_jsonable(result.graph_paths),
            "score_debug": to_jsonable(result.score_debug),
            "gaps": result.gaps,
            "conflicts": result.conflicts,
            "sensitivity": result.sensitivity,
            "agentic_contract": _graph_agentic_contract(),
        }
        deterministic.update(_graph_path_product_payload(query, deterministic))
        if mode == "deterministic":
            return deterministic
        if mode != "agentic":
            raise ValueError(f"unsupported graph path mode: {mode}")
        try:
            agentic = self._agentic_service_search(
                _graph_agentic_query(query, deterministic),
                user,
                represented_user_id=owner_user_id,
                max_iterations=max(1, min(int(max_iterations or 3), 8)),
                skills=[],
                tool_policy={"mode": "none"},
            )
        except AgenticServiceError as exc:
            return {
                **deterministic,
                "ok": False,
                "mode": "agentic",
                "requires_agentic_service_online": True,
                "display_mode": "deterministic_fallback",
                "error": {
                    "type": "agentic_service_unavailable",
                    "message": "Agentic GraphRAG is unavailable. Deterministic graph retrieval path is shown.",
                    "detail": str(exc),
                },
                "deterministic": deterministic,
            }
        unusable = _agentic_graph_unusable_reason(agentic)
        if not unusable and not _agentic_graph_answer_too_short(agentic, deterministic):
            unusable = _agentic_graph_query_mismatch_reason(query, agentic, deterministic)
        if unusable:
            return {
                **deterministic,
                "ok": False,
                "mode": "agentic",
                "requires_agentic_service_online": True,
                "display_mode": "deterministic_fallback",
                "error": {
                    "type": "agentic_graph_answer_unusable",
                    "message": "Agentic GraphRAG returned an unusable answer. Deterministic graph retrieval path is shown.",
                    "detail": unusable,
                },
                "agentic_service": agentic.get("agentic_service") if isinstance(agentic.get("agentic_service"), dict) else {},
                "deterministic": deterministic,
            }
        repair_agentic = None
        if _agentic_graph_answer_too_short(agentic, deterministic):
            try:
                repair_agentic = self._agentic_service_search(
                    _graph_agentic_repair_query(query, deterministic, agentic),
                    user,
                    represented_user_id=owner_user_id,
                    max_iterations=max(1, min(int(max_iterations or 3), 8)),
                    skills=[],
                    tool_policy={"mode": "none"},
                )
                repair_unusable = _agentic_graph_unusable_reason(repair_agentic)
                if not repair_unusable:
                    repair_unusable = _agentic_graph_query_mismatch_reason(query, repair_agentic, deterministic)
                if not repair_unusable and not _agentic_graph_answer_too_short(repair_agentic, deterministic):
                    agentic = _merge_graph_agentic_repair(agentic, repair_agentic)
            except AgenticServiceError as exc:
                repair_agentic = {
                    "ok": False,
                    "error": {
                        "type": "agentic_graph_repair_unavailable",
                        "detail": str(exc),
                    },
                }
        answer_payload = _graph_agentic_answer_payload(agentic, deterministic)
        if repair_agentic is not None:
            answer_payload["agentic_repair"] = _graph_agentic_repair_summary(repair_agentic, answer_payload)
        return {
            **deterministic,
            "mode": "agentic",
            "requires_agentic_service_online": True,
            **answer_payload,
            "agentic_retrieval": agentic.get("retrieval") if isinstance(agentic.get("retrieval"), dict) else {},
            "agentic_trace": agentic.get("trace") if isinstance(agentic.get("trace"), dict) else {},
            "agentic_source_refs": agentic.get("source_refs") if isinstance(agentic.get("source_refs"), list) else [],
            "agentic_service": agentic.get("agentic_service") if isinstance(agentic.get("agentic_service"), dict) else {},
            **_agentic_fact_filter_payload(agentic, deterministic),
            "deterministic": deterministic,
        }

    def workspace_graph_topics(
        self,
        *,
        owner_user_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        topics = self.store.list_knowledge_topics(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            query=query,
            limit=max(1, min(int(limit or 100), 500)),
        )
        topic_ids = {topic.topic_id for topic in topics}
        mentions = self.store.list_topic_mentions(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            topic_ids=topic_ids or None,
            limit=5000,
        )
        supports = self.store.list_artifact_supports(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            artifact_type="topic",
            artifact_ids=topic_ids or None,
            status="active",
            limit=5000,
        )
        source_ids = {mention.source_item_id for mention in mentions}
        source_items = {
            item.source_item_id: item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id and item.source_item_id in source_ids and _is_active_lifecycle(item)
        }
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "topics": [
                _knowledge_topic_payload(
                    topic,
                    mentions=[mention for mention in mentions if mention.topic_id == topic.topic_id],
                    supports=[support for support in supports if support.topic_id == topic.topic_id or support.artifact_id == topic.topic_id],
                    source_items=source_items,
                )
                for topic in topics
            ],
            "counts": {
                "topics": len(topics),
                "mentions": len(mentions),
                "supports": len(supports),
                "sources": len(source_items),
            },
        }

    def workspace_graph_paths(
        self,
        *,
        query: str,
        owner_user_id: str | None = None,
        top_k: int = 5,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        topics_payload = self.workspace_graph_topics(owner_user_id=owner_user_id, query=query, limit=50, context=context)
        path_payload = self.workspace_graph_path(query=query, owner_user_id=owner_user_id, top_k=top_k, mode="deterministic", context=context)
        topics = _list_of_dicts(topics_payload.get("topics"))
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "query": query,
            "ontology_version": "pska.topic_fact_graph.v1",
            "topic_paths": _topic_paths_from_topic_payloads(topics, limit=max(1, min(top_k, 20))),
            "graph_paths": _list_of_dicts(path_payload.get("graph_paths")),
            "citations": _list_of_dicts(path_payload.get("citations")),
            "score_debug": path_payload.get("score_debug") or {},
            "counts": {
                "matching_topics": len(topics),
                "graph_paths": len(_list_of_dicts(path_payload.get("graph_paths"))),
            },
        }

    def workspace_digest_linking_run(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = _tenant_id_for_request(context, str(payload.get("tenant_id")) if payload.get("tenant_id") else None)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        requested_source_ids = set(_string_list(payload.get("source_item_ids")))
        limit = max(1, min(int(payload.get("limit") or 80), 300))
        max_topics_per_source = max(3, min(int(payload.get("max_topics_per_source") or 12), 40))
        source_items = [
            item
            for item in self.store.list_source_items(tenant_id=tenant_id)
            if item.owner_user_id == owner_user_id and _is_active_lifecycle(item)
        ]
        if requested_source_ids:
            source_items = [item for item in source_items if item.source_item_id in requested_source_ids]
        source_items = source_items[:limit]
        source_ids = {item.source_item_id for item in source_items}
        source_label_by_id = {item.source_item_id: item.title or item.source_item_id for item in source_items}
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        claims = self.store.list_knowledge_claims(owner_user_id=owner_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=limit * 4)
        notes = self.store.list_digest_notes(owner_user_id=owner_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=limit * 2)
        documents_by_source: dict[str, list[Any]] = {}
        chunks_by_source: dict[str, list[Any]] = {}
        claims_by_source: dict[str, list[Any]] = {}
        notes_by_source: dict[str, list[Any]] = {}
        for document in documents:
            documents_by_source.setdefault(str(getattr(document, "source_item_id", "") or ""), []).append(document)
        for chunk in chunks:
            chunks_by_source.setdefault(str(getattr(chunk, "source_item_id", "") or ""), []).append(chunk)
        for claim in claims:
            for ref in getattr(claim, "source_refs", []) or []:
                if ref.source_item_id:
                    claims_by_source.setdefault(ref.source_item_id, []).append(claim)
        for note in notes:
            for ref in getattr(note, "source_refs", []) or []:
                if ref.source_item_id:
                    notes_by_source.setdefault(ref.source_item_id, []).append(note)

        topic_by_id: dict[str, KnowledgeTopic] = {}
        mentions_by_topic: dict[str, list[TopicMention]] = {}
        supports_written = 0
        for item in source_items:
            candidates = _linking_topic_candidates_for_source(
                item=item,
                documents=documents_by_source.get(item.source_item_id) or [],
                chunks=chunks_by_source.get(item.source_item_id) or [],
                claims=claims_by_source.get(item.source_item_id) or [],
                digest_notes=notes_by_source.get(item.source_item_id) or [],
                max_topics=max_topics_per_source,
            )
            for candidate in candidates:
                normalized = _topic_normalized_label(str(candidate.get("label") or ""))
                if not normalized:
                    continue
                topic_id = _topic_stable_id(tenant_id=tenant_id, owner_user_id=owner_user_id, normalized_label=normalized)
                support_kinds = _string_list(candidate.get("support_kinds"))
                quality_tier = str(candidate.get("quality_tier") or "diagnostic")
                review_eligible = bool(candidate.get("review_eligible")) and quality_tier == "strong"
                support_artifacts = _list_of_dicts(candidate.get("support_artifacts"))
                source_refs = _list_of_dicts(candidate.get("source_refs")) or [
                    {
                        "source_item_id": item.source_item_id,
                        "document_id": candidate.get("document_id"),
                        "chunk_id": candidate.get("chunk_id"),
                        "mention_text": candidate.get("mention_text"),
                    }
                ]
                promotion_reason = str(candidate.get("promotion_reason") or ("strong_support" if review_eligible else "diagnostic_only"))
                topic = self.store.upsert_knowledge_topic(
                    KnowledgeTopic(
                        topic_id=topic_id,
                        owner_user_id=owner_user_id,
                        label=str(candidate.get("label") or normalized),
                        normalized_label=normalized,
                        topic_type="topic",
                        description=str(candidate.get("description") or ""),
                        confidence=float(candidate.get("confidence") or 0.0),
                        producer="pska.linking_digest",
                        metadata={
                            "quality_tier": quality_tier,
                            "support_kinds": support_kinds,
                            "promotion_reason": promotion_reason,
                            "review_eligible": review_eligible,
                            "support_artifacts": support_artifacts[:12],
                            "diagnostics": {
                                "lexical_only": not review_eligible,
                                "note": "Diagnostic topics do not create Review items or GraphRAG evidence paths.",
                            },
                            "run_type": "linking_digest",
                        },
                        tenant_id=tenant_id,
                    )
                )
                topic_by_id[topic.topic_id] = topic
                mention = self.store.upsert_topic_mention(
                    TopicMention(
                        topic_mention_id=_topic_mention_stable_id(
                            tenant_id=tenant_id,
                            owner_user_id=owner_user_id,
                            topic_id=topic.topic_id,
                            source_item_id=item.source_item_id,
                            artifact_id=str(candidate.get("artifact_id") or item.source_item_id),
                        ),
                        topic_id=topic.topic_id,
                        owner_user_id=owner_user_id,
                        source_item_id=item.source_item_id,
                        document_id=str(candidate.get("document_id") or "") or None,
                        chunk_id=str(candidate.get("chunk_id") or "") or None,
                        artifact_type=str(candidate.get("artifact_type") or "source_item"),
                        artifact_id=str(candidate.get("artifact_id") or item.source_item_id),
                        mention_text=str(candidate.get("mention_text") or ""),
                        confidence=float(candidate.get("confidence") or 0.0),
                        producer="pska.linking_digest",
                        metadata={
                            "source_title": getattr(item, "title", ""),
                            "run_type": "linking_digest",
                            "quality_tier": quality_tier,
                            "support_kinds": support_kinds,
                            "support_artifacts": support_artifacts[:12],
                            "source_refs": source_refs,
                            "promotion_reason": promotion_reason,
                            "review_eligible": review_eligible,
                        },
                        tenant_id=tenant_id,
                    )
                )
                mentions_by_topic.setdefault(topic.topic_id, []).append(mention)
                topic_support_id = _artifact_support_stable_id(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    artifact_type="topic",
                    artifact_id=topic.topic_id,
                    support_type="topic_mention",
                    source_item_id=item.source_item_id,
                    chunk_id=mention.chunk_id or "",
                )
                mention.metadata["artifact_support_id"] = topic_support_id
                self.store.upsert_artifact_support(
                    ArtifactSupport(
                        artifact_support_id=topic_support_id,
                        owner_user_id=owner_user_id,
                        artifact_type="topic",
                        artifact_id=topic.topic_id,
                        support_type="topic_mention",
                        source_item_id=item.source_item_id,
                        document_id=mention.document_id,
                        chunk_id=mention.chunk_id,
                        topic_id=topic.topic_id,
                        confidence=mention.confidence,
                        metadata={
                            "topic_mention_id": mention.topic_mention_id,
                            "quality_tier": quality_tier,
                            "support_kinds": support_kinds,
                            "support_artifacts": support_artifacts[:12],
                            "promotion_reason": promotion_reason,
                            "review_eligible": review_eligible,
                            "source_refs": source_refs,
                        },
                        tenant_id=tenant_id,
                    )
                )
                supports_written += 1

        review_items: list[ReviewItem] = []
        for topic_id, mentions in mentions_by_topic.items():
            eligible_mentions = [mention for mention in mentions if _topic_mention_review_eligible(mention)]
            source_refs = _topic_source_refs_from_mentions(eligible_mentions)
            if len({ref["source_item_id"] for ref in source_refs}) < 2:
                continue
            topic = topic_by_id.get(topic_id)
            if topic is None:
                continue
            support_ids = [
                _artifact_support_stable_id(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    artifact_type="topic",
                    artifact_id=topic.topic_id,
                    support_type="topic_mention",
                    source_item_id=str(ref.get("source_item_id") or ""),
                    chunk_id=str(ref.get("chunk_id") or ""),
                )
                for ref in source_refs
                if ref.get("source_item_id")
            ]
            support_artifacts = _dedupe_support_artifacts(
                [
                    artifact
                    for mention in eligible_mentions
                    for artifact in _list_of_dicts((getattr(mention, "metadata", {}) or {}).get("support_artifacts"))
                ]
            )
            claim_ids = sorted({str(item.get("artifact_id") or "") for item in support_artifacts if item.get("artifact_type") == "knowledge_claim" and item.get("artifact_id")})
            support_kinds = sorted(
                {
                    kind
                    for mention in eligible_mentions
                    for kind in _string_list((getattr(mention, "metadata", {}) or {}).get("support_kinds"))
                }
            )
            summary = f"{len(source_refs)} 个资料条目通过强支撑共同指向“{topic.label}”（{', '.join(support_kinds[:4]) or 'support'}），建议 Review 后决定是否写入长期图谱。"
            members = [
                {"entity_type": "topic", "label": topic.label, "role": "topic"},
                *[
                    {
                        "entity_type": "source_item",
                        "label": source_label_by_id.get(str(ref.get("source_item_id") or ""), str(ref.get("source_item_id") or "source")),
                        "role": "evidence_source",
                    }
                    for ref in source_refs[:8]
                    if ref.get("source_item_id")
                ],
            ]
            review_id = _linking_review_stable_id(tenant_id=tenant_id, owner_user_id=owner_user_id, topic_id=topic_id, source_refs=source_refs)
            review_item = ReviewItem(
                review_item_id=review_id,
                owner_user_id=owner_user_id,
                review_type=ReviewType.RELATIONSHIP_CANDIDATE,
                title=f"共享主题：{topic.label}",
                proposal={
                    "kind": "linking_digest_relationship",
                    "relationship": "shared_topic",
                    "relation_type": "shared_topic",
                    "topic_id": topic.topic_id,
                    "topic_label": topic.label,
                    "source_refs": source_refs,
                    "support_ids": support_ids,
                    "support_kinds": support_kinds,
                    "support_artifacts": support_artifacts,
                    "members": members,
                    "entity_ids": [],
                    "claim_ids": claim_ids,
                    "quality_tier": "strong",
                    "promotion_reason": "shared_strong_support",
                    "review_eligible": True,
                    "producer": "pska.linking_digest",
                    "confidence": min(0.9, max(0.55, sum(mention.confidence for mention in eligible_mentions) / max(1, len(eligible_mentions)))),
                    "evidence_text": summary,
                    "plain_text_summary": summary,
                },
                tenant_id=tenant_id,
            )
            self.store.add_review_item(review_item)
            review_items.append(review_item)
            for ref in source_refs:
                self.store.upsert_artifact_support(
                    ArtifactSupport(
                        artifact_support_id=_artifact_support_stable_id(
                            tenant_id=tenant_id,
                            owner_user_id=owner_user_id,
                            artifact_type="review_item",
                            artifact_id=review_id,
                            support_type="shared_topic_source",
                            source_item_id=ref["source_item_id"],
                            chunk_id=str(ref.get("chunk_id") or ""),
                        ),
                        owner_user_id=owner_user_id,
                        artifact_type="review_item",
                        artifact_id=review_id,
                        support_type="shared_topic_source",
                        source_item_id=ref["source_item_id"],
                        document_id=ref.get("document_id"),
                        chunk_id=ref.get("chunk_id"),
                        topic_id=topic.topic_id,
                        confidence=review_item.proposal["confidence"],
                        metadata={
                            "topic_label": topic.label,
                            "quality_tier": "strong",
                            "support_kinds": support_kinds,
                            "promotion_reason": "shared_strong_support",
                            "review_eligible": True,
                        },
                        tenant_id=tenant_id,
                    )
                )
                supports_written += 1

        return {
            "ok": True,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "run_type": "linking_digest",
            "source_item_count": len(source_items),
            "topic_count": len(topic_by_id),
            "topic_mention_count": sum(len(items) for items in mentions_by_topic.values()),
            "artifact_support_count": supports_written,
            "relationship_candidate_count": len(review_items),
            "topics": [_knowledge_topic_payload(topic, mentions=mentions_by_topic.get(topic.topic_id) or []) for topic in topic_by_id.values()],
            "review_items": to_jsonable(review_items),
            "notes": [
                "linking_digest is deterministic and domain-agnostic.",
                "Cross-document relationships are review candidates, not automatically approved long-term knowledge.",
            ],
        }

    def workspace_writer_suggest(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        selected_text = str(payload.get("selected_text") or "").strip()
        draft_text = str(payload.get("draft_text") or "").strip()
        instruction = str(payload.get("instruction") or "请基于 PSKA 证据给出中文写作建议。").strip()
        query = str(payload.get("query") or "").strip() or _workspace_writer_query(selected_text, draft_text, instruction)
        if not selected_text and not query:
            raise ValueError("selected_text or query is required")
        user_id = str(payload.get("user_id") or "user_primary")
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        represented_user_id = payload.get("represented_user_id")
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        response = self.retrieval.search(
            query,
            user,
            represented_user_id=represented_user_id,
            top_k=int(payload.get("top_k") or 5),
        )
        retrieval = _console_search_summary(to_jsonable(response))
        diagnostics = retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {}
        memory_context = retrieval.get("memory_context") or []
        profile_context = retrieval.get("profile_context") or []
        citations = retrieval.get("citations") or []
        graph_paths = retrieval.get("graph_paths") or []
        gaps = diagnostics.get("gaps") or []
        conflicts = diagnostics.get("conflicts") or []
        return {
            "ok": True,
            "mode": "writer_suggest",
            "read_only": True,
            "default_language": "zh",
            "does_not_mutate_memory_profile_graph": True,
            "query_context": {
                "query": query,
                "instruction": instruction,
                "selected_text": selected_text,
                "selected_text_preview": selected_text[:240],
                "draft_text_preview": draft_text[:240],
            },
            "suggestion": _workspace_writer_suggestion(
                selected_text=selected_text,
                instruction=instruction,
                citations=citations,
                graph_paths=graph_paths,
                memory_context=memory_context,
                profile_context=profile_context,
                gaps=gaps,
                conflicts=conflicts,
            ),
            "evidence": {
                "citations": citations,
                "source_refs": citations,
                "graph_paths": graph_paths,
                "memory_context": memory_context,
                "profile_context": profile_context,
                "gaps": gaps,
                "conflicts": conflicts,
            },
        }

    def workspace_writing_boards(
        self,
        *,
        owner_user_id: str | None = None,
        limit: int = 50,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        _, tenant_id, owner = _writing_request_scope(context, requested_owner_user_id=owner_user_id)
        boards = self.store.list_writing_boards(tenant_id=tenant_id, owner_user_id=owner, limit=limit)
        return {"ok": True, "boards": [_writing_board_payload(board) for board in boards]}

    def workspace_writing_create_board(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        title = str(payload.get("title") or "").strip() or "Untitled inquiry"
        goal = str(payload.get("goal") or "").strip()
        metadata = _writing_board_metadata_with_knowledge_scope(
            self.store,
            payload,
            tenant_id=tenant_id,
            owner_user_id=owner,
        )
        board = WritingBoard(
            board_id=str(payload.get("board_id") or f"wboard_{uuid4().hex}"),
            tenant_id=tenant_id,
            owner_user_id=owner,
            title=title,
            goal=goal,
            metadata=metadata,
        )
        created = self.store.create_writing_board(board)
        return {"ok": True, "board": _writing_board_payload(created)}

    def workspace_writing_board(self, board_id: str, *, context: RequestContext | None = None) -> dict[str, Any]:
        _, tenant_id, owner = _writing_request_scope(context)
        board = self.store.get_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner)
        nodes = self.store.list_writing_nodes(board_id, tenant_id=tenant_id, owner_user_id=owner)
        edges = self.store.list_writing_edges(board_id, tenant_id=tenant_id, owner_user_id=owner)
        return {
            "ok": True,
            "board": _writing_board_payload(board),
            "nodes": [_writing_node_payload(node) for node in nodes],
            "edges": [_writing_edge_payload(edge) for edge in edges],
        }

    def workspace_writing_update_board(self, board_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        board = self.store.update_writing_board(
            board_id,
            tenant_id=tenant_id,
            owner_user_id=owner,
            title=str(payload["title"]) if "title" in payload else None,
            goal=str(payload["goal"]) if "goal" in payload else None,
            metadata=_writing_board_metadata_with_knowledge_scope(
                self.store,
                payload,
                tenant_id=tenant_id,
                owner_user_id=owner,
            )
            if isinstance(payload.get("metadata"), dict) or _knowledge_base_ids_from_payload(payload)
            else None,
        )
        return {"ok": True, "board": _writing_board_payload(board)}

    def workspace_writing_delete_board(self, board_id: str, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        _, tenant_id, owner = _writing_request_scope(context, payload)
        self.store.delete_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner)
        return {"ok": True, "deleted": {"board_id": board_id}}

    def workspace_writing_create_node(self, board_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        node_type = _writing_node_type(str(payload.get("node_type") or payload.get("type") or "question"))
        node = WritingNode(
            node_id=str(payload.get("node_id") or f"wnode_{uuid4().hex}"),
            board_id=board_id,
            tenant_id=tenant_id,
            owner_user_id=owner,
            node_type=node_type,
            title=str(payload.get("title") or _writing_default_node_title(node_type)).strip(),
            body_markdown=str(payload.get("body_markdown") or payload.get("body") or ""),
            position=dict(payload.get("position") or {}),
            size=dict(payload.get("size") or {}),
            status=str(payload.get("status") or "idle"),
            source_refs=list(payload.get("source_refs") or []),
            citations=list(payload.get("citations") or []),
            quality_signals=dict(payload.get("quality_signals") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )
        created = self.store.upsert_writing_node(node)
        return {"ok": True, "node": _writing_node_payload(created)}

    def workspace_writing_update_node(self, board_id: str, node_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        if node_id not in {node.node_id for node in self.store.list_writing_nodes(board_id, tenant_id=tenant_id, owner_user_id=owner)}:
            raise KeyError(node_id)
        node = self.store.update_writing_node(
            node_id,
            tenant_id=tenant_id,
            owner_user_id=owner,
            title=str(payload["title"]) if "title" in payload else None,
            body_markdown=str(payload["body_markdown"]) if "body_markdown" in payload else None,
            position=dict(payload["position"]) if isinstance(payload.get("position"), dict) else None,
            size=dict(payload["size"]) if isinstance(payload.get("size"), dict) else None,
            status=str(payload["status"]) if "status" in payload else None,
            source_refs=list(payload["source_refs"]) if isinstance(payload.get("source_refs"), list) else None,
            citations=list(payload["citations"]) if isinstance(payload.get("citations"), list) else None,
            quality_signals=dict(payload["quality_signals"]) if isinstance(payload.get("quality_signals"), dict) else None,
            metadata=dict(payload["metadata"]) if isinstance(payload.get("metadata"), dict) else None,
        )
        if node.board_id != board_id:
            raise KeyError(node_id)
        return {"ok": True, "node": _writing_node_payload(node)}

    def workspace_writing_delete_node(self, board_id: str, node_id: str, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        _, tenant_id, owner = _writing_request_scope(context, payload)
        if node_id not in {node.node_id for node in self.store.list_writing_nodes(board_id, tenant_id=tenant_id, owner_user_id=owner)}:
            raise KeyError(node_id)
        self.store.delete_writing_node(node_id, tenant_id=tenant_id, owner_user_id=owner)
        return {"ok": True, "deleted": {"node_id": node_id}}

    def workspace_writing_create_edge(self, board_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        edge = WritingEdge(
            edge_id=str(payload.get("edge_id") or f"wedge_{uuid4().hex}"),
            board_id=board_id,
            tenant_id=tenant_id,
            owner_user_id=owner,
            source_node_id=str(payload.get("source_node_id") or payload.get("source") or ""),
            target_node_id=str(payload.get("target_node_id") or payload.get("target") or ""),
            edge_type=_writing_edge_type(str(payload.get("edge_type") or payload.get("type") or "raises")),
            label=str(payload.get("label") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )
        created = self.store.upsert_writing_edge(edge)
        return {"ok": True, "edge": _writing_edge_payload(created)}

    def workspace_writing_delete_edge(self, board_id: str, edge_id: str, payload: dict[str, Any] | None = None, context: RequestContext | None = None) -> dict[str, Any]:
        _, tenant_id, owner = _writing_request_scope(context, payload)
        if edge_id not in {edge.edge_id for edge in self.store.list_writing_edges(board_id, tenant_id=tenant_id, owner_user_id=owner)}:
            raise KeyError(edge_id)
        self.store.delete_writing_edge(edge_id, tenant_id=tenant_id, owner_user_id=owner)
        return {"ok": True, "deleted": {"edge_id": edge_id}}

    def workspace_writing_suggest_questions(self, board_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        board = self.store.get_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner)
        nodes = self.store.list_writing_nodes(board_id, tenant_id=tenant_id, owner_user_id=owner)
        focus_node_id = str(payload.get("node_id") or "")
        focus = next((node for node in nodes if node.node_id == focus_node_id), None)
        direction = str(payload.get("direction") or "followup")
        return {
            "ok": True,
            "board_id": board_id,
            "node_id": focus_node_id or None,
            "direction": direction,
            "persisted": False,
            "suggestions": _writing_question_suggestions(board, nodes, focus, direction=direction),
        }

    def workspace_writing_compose(self, board_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        board = self.store.get_writing_board(board_id, tenant_id=tenant_id, owner_user_id=owner)
        nodes = self.store.list_writing_nodes(board_id, tenant_id=tenant_id, owner_user_id=owner)
        node_by_id = {node.node_id: node for node in nodes}
        section_node_id = str(payload.get("section_node_id") or "")
        answer_node_ids = [str(value) for value in payload.get("answer_node_ids") or [] if str(value)]
        section = node_by_id.get(section_node_id)
        answer_nodes = [
            node
            for node_id in answer_node_ids
            if (node := node_by_id.get(node_id)) and node.node_type == "answer"
        ]
        return {
            "ok": True,
            "board_id": board_id,
            "section_node_id": section_node_id or None,
            "answer_node_ids": [node.node_id for node in answer_nodes],
            "draft_markdown": _writing_compose_markdown(board=board, section=section, answer_nodes=answer_nodes),
            "source_refs": _dedupe_writing_refs([ref for node in answer_nodes for ref in node.source_refs]),
            "citations": _dedupe_writing_refs([ref for node in answer_nodes for ref in node.citations]),
            "retrieval_used": False,
        }

    def workspace_evidence_brief_create(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload, tenant_id, owner = _writing_request_scope(context, payload)
        limit = max(1, min(int(payload.get("limit") or 12), 40))
        job_id = str(payload.get("job_id") or "").strip() or None
        digest_note_ids = set(_string_list(payload.get("digest_note_ids") or payload.get("digest_note_id")))
        claim_ids = set(_string_list(payload.get("knowledge_claim_ids") or payload.get("claim_ids") or payload.get("knowledge_claim_id")))
        review_item_ids = set(_string_list(payload.get("review_item_ids") or payload.get("review_item_id")))
        ask_run_ids = set(_string_list(payload.get("ask_run_ids") or payload.get("ask_run_id")))
        explicit_non_ask_selector = bool(job_id or digest_note_ids or claim_ids or review_item_ids)
        if ask_run_ids and not explicit_non_ask_selector:
            notes, claims, review_items = [], [], []
        else:
            notes, claims, review_items = _evidence_brief_artifacts(
                self.store,
                tenant_id=tenant_id,
                owner_user_id=owner,
                job_id=job_id,
                digest_note_ids=digest_note_ids,
                claim_ids=claim_ids,
                review_item_ids=review_item_ids,
                limit=limit,
            )
        ask_runs = _evidence_brief_ask_runs(self.store, tenant_id=tenant_id, owner_user_id=owner, ask_run_ids=ask_run_ids, limit=limit)
        artifacts = [*notes, *claims, *review_items, *ask_runs]
        if not artifacts:
            return _evidence_brief_unavailable(
                reason="missing_artifacts",
                error="Evidence Brief 需要至少一个 digest note、claim、review item、Ask run，或带输出的 job_id。",
                tenant_id=tenant_id,
                owner_user_id=owner,
            )
        refs = _evidence_brief_refs(self.store, tenant_id=tenant_id, owner_user_id=owner, artifacts=artifacts)
        warnings = _evidence_brief_warnings(notes, claims, review_items, ask_runs)
        active_refs = [ref for ref in refs if _source_ref_lifecycle_status(ref) == "active"]
        if not refs:
            return _evidence_brief_unavailable(
                reason="missing_source_refs",
                error="Evidence Brief 需要至少一个可追溯 source_ref/citation。",
                tenant_id=tenant_id,
                owner_user_id=owner,
                warnings=warnings,
            )
        if not active_refs:
            return _evidence_brief_unavailable(
                reason="stale_evidence",
                error="Evidence Brief 的证据来源已删除或失效，请先恢复资料或选择仍然 active 的证据。",
                tenant_id=tenant_id,
                owner_user_id=owner,
                warnings=warnings,
                source_refs=refs,
            )
        if ask_runs and len(artifacts) == len(ask_runs) and any(item.get("warning") == "insufficient_evidence" for item in warnings):
            return _evidence_brief_unavailable(
                reason="unsupported_answer",
                error="这轮 Ask 没有通过证据校验，不能生成 Evidence Brief。",
                tenant_id=tenant_id,
                owner_user_id=owner,
                warnings=warnings,
                source_refs=refs,
            )
        refs = active_refs
        title = str(payload.get("title") or _evidence_brief_title(notes, claims, review_items, ask_runs)).strip()
        lineage = _evidence_brief_lineage(
            job_id=job_id,
            notes=notes,
            claims=claims,
            review_items=review_items,
            ask_runs=ask_runs,
            source_refs=refs,
            warnings=warnings,
        )
        knowledge_base_lineage = _knowledge_base_lineage_from_source_refs(refs)
        board_metadata = {
            "kind": "evidence_wiki_brief",
            "status": "draft",
            "review_status": _evidence_brief_review_status(review_items),
            "lineage": lineage,
            **knowledge_base_lineage,
        }
        knowledge_base_ids = _string_list(knowledge_base_lineage.get("knowledge_base_ids"))
        if knowledge_base_ids:
            board_metadata["knowledge_base_scope"] = {
                "mode": "hard",
                "knowledge_base_ids": knowledge_base_ids,
                "source_item_count": len(_source_refs_source_item_ids(refs)),
            }
        board = self.store.create_writing_board(
            WritingBoard(
                board_id=str(payload.get("board_id") or f"wbrief_{uuid4().hex}"),
                tenant_id=tenant_id,
                owner_user_id=owner,
                title=title,
                goal=str(payload.get("goal") or "Evidence Wiki brief draft with citations and review lineage."),
                metadata=board_metadata,
            )
        )
        nodes, edges = _create_evidence_brief_writing_nodes(
            self.store,
            board=board,
            notes=notes,
            claims=claims,
            review_items=review_items,
            ask_runs=ask_runs,
            refs=refs,
            lineage=lineage,
        )
        return {
            "ok": True,
            "brief": {
                "board_id": board.board_id,
                "title": board.title,
                "status": "draft",
                "review_status": board.metadata.get("review_status"),
                "source_refs": refs,
                "lineage": lineage,
                "warnings": warnings,
                **knowledge_base_lineage,
            },
            "board": _writing_board_payload(board),
            "nodes": [_writing_node_payload(node) for node in nodes],
            "edges": [_writing_edge_payload(edge) for edge in edges],
        }

    def console_memory(self, *, owner_user_id: str = "user_primary", tenant_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        limit = max(0, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        memories = sorted(
            self.store.list_agent_memories(owner_user_id=owner_user_id, tenant_id=tenant_id),
            key=lambda memory: (
                memory.confidence,
                memory.last_verified_at.isoformat() if memory.last_verified_at else "",
                memory.agent_memory_id,
            ),
            reverse=True,
        )[:limit]
        profile_cards = sorted(
            self.store.list_profile_cards(owner_user_id=owner_user_id, tenant_id=tenant_id),
            key=lambda card: (card.confidence, card.profile_card_id),
            reverse=True,
        )[:limit]
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "read_only": True,
            "agent_memories": [_console_agent_memory(memory) for memory in memories],
            "profile_cards": [_console_profile_card(card) for card in profile_cards],
            "memory_count": len(memories),
            "profile_count": len(profile_cards),
            "limit": limit,
        }

    def console_jobs(self, *, tenant_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        limit = max(1, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        try:
            ready = self.ready()
        except Exception as exc:  # noqa: BLE001
            ready = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "checks": {}}
        stats = self.job_stats(tenant_id=tenant_id, limit=1000)["stats"]
        failed_jobs = [to_jsonable(job) for job in self.store.list_jobs(tenant_id=tenant_id, status="failed", limit=limit)]
        running_jobs = [to_jsonable(job) for job in self.store.list_jobs(tenant_id=tenant_id, status="running", limit=limit)]
        stale_running = stats.get("stale_running") or [
            _console_job_summary(job)
            for job in self.store.list_jobs(tenant_id=tenant_id, status="running", limit=limit)
            if _console_job_is_stale(job)
        ]
        issues = _console_ops_issues(ready, stats, failed_jobs, stale_running)
        commands = []
        for issue in issues:
            commands.extend(issue.get("recovery_commands") or [])
        common_commands = [
            "./scripts/pska service-check",
            "./scripts/pska local-daemon",
            "lsof -nP -iTCP:8765 -sTCP:LISTEN",
        ]
        return {
            "ok": bool(ready.get("ok")) and not any(issue.get("severity") == "critical" for issue in issues),
            "tenant_id": tenant_id,
            "requires_agentic_service_online": False,
            "read_only": True,
            "service_readiness": _console_service_readiness(ready),
            "worker_health": {
                "by_status": stats.get("by_status") or {},
                "by_type": stats.get("by_type") or {},
                "active_worker_ids": stats.get("active_worker_ids") or [],
                "running": int((stats.get("by_status") or {}).get("running") or 0),
                "failed": int((stats.get("by_status") or {}).get("failed") or 0),
                "stale_running_count": len(stale_running),
                "stale_running": stale_running[:limit],
            },
            "digest_backlog": stats.get("digest_backlog") or {},
            "recent_failed": failed_jobs[:limit],
            "running_jobs": running_jobs[:limit],
            "issues": issues,
            "recommended_recovery_commands": list(dict.fromkeys([*commands, *common_commands])),
            "notes": [
                "This page is read-only and does not run retry, cancel, or recovery actions.",
                "digest_via_fastreact backlog should be processed by the configured agentic service adapter, not the local PSKA worker.",
            ],
        }

    def console_sources(self, *, owner_user_id: str = "user_primary", tenant_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        limit = max(1, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        source_items = self.store.list_source_items(tenant_id=tenant_id)
        connector_states = self.store.list_connector_states(tenant_id=tenant_id, owner_user_id=owner_user_id)
        knowledge_sources = self.store.list_knowledge_sources(tenant_id=tenant_id, owner_user_id=owner_user_id)
        metrics = _connector_metrics(source_items, connector_states)
        recent_sources = _console_recent_sources(source_items, owner_user_id=owner_user_id, limit=limit)
        states = [_console_connector_state(state) for state in connector_states[:limit]]
        processing_spans = self.store.list_processing_spans(tenant_id=tenant_id, limit=max(limit * 6, 50))
        source_cards = []
        for source in knowledge_sources[:limit]:
            sync_runs = self.store.list_sync_runs(tenant_id=tenant_id, knowledge_source_id=source.knowledge_source_id, limit=3)
            latest_sync_run_id = sync_runs[0].sync_run_id if sync_runs else None
            source_spans = [
                span
                for span in processing_spans
                if span.knowledge_source_id == source.knowledge_source_id and (latest_sync_run_id is None or span.sync_run_id == latest_sync_run_id)
            ]
            source_cards.append(_console_knowledge_source(source, sync_runs, source_spans))
        files_roots = _console_knowledge_source_roots(source_cards) or _console_files_roots(states)
        input_sources = _console_input_sources(self.config, source_cards)
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "read_only": True,
            "source_counts": {
                "source_items": self.store.count_table("source_items", tenant_id=tenant_id),
                "documents": self.store.count_table("documents", tenant_id=tenant_id),
                "chunks": self.store.count_table("chunks", tenant_id=tenant_id),
                "processing_spans": self.store.count_table("processing_spans", tenant_id=tenant_id),
            },
            "source_channels": metrics.get("source_channels") or {},
            "knowledge_sources": {
                "source_count": len(knowledge_sources),
                "sources": source_cards,
            },
            "source_adapters": supported_source_adapters(),
            "recent_sources": recent_sources,
            "connector_state": {
                "state_count": len(connector_states),
                "enabled_state_count": len([state for state in connector_states if getattr(state, "enabled", False)]),
                "state_sync_status": metrics.get("state_sync_status") or {},
                "states": states,
            },
            "files": {
                "roots": files_roots,
                "configured": bool(files_roots),
                "recommended_commands": _console_files_commands(files_roots),
            },
            "input_sources": input_sources,
            "processing_spans": {
                "span_count": len(processing_spans),
                "spans": [_console_processing_span(span) for span in processing_spans],
            },
            "workspace": {
                "root": str(self.config.workspace.root.expanduser()),
                "excluded_paths": [str(path) for path in _workspace_excluded_paths(self.config)],
            },
            "recommended_commands": [
                "./scripts/pska knowledge-source list --owner-user-id user_primary",
                *_console_files_commands(files_roots),
            ],
            "notes": [
                "Knowledge Sources are the user-facing source of truth.",
                "PSKA input sources include local file roots and connector inboxes such as Twitter/X zip archives.",
                "Connector state is adapter runtime/debug metadata.",
            ],
        }

    def cleanup_knowledge_source(
        self,
        knowledge_source_id: str,
        payload: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _owner_user_id_for_write(payload, context)
        execute = bool(payload.get("execute", False))
        delete_knowledge_source = bool(payload.get("delete_knowledge_source", False))
        pause_knowledge_source = bool(payload.get("pause_knowledge_source", True))
        source = self.store.get_knowledge_source(knowledge_source_id)
        if source.owner_user_id != owner_user_id:
            raise PermissionError("knowledge source owner mismatch")
        source_root = _knowledge_source_root(source)
        protected = source_root in {str(root.expanduser().resolve(strict=False)) for root in self.config.files.roots}
        if execute and protected and not bool(payload.get("allow_configured_root_cleanup", False)):
            return {
                "ok": False,
                "dry_run": False,
                "protected": True,
                "error": "cleanup blocked because this knowledge source is configured in files.roots",
                "knowledge_source": _console_knowledge_source(
                    source,
                    self.store.list_sync_runs(knowledge_source_id=source.knowledge_source_id, limit=1),
                ),
                "root": source_root,
            }
        return _cleanup_knowledge_source_payload(
            self.store,
            source,
            execute=execute,
            delete_knowledge_source=delete_knowledge_source,
            pause_knowledge_source=pause_knowledge_source,
        )

    def schedule_digest(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _owner_user_id_for_write(payload, context)
        source_item_ids = _string_list(payload.get("source_item_ids"))
        knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
        scoped_source_item_ids = set(_string_list(context.scope.get("source_item_ids"))) if context and context.scope else set()
        force = bool(payload.get("force", False))
        limit = _batch_limit(payload.get("limit") or 20)
        batch_size = _batch_limit(payload.get("batch_size") or payload.get("limit") or 1)
        priority = int(payload.get("priority") or 0)
        max_attempts = int(payload.get("max_attempts") or 3)
        retry_backoff_seconds = int(payload.get("retry_backoff_seconds") or payload.get("backoff_seconds") or 60)
        quota = _digest_schedule_quota(self.store, owner_user_id=owner_user_id, tenant_id=tenant_id, payload=payload, force=force)
        if quota["limited"]:
            return {
                "job": None,
                "owner_user_id": owner_user_id,
                "tenant_id": tenant_id,
                "scheduled_source_item_ids": [],
                "skipped_source_item_ids": [],
                "selected_source_items": [],
                "skipped_source_items": [],
                "force": force,
                "limit": limit,
                "batch_size": batch_size,
                "policy": _digest_budget_policy(limit=limit, batch_size=batch_size, force=force),
                "quota": quota,
                "quota_limited": True,
            }

        source_items = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == owner_user_id and _is_active_lifecycle(item)]
        if source_item_ids:
            requested = set(source_item_ids)
            source_items = [item for item in source_items if item.source_item_id in requested]
        if scoped_source_item_ids:
            source_items = [item for item in source_items if item.source_item_id in scoped_source_item_ids]
        source_items = sorted(source_items, key=lambda item: (item.created_at, item.source_item_id), reverse=True)

        coverage = {} if force else _digest_source_coverage(self.store, tenant_id=tenant_id)
        current_coverage = {
            item.source_item_id: covered
            for item in source_items
            if (covered := coverage.get(item.source_item_id)) and _digest_coverage_is_current(item, covered)
        }
        skipped_items = [
            _digest_source_explanation(item, selected=False, reason=current_coverage[item.source_item_id]["reason"], job=current_coverage[item.source_item_id]["job"])
            for item in source_items
            if item.source_item_id in current_coverage
        ]
        eligible = [item for item in source_items if force or item.source_item_id not in current_coverage]
        selected = eligible[:limit]
        selected_items = [
            _digest_source_explanation(
                item,
                selected=True,
                reason=_digest_selection_reason(item, coverage, force=force),
                job=(coverage.get(item.source_item_id) or {}).get("job") if not force else None,
            )
            for item in selected
        ]
        if len(eligible) > limit:
            skipped_items.extend(
                _digest_source_explanation(item, selected=False, reason="limit_reached", job=None)
                for item in eligible[limit:]
            )
        source_refs = [{"source_item_id": item.source_item_id} for item in selected]

        job = None
        if source_refs:
            prompt_lineage = _prompt_profile_lineage(self.store, tenant_id=tenant_id, owner_user_id=owner_user_id, profile_type="digest")
            job_scope: dict[str, Any] = {"source_item_ids": [ref["source_item_id"] for ref in source_refs]}
            if knowledge_base_ids:
                job_scope["knowledge_base_ids"] = knowledge_base_ids
            job_payload: dict[str, Any] = {
                "owner_user_id": owner_user_id,
                "tenant_id": tenant_id,
                "batch_size": batch_size,
                "retry_backoff_seconds": retry_backoff_seconds,
                "source_refs": source_refs,
                "scope": job_scope,
                "triggered_by": str(payload.get("triggered_by") or payload.get("actor_user_id") or owner_user_id),
                "producer": "pska.digest_scheduler",
                **prompt_lineage,
            }
            if payload.get("reason"):
                job_payload["reason"] = str(payload["reason"])
            job = self.jobs.submit(DIGEST_VIA_FASTREACT, job_payload, max_attempts=max_attempts, priority=priority)
            self.store.add_job_event(
                job.job_id,
                "digest_scheduled",
                "Scheduled digest job from source backlog",
                {
                    "owner_user_id": owner_user_id,
                    "tenant_id": tenant_id,
                    "source_item_count": len(source_refs),
                    "force": force,
                    "priority": priority,
                    "policy": _digest_budget_policy(limit=limit, batch_size=batch_size, force=force),
                    "selected_source_items": selected_items,
                    "skipped_source_items": skipped_items[:20],
                },
            )

        return {
            "job": to_jsonable(job) if job else None,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "scheduled_source_item_ids": [ref["source_item_id"] for ref in source_refs],
            "skipped_source_item_ids": [item["source_item_id"] for item in skipped_items],
            "selected_source_items": selected_items,
            "skipped_source_items": skipped_items,
            "force": force,
            "limit": limit,
            "batch_size": batch_size,
            "policy": _digest_budget_policy(limit=limit, batch_size=batch_size, force=force),
            "quota": quota,
            "quota_limited": False,
        }

    def job_context(
        self,
        job_id: str,
        context: RequestContext | None = None,
        *,
        cursor: str | int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        tenant_id = str(job.tenant_id or DEFAULT_TENANT_ID)
        requested_tenant_id = _tenant_id_for_request(context) if context else tenant_id
        if requested_tenant_id != tenant_id:
            raise PermissionError("job tenant mismatch")
        user_id = context.effective_user_id if context else str(job.payload.get("owner_user_id") or "user_primary")
        represented_user_id = context.represented_user_id if context else None
        allowed_owner_id = represented_user_id or user_id
        source_item_ids = _job_source_item_ids(job)
        candidate_items = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == allowed_owner_id and _is_active_lifecycle(item)]
        if source_item_ids:
            candidate_items = [item for item in candidate_items if item.source_item_id in source_item_ids]
        candidate_items = sorted(candidate_items, key=lambda item: (item.created_at, item.source_item_id))
        offset = _cursor_offset(cursor)
        batch_size = _batch_limit(limit if limit is not None else (job.payload.get("batch_size") if isinstance(job.payload, dict) else None))
        source_items = candidate_items[offset : offset + batch_size]
        next_offset = offset + len(source_items)
        has_more = next_offset < len(candidate_items)
        source_ids = {item.source_item_id for item in source_items}
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        passage_windows = _passage_windows_for_documents(documents, chunks)
        entities = [entity for entity in self.store.list_entities(tenant_id=tenant_id) if entity.owner_user_id == allowed_owner_id]
        memories = self.store.list_agent_memories(owner_user_id=allowed_owner_id, tenant_id=tenant_id)
        return {
            "job": to_jsonable(job),
            "tenant_id": tenant_id,
            "request_user_id": allowed_owner_id,
            "source_items": to_jsonable(source_items),
            "documents": to_jsonable(documents),
            "passage_windows": to_jsonable(passage_windows),
            "chunks": to_jsonable(chunks),
            "knowledge_claims": to_jsonable(self.store.list_knowledge_claims(owner_user_id=allowed_owner_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=100)),
            "digest_notes": to_jsonable(self.store.list_digest_notes(owner_user_id=allowed_owner_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=20)),
            "agent_memories": to_jsonable(memories[:20]),
            "entities": to_jsonable(entities[:50]),
            "context_policy": {
                "input_strategy": "document_first",
                "passage_window_policy": "full_document_until_budget_then_paragraph_window",
                "target_window_tokens": 24000,
                "chunks_role": "retrieval_slices_compatibility",
                "token_estimate": sum(window.token_estimate for window in passage_windows),
            },
            "cursor": str(offset),
            "next_cursor": str(next_offset) if has_more else None,
            "has_more": has_more,
            "batch_size": batch_size,
            "total_source_items": len(candidate_items),
        }

    def lease_job(self, job_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        worker_id = str(payload.get("worker_id") or (context.effective_user_id if context else "pska-worker-http"))
        lease_seconds = int(payload.get("lease_seconds") or 300)
        job = self.store.lease_job(job_id, worker_id=worker_id, lease_seconds=lease_seconds)
        context_payload = self.job_context(job.job_id, context=context)
        return {
            "job": to_jsonable(job),
            "context": context_payload,
            "allowed_tools": _allowed_tools_for_job(job),
            "lease_seconds": lease_seconds,
        }

    def complete_job(self, job_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        _assert_job_context_tenant(self.store.get_job(job_id), context)
        result = dict(payload.get("result") or {})
        if payload.get("summary") is not None:
            result.setdefault("summary", payload.get("summary"))
        return {"job": to_jsonable(self.store.finish_job(job_id, result))}

    def fail_job(self, job_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        _assert_job_context_tenant(self.store.get_job(job_id), context)
        error = str(payload.get("error") or "job failed")
        retryable = bool(payload.get("retryable", True))
        return {"job": to_jsonable(self.store.fail_job(job_id, error, retryable=retryable))}

    def retry_job(self, job_id: str, context: RequestContext | None = None) -> dict[str, Any]:
        _assert_job_context_tenant(self.store.get_job(job_id), context)
        return {"job": to_jsonable(self.store.retry_job(job_id))}

    def cancel_job(self, job_id: str, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        _assert_job_context_tenant(self.store.get_job(job_id), context)
        return {"job": to_jsonable(self.store.cancel_job(job_id, reason=str(payload.get("reason") or "")))}

    def recover_jobs(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        jobs = self.store.recover_stale_jobs(
            tenant_id=str(payload.get("tenant_id") or DEFAULT_TENANT_ID),
            max_age_seconds=int(payload.get("max_age_seconds") or 3600),
        )
        return {"recovered": to_jsonable(jobs)}


class PSKARequestHandler(BaseHTTPRequestHandler):
    api: PSKAApi

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except KeyError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=getattr(self, "path", ""), payload={})
            self._json(404, {"error": f"not found: {exc}"})
        except PermissionError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=getattr(self, "path", ""), payload={})
            self._json(403, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        self._begin_request(path=path, payload={})
        if path in {"/console", "/console/"}:
            return self._asset(200, _CONSOLE_HTML, "text/html; charset=utf-8")
        if path in {"/console/reviews", "/console/reviews/"}:
            return self._asset(200, _CONSOLE_REVIEWS_HTML, "text/html; charset=utf-8")
        if path in {"/console/search", "/console/search/"}:
            return self._asset(200, _CONSOLE_SEARCH_HTML, "text/html; charset=utf-8")
        if path in {"/console/memory", "/console/memory/"}:
            return self._asset(200, _CONSOLE_MEMORY_HTML, "text/html; charset=utf-8")
        if path in {"/console/jobs", "/console/jobs/", "/console/ops", "/console/ops/"}:
            return self._asset(200, _CONSOLE_JOBS_HTML, "text/html; charset=utf-8")
        if path in {"/console/sources", "/console/sources/"}:
            return self._asset(200, _CONSOLE_SOURCES_HTML, "text/html; charset=utf-8")
        if path in {"/workspace", "/workspace/", "/app", "/app/"}:
            return self._asset(200, _WORKSPACE_HTML, "text/html; charset=utf-8")
        if path == "/console/app.css":
            return self._asset(200, _CONSOLE_CSS, "text/css; charset=utf-8")
        if path == "/workspace/app.css":
            return self._asset(200, _WORKSPACE_CSS, "text/css; charset=utf-8")
        if path == "/console/app.js":
            return self._asset(200, _CONSOLE_JS, "text/javascript; charset=utf-8")
        if path == "/workspace/app.js":
            return self._asset(200, _WORKSPACE_JS, "text/javascript; charset=utf-8")
        if path == "/console/reviews.js":
            return self._asset(200, _CONSOLE_REVIEWS_JS, "text/javascript; charset=utf-8")
        if path == "/console/search.js":
            return self._asset(200, _CONSOLE_SEARCH_JS, "text/javascript; charset=utf-8")
        if path == "/console/memory.js":
            return self._asset(200, _CONSOLE_MEMORY_JS, "text/javascript; charset=utf-8")
        if path == "/console/jobs.js":
            return self._asset(200, _CONSOLE_JOBS_JS, "text/javascript; charset=utf-8")
        if path == "/console/sources.js":
            return self._asset(200, _CONSOLE_SOURCES_JS, "text/javascript; charset=utf-8")
        if path == "/health":
            return self._json(200, self.api.health())
        context = self._context({})
        if context is None:
            return
        if path == "/ready":
            return self._json(200, self.api.ready())
        if path == "/workspace/readiness":
            return self._json(200, self.api.workspace_readiness(context=context))
        if path == "/workspace/prompt-profiles":
            return self._json(200, self.api.workspace_prompt_profiles({"owner_user_id": _first(query.get("owner_user_id"))}, context=context))
        if path == "/workspace/prompt-profiles/effective":
            prompt_profiles = self.api.workspace_prompt_profiles({"owner_user_id": _first(query.get("owner_user_id"))}, context=context)
            return self._json(
                200,
                {
                    "ok": True,
                    "tenant_id": prompt_profiles.get("tenant_id"),
                    "owner_user_id": prompt_profiles.get("owner_user_id"),
                    "effective": prompt_profiles.get("effective") or {},
                },
            )
        if path == "/workspace/knowledge-bases":
            return self._json(
                200,
                self.api.workspace_knowledge_bases(
                    {
                        "owner_user_id": _first(query.get("owner_user_id")),
                        "default_space_id": _first(query.get("default_space_id")),
                        "space_id": _first(query.get("space_id")),
                        "include_deleted": (_first(query.get("include_deleted")) or "false").lower() == "true",
                        "include_archived": (_first(query.get("include_archived")) or "false").lower() == "true",
                    },
                    context=context,
                ),
            )
        if path.startswith("/workspace/knowledge-bases/"):
            parts = _knowledge_base_path_parts(path)
            if len(parts) == 1:
                return self._json(
                    200,
                    self.api.workspace_knowledge_base(
                        unquote(parts[0]),
                        {"owner_user_id": _first(query.get("owner_user_id"))},
                        context=context,
                    ),
                )
        if path == "/workspace/documents/data":
            return self._json(
                200,
                self.api.workspace_documents_data(
                    {
                        "owner_user_id": _first(query.get("owner_user_id")),
                        "knowledge_base_ids": _query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                        "include_deleted": (_first(query.get("include_deleted")) or "true").lower() != "false",
                        "limit": _int_first(query.get("limit")) or 100,
                    },
                    context=context,
                ),
            )
        if path == "/workspace/reader/source":
            return self._json(
                200,
                self.api.workspace_reader_source(
                    {
                        "owner_user_id": _first(query.get("owner_user_id")),
                        "source_item_id": _first(query.get("source_item_id")),
                        "knowledge_base_ids": _query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                        "max_document_chars": _int_first(query.get("max_document_chars")) or 60000,
                    },
                    context=context,
                ),
            )
        if path == "/workspace/ask/conversations":
            return self._json(200, self.api.workspace_ask_conversations({"limit": _int_first(query.get("limit")) or 50}, context=context))
        if path.startswith("/workspace/ask/conversations/"):
            parts = _ask_conversation_path_parts(path)
            if len(parts) == 1:
                return self._json(200, self.api.workspace_ask_conversation(unquote(parts[0]), context=context))
        if path == "/workspace/sources/adapters":
            return self._json(200, {"ok": True, "adapters": supported_source_adapters()})
        if path == "/index-status":
            return self._json(200, self.api.index_status())
        if path == "/metrics":
            return self._json(200, self.api.metrics())
        if path == "/console/data":
            return self._json(
                200,
                self.api.console_dashboard(
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    tenant_id=context.tenant_id,
                    limit=_int_first(query.get("limit")) or 5,
                ),
            )
        if path == "/console/reviews/data":
            return self._json(
                200,
                self.api.console_reviews(
                    status=_first(query.get("status")) or "pending",
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    tenant_id=context.tenant_id,
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    limit=_int_first(query.get("limit")) or 50,
                ),
            )
        if path == "/console/memory/data":
            return self._json(
                200,
                self.api.console_memory(
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    tenant_id=context.tenant_id,
                    limit=_int_first(query.get("limit")) or 50,
                ),
            )
        if path == "/console/jobs/data":
            return self._json(200, self.api.console_jobs(tenant_id=context.tenant_id, limit=_int_first(query.get("limit")) or 20))
        if path == "/digest/logs":
            return self._json(
                200,
                self.api.digest_logs(
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    tenant_id=context.tenant_id,
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    limit=_int_first(query.get("limit")) or 10,
                ),
            )
        if path == "/workspace/digest/data":
            return self._json(
                200,
                self.api.workspace_digest_data(
                    owner_user_id=_first(query.get("owner_user_id")),
                    tenant_id=context.tenant_id,
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    limit=_int_first(query.get("limit")) or 50,
                    context=context,
                ),
            )
        if path == "/console/sources/data":
            return self._json(
                200,
                self.api.console_sources(
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    tenant_id=context.tenant_id,
                    limit=_int_first(query.get("limit")) or 20,
                ),
            )
        if path == "/workspace/today/data":
            return self._json(
                200,
                self.api.workspace_today(
                    owner_user_id=_first(query.get("owner_user_id")),
                    limit=_int_first(query.get("limit")) or 10,
                    context=context,
                ),
            )
        if path == "/workspace/activity/data":
            return self._json(
                200,
                self.api.workspace_activity(
                    owner_user_id=_first(query.get("owner_user_id")),
                    limit=_int_first(query.get("limit")) or 50,
                    context=context,
                ),
            )
        if path == "/workspace/discoveries/data":
            return self._json(
                200,
                self.api.workspace_discoveries(
                    owner_user_id=_first(query.get("owner_user_id")),
                    limit=_int_first(query.get("limit")) or 50,
                    min_score=_float_first(query.get("min_score"), DISCOVERY_TODAY_SCORE_THRESHOLD),
                    context=context,
                ),
            )
        if path == "/workspace/corpus/data":
            return self._json(
                200,
                self.api.workspace_corpus(
                    owner_user_id=_first(query.get("owner_user_id")),
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    source_channel=_first(query.get("source_channel")),
                    query=_first(query.get("query")),
                    limit=_int_first(query.get("limit")) or 20,
                    context=context,
                ),
            )
        if path == "/workspace/graph/data":
            return self._json(
                200,
                self.api.workspace_graph_data(
                    owner_user_id=_first(query.get("owner_user_id")),
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    limit=_int_first(query.get("limit")) or 30,
                    node_types=_node_types_param(_first(query.get("node_types"))),
                    context=context,
                ),
            )
        if path == "/workspace/graph/topics":
            return self._json(
                200,
                self.api.workspace_graph_topics(
                    owner_user_id=_first(query.get("owner_user_id")),
                    query=_first(query.get("query")),
                    limit=_int_first(query.get("limit")) or 100,
                    context=context,
                ),
            )
        if path == "/workspace/graph/subgraph":
            return self._json(
                200,
                self.api.workspace_graph_subgraph(
                    node_id=_first(query.get("node_id")) or "",
                    owner_user_id=_first(query.get("owner_user_id")),
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    limit=_int_first(query.get("limit")) or 80,
                    hops=_int_first(query.get("hops")) or 1,
                    node_types=_node_types_param(_first(query.get("node_types"))),
                    context=context,
                ),
            )
        if path == "/workspace/graph/search-subgraph":
            return self._json(
                200,
                self.api.workspace_graph_search_subgraph(
                    query=_first(query.get("query")) or "",
                    owner_user_id=_first(query.get("owner_user_id")),
                    knowledge_base_ids=_query_string_list(query, "knowledge_base_ids") or _query_string_list(query, "knowledge_base_id"),
                    limit=_int_first(query.get("limit")) or 80,
                    hops=_int_first(query.get("hops")) or 1,
                    top_k=_int_first(query.get("top_k")) or 5,
                    node_types=_node_types_param(_first(query.get("node_types"))),
                    context=context,
                ),
            )
        if path == "/workspace/graph/paths":
            return self._json(
                200,
                self.api.workspace_graph_paths(
                    query=_first(query.get("query")) or "",
                    owner_user_id=_first(query.get("owner_user_id")),
                    top_k=_int_first(query.get("top_k")) or 5,
                    context=context,
                ),
            )
        if path == "/workspace/graph/path":
            return self._json(
                200,
                self.api.workspace_graph_path(
                    query=_first(query.get("query")) or "",
                    owner_user_id=_first(query.get("owner_user_id")),
                    top_k=_int_first(query.get("top_k")) or 5,
                    mode=_first(query.get("mode")) or "agentic",
                    max_iterations=_int_first(query.get("max_iterations")) or 3,
                    context=context,
                ),
            )
        if path == "/workspace/writing/boards":
            return self._json(
                200,
                self.api.workspace_writing_boards(
                    owner_user_id=_first(query.get("owner_user_id")),
                    limit=_int_first(query.get("limit")) or 50,
                    context=context,
                ),
            )
        if path.startswith("/workspace/writing/boards/"):
            parts = _writing_path_parts(path)
            if len(parts) == 1:
                return self._json(200, self.api.workspace_writing_board(unquote(parts[0]), context=context))
        if path == "/review-items":
            return self._json(200, self.api.review_items(tenant_id=context.tenant_id))
        if path == "/connectors/states":
            return self._json(
                200,
                self.api.connector_states(
                    tenant_id=context.tenant_id,
                    owner_user_id=_first(query.get("owner_user_id")),
                    connector_id=_first(query.get("connector_id")),
                ),
            )
        if path.startswith("/connectors/states/"):
            return self._json(200, self.api.connector_states(tenant_id=context.tenant_id, connector_state_id=path.removeprefix("/connectors/states/")))
        if path == "/jobs/stats":
            return self._json(200, self.api.job_stats(tenant_id=context.tenant_id, limit=_int_first(query.get("limit")) or 1000))
        if path == "/jobs":
            return self._json(
                200,
                self.api.job_status(
                    status=_first(query.get("status")),
                    job_type=_first(query.get("job_type")),
                    tenant_id=context.tenant_id,
                    limit=_int_first(query.get("limit")) or 50,
                ),
            )
        if path.startswith("/jobs/") and path.endswith("/context"):
            job_id = path.removeprefix("/jobs/").removesuffix("/context")
            return self._json(200, self.api.job_context(job_id, context=context, cursor=_first(query.get("cursor")), limit=_int_first(query.get("limit"))))
        if path.startswith("/digest/batches/"):
            job_id = path.removeprefix("/digest/batches/")
            return self._json(200, self.api.job_context(job_id, context=context, cursor=_first(query.get("cursor")), limit=_int_first(query.get("limit"))))
        if path.startswith("/jobs/"):
            return self._json(200, self.api.job_status(path.removeprefix("/jobs/"), tenant_id=context.tenant_id))
        self._json(404, {"error": f"not found: {path}"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            self._begin_request(path=path, payload=payload)
            context = self._context(payload)
            if context is None:
                return
            if path == "/mcp":
                response = self.api.mcp_jsonrpc(payload, context=context)
                if response is None:
                    return self._empty(204)
                return self._json(200, response)
            if path == "/ingest/channel-payload":
                return self._json(200, self.api.ingest_payload(payload, context=context))
            if path == "/connectors/records":
                return self._json(200, self.api.ingest_connector_record(payload, context=context))
            if path == "/connectors/states":
                return self._json(200, self.api.upsert_connector_state(payload, context=context))
            if path.startswith("/connectors/states/"):
                payload.setdefault("connector_state_id", path.removeprefix("/connectors/states/"))
                return self._json(200, self.api.upsert_connector_state(payload, context=context))
            if path == "/search":
                return self._json(200, self.api.search(payload, context=context))
            if path == "/agentic-search":
                return self._json(200, self.api.agentic_search(payload, context=context))
            if path == "/console/search/query":
                return self._json(200, self.api.console_search(payload, context=context))
            if path == "/workspace/ask":
                return self._json(200, self.api.workspace_ask(payload, context=context))
            if path == "/workspace/ask/understand":
                return self._json(200, self.api.workspace_ask_understand(payload, context=context))
            if path == "/workspace/ask/stream":
                return self._sse_events(200, self.api.workspace_ask_event_stream(payload, context=context))
            if path == "/workspace/ask/conversations":
                return self._json(200, self.api.create_workspace_ask_conversation(payload, context=context))
            if path.startswith("/workspace/ask/conversations/"):
                parts = _ask_conversation_path_parts(path)
                if len(parts) == 3 and parts[1] == "messages" and parts[2] == "stream":
                    return self._sse_events(200, self.api.workspace_ask_conversation_event_stream(unquote(parts[0]), payload, context=context))
            if path == "/workspace/search/query":
                return self._json(200, self.api.workspace_search(payload, context=context))
            if path == "/workspace/knowledge-bases/search":
                return self._json(200, self.api.workspace_knowledge_base_search(payload, context=context))
            if path == "/workspace/chunking/preview":
                return self._json(200, self.api.chunking_preview(payload, context=context))
            if path == "/workspace/knowledge-bases":
                return self._json(200, self.api.create_workspace_knowledge_base(payload, context=context))
            if path.startswith("/workspace/knowledge-bases/"):
                parts = _knowledge_base_path_parts(path)
                if len(parts) == 2 and parts[1] == "restore":
                    return self._json(200, self.api.restore_workspace_knowledge_base(unquote(parts[0]), payload, context=context))
                if len(parts) == 2 and parts[1] == "pin":
                    return self._json(200, self.api.pin_workspace_knowledge_base(unquote(parts[0]), payload, context=context, pinned=True))
            if path == "/workspace/sources/preview":
                return self._json(200, self.api.source_preview(payload, context=context))
            if path == "/workspace/sources/text":
                return self._json(200, self.api.create_text_source(payload, context=context))
            if path == "/workspace/sources/upload":
                return self._json(200, self.api.create_upload_source(payload, context=context))
            if path == "/workspace/sources":
                return self._json(200, self.api.create_knowledge_source(payload, context=context))
            if path == "/workspace/sources/sync":
                return self._json(200, self.api.sync_knowledge_sources(payload, context=context))
            if path == "/workspace/documents/move":
                return self._json(200, self.api.workspace_documents_move(payload, context=context))
            if path in {"/workspace/documents/link", "/workspace/documents/memberships"}:
                return self._json(200, self.api.workspace_documents_link(payload, context=context))
            if path == "/workspace/documents/delete":
                return self._json(200, self.api.workspace_documents_delete(payload, context=context))
            if path == "/workspace/prompt-profiles":
                return self._json(200, self.api.update_workspace_prompt_profiles(payload, context=context))
            if path == "/workspace/digest/run":
                return self._json(200, self.api.workspace_digest_run(payload, context=context))
            if path == "/workspace/digest/linking/run":
                return self._json(200, self.api.workspace_digest_linking_run(payload, context=context))
            if path == "/workspace/evidence-briefs":
                return self._json(200, self.api.workspace_evidence_brief_create(payload, context=context))
            if path == "/workspace/writer/suggest":
                return self._json(200, self.api.workspace_writer_suggest(payload, context=context))
            if path == "/workspace/activity":
                return self._json(200, self.api.record_workspace_activity(payload, context=context))
            if path == "/workspace/writing/boards":
                return self._json(200, self.api.workspace_writing_create_board(payload, context=context))
            if path.startswith("/workspace/writing/boards/"):
                parts = _writing_path_parts(path)
                if len(parts) == 2 and parts[1] == "nodes":
                    return self._json(200, self.api.workspace_writing_create_node(unquote(parts[0]), payload, context=context))
                if len(parts) == 2 and parts[1] == "edges":
                    return self._json(200, self.api.workspace_writing_create_edge(unquote(parts[0]), payload, context=context))
                if len(parts) == 2 and parts[1] == "suggest-questions":
                    return self._json(200, self.api.workspace_writing_suggest_questions(unquote(parts[0]), payload, context=context))
                if len(parts) == 2 and parts[1] == "compose":
                    return self._json(200, self.api.workspace_writing_compose(unquote(parts[0]), payload, context=context))
            if path.startswith("/workspace/discoveries/") and path.endswith("/accept"):
                discovery_id = path.removeprefix("/workspace/discoveries/").removesuffix("/accept")
                return self._json(200, self.api.accept_discovery_item(discovery_id, payload))
            if path.startswith("/workspace/discoveries/") and path.endswith("/ignore"):
                discovery_id = path.removeprefix("/workspace/discoveries/").removesuffix("/ignore")
                return self._json(200, self.api.ignore_discovery_item(discovery_id, payload))
            if path.startswith("/workspace/discoveries/") and path.endswith("/snooze"):
                discovery_id = path.removeprefix("/workspace/discoveries/").removesuffix("/snooze")
                return self._json(200, self.api.snooze_discovery_item(discovery_id, payload))
            if path == "/extract/all":
                return self._json(200, self.api.extract_all(payload, context=context))
            if path == "/profile/update-proposals":
                return self._json(200, self.api.propose_profile_update(payload, context=context))
            if path == "/candidates":
                return self._json(200, self.api.write_candidates(payload, context=context))
            if path == "/digest/candidates":
                return self._json(200, self.api.write_candidates(payload, context=context))
            if path == "/files/sync":
                return self._json(200, self.api.files_sync(payload, context=context))
            if path.startswith("/knowledge-sources/") and path.endswith("/sync"):
                knowledge_source_id = path.removeprefix("/knowledge-sources/").removesuffix("/sync")
                payload["knowledge_source_ids"] = [knowledge_source_id]
                return self._json(200, self.api.sync_knowledge_sources(payload, context=context))
            if path.startswith("/knowledge-sources/") and path.endswith("/cleanup"):
                knowledge_source_id = path.removeprefix("/knowledge-sources/").removesuffix("/cleanup")
                return self._json(200, self.api.cleanup_knowledge_source(knowledge_source_id, payload, context=context))
            if path == "/digest/schedule":
                return self._json(200, self.api.schedule_digest(payload, context=context))
            if path == "/digest/now":
                return self._json(200, self.api.digest_now(payload, context=context))
            if path == "/jobs":
                return self._json(200, self.api.submit_job(payload, context=context))
            if path == "/jobs/run":
                return self._json(200, self.api.run_jobs(payload, context=context))
            if path == "/jobs/recover":
                return self._json(200, self.api.recover_jobs(payload, context=context))
            if path == "/jobs/recover-stale":
                return self._json(200, self.api.recover_jobs(payload, context=context))
            if path.startswith("/jobs/") and path.endswith("/lease"):
                job_id = path.removeprefix("/jobs/").removesuffix("/lease")
                return self._json(200, self.api.lease_job(job_id, payload, context=context))
            if path.startswith("/jobs/") and path.endswith("/complete"):
                job_id = path.removeprefix("/jobs/").removesuffix("/complete")
                return self._json(200, self.api.complete_job(job_id, payload, context=context))
            if path.startswith("/jobs/") and path.endswith("/fail"):
                job_id = path.removeprefix("/jobs/").removesuffix("/fail")
                return self._json(200, self.api.fail_job(job_id, payload, context=context))
            if path.startswith("/jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/jobs/").removesuffix("/cancel")
                return self._json(200, self.api.cancel_job(job_id, payload, context=context))
            if path.startswith("/review-items/") and path.endswith("/approve"):
                review_item_id = path.removeprefix("/review-items/").removesuffix("/approve")
                return self._json(200, self.api.approve_review_item(review_item_id, payload))
            if path.startswith("/review-items/") and path.endswith("/reject"):
                review_item_id = path.removeprefix("/review-items/").removesuffix("/reject")
                return self._json(200, self.api.reject_review_item(review_item_id, payload))
            if path.startswith("/review-items/") and path.endswith("/apply"):
                review_item_id = path.removeprefix("/review-items/").removesuffix("/apply")
                return self._json(200, self.api.apply_review_item(review_item_id, payload))
            if path.startswith("/jobs/") and path.endswith("/retry"):
                job_id = path.removeprefix("/jobs/").removesuffix("/retry")
                return self._json(200, self.api.retry_job(job_id, context=context))
            self._json(404, {"error": f"not found: {path}"})
        except KeyError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(404, {"error": f"not found: {exc}"})
        except PermissionError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(403, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - local API should report JSON errors.
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            self._begin_request(path=path, payload=payload)
            context = self._context(payload)
            if context is None:
                return
            if path.startswith("/workspace/knowledge-bases/"):
                parts = _knowledge_base_path_parts(path)
                if len(parts) == 1:
                    return self._json(200, self.api.update_workspace_knowledge_base(unquote(parts[0]), payload, context=context))
            if path.startswith("/workspace/writing/boards/"):
                parts = _writing_path_parts(path)
                if len(parts) == 1:
                    return self._json(200, self.api.workspace_writing_update_board(unquote(parts[0]), payload, context=context))
                if len(parts) == 3 and parts[1] == "nodes":
                    return self._json(200, self.api.workspace_writing_update_node(unquote(parts[0]), unquote(parts[2]), payload, context=context))
            self._json(404, {"error": f"not found: {path}"})
        except KeyError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(404, {"error": f"not found: {exc}"})
        except PermissionError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(403, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - local API should report JSON errors.
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            self._begin_request(path=path, payload=payload)
            context = self._context(payload)
            if context is None:
                return
            if path.startswith("/workspace/knowledge-bases/"):
                parts = _knowledge_base_path_parts(path)
                if len(parts) == 2 and parts[1] == "pin":
                    return self._json(200, self.api.pin_workspace_knowledge_base(unquote(parts[0]), payload, context=context, pinned=False))
                if len(parts) == 1:
                    return self._json(200, self.api.delete_workspace_knowledge_base(unquote(parts[0]), payload, context=context))
            if path.startswith("/workspace/ask/conversations/"):
                parts = _ask_conversation_path_parts(path)
                if len(parts) == 1:
                    return self._json(200, self.api.delete_workspace_ask_conversation(unquote(parts[0]), payload, context=context))
            if path.startswith("/workspace/writing/boards/"):
                parts = _writing_path_parts(path)
                if len(parts) == 1:
                    return self._json(200, self.api.workspace_writing_delete_board(unquote(parts[0]), payload, context=context))
                if len(parts) == 3 and parts[1] == "nodes":
                    return self._json(200, self.api.workspace_writing_delete_node(unquote(parts[0]), unquote(parts[2]), payload, context=context))
                if len(parts) == 3 and parts[1] == "edges":
                    return self._json(200, self.api.workspace_writing_delete_edge(unquote(parts[0]), unquote(parts[2]), payload, context=context))
            self._json(404, {"error": f"not found: {path}"})
        except KeyError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(404, {"error": f"not found: {exc}"})
        except PermissionError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(403, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - local API should report JSON errors.
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        content_type = self.headers.get("content-type") or ""
        if content_type.lower().startswith("multipart/form-data"):
            return _parse_multipart_payload(content_type, raw)
        return json.loads(raw.decode("utf-8"))

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            self._begin_request(path=path, payload=payload)
            context = self._context(payload)
            if context is None:
                return
            if path == "/workspace/prompt-profiles":
                return self._json(200, self.api.update_workspace_prompt_profiles(payload, context=context))
            self._json(404, {"error": f"not found: {path}"})
        except KeyError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(404, {"error": f"not found: {exc}"})
        except PermissionError as exc:
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(403, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - local API should report JSON errors.
            if not hasattr(self, "_request_meta"):
                self._begin_request(path=path, payload={})
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _context(self, payload: dict[str, Any]) -> RequestContext | None:
        config = getattr(self.api, "config", None)
        service_token = getattr(getattr(config, "service", ServiceConfig()), "service_token", None)
        auth_config = getattr(config, "auth", None)
        auth_mode = str(getattr(auth_config, "mode", "service_token") or "service_token").strip().lower()
        authenticated = False
        if auth_mode == "service_token":
            try:
                authenticated = authenticate_headers(self.headers, service_token)
            except AuthError as exc:
                self._json(401, {"error": str(exc)})
                return None
        if auth_mode == "service_token" and service_token_required(service_token) and not authenticated:
            self._json(401, {"error": "PSKA service token required"})
            return None
        try:
            context = context_from_headers(self.headers, payload, service_authenticated=authenticated, auth_config=auth_config)
        except AuthError as exc:
            self._json(401, {"error": str(exc)})
            return None
        self.api.ensure_context_identity(context)
        self._request_context = context
        return context

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        if not hasattr(self, "_request_meta"):
            self._begin_request(path=urlparse(self.path).path, payload={})
        self._request_meta.update(_response_metrics(payload))
        data = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("x-pska-request-id", self._request_id())
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self._log_request(status)

    def _sse(self, status: int, payload: dict[str, Any]) -> None:
        if not hasattr(self, "_request_meta"):
            self._begin_request(path=urlparse(self.path).path, payload={})
        self._request_meta.update(_response_metrics(payload))
        self.send_response(status)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-pska-request-id", self._request_id())
        self.end_headers()
        for event_name, event_payload in _ask_sse_events(payload):
            frame = f"event: {event_name}\ndata: {json.dumps(to_jsonable(event_payload), ensure_ascii=False)}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
        self._log_request(status)

    def _sse_events(self, status: int, events: Any) -> None:
        if not hasattr(self, "_request_meta"):
            self._begin_request(path=urlparse(self.path).path, payload={})
        self.send_response(status)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-pska-request-id", self._request_id())
        self.end_headers()
        self.wfile.flush()
        logged_status = status
        try:
            for event_name, event_payload in events:
                if event_name == "done" and isinstance(event_payload, dict):
                    self._request_meta.update(
                        _response_metrics(event_payload.get("result") if isinstance(event_payload.get("result"), dict) else event_payload)
                    )
                frame = f"event: {event_name}\ndata: {json.dumps(to_jsonable(event_payload), ensure_ascii=False)}\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        except BrokenPipeError:
            logged_status = 499
        except Exception as exc:  # noqa: BLE001 - SSE cannot switch back to JSON after headers.
            logged_status = 500
            frame = f"event: error\ndata: {json.dumps({'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                logged_status = 499
        self._log_request(logged_status)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("x-pska-request-id", self._request_id())
        self.send_header("content-length", "0")
        self.end_headers()
        self._log_request(status)

    def _asset(self, status: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("x-pska-request-id", self._request_id())
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self._log_request(status)

    def _begin_request(self, *, path: str, payload: dict[str, Any]) -> None:
        request_id = self.headers.get("X-PSKA-Request-Id") or self.headers.get("X-Request-Id") or f"req_{uuid4().hex}"
        self._request_meta = {
            "request_id": request_id,
            "method": self.command,
            "path": path,
            "started_at": time.monotonic(),
            **_request_refs(path, payload),
        }

    def _request_id(self) -> str:
        if not hasattr(self, "_request_meta"):
            self._begin_request(path=urlparse(self.path).path, payload={})
        return str(self._request_meta["request_id"])

    def _log_request(self, status: int) -> None:
        meta = dict(getattr(self, "_request_meta", {}))
        if meta.get("logged"):
            return
        context = getattr(self, "_request_context", None)
        duration_ms = None
        if meta.get("started_at") is not None:
            duration_ms = round((time.monotonic() - float(meta["started_at"])) * 1000, 2)
        record = {
            "event": "pska.http_request",
            "request_id": meta.get("request_id"),
            "method": meta.get("method"),
            "path": meta.get("path"),
            "status": status,
            "duration_ms": duration_ms,
            "caller": getattr(context, "caller", None),
            "user_id": getattr(context, "effective_user_id", None),
            "represented_user_id": getattr(context, "represented_user_id", None),
            "job_id": meta.get("job_id"),
            "source_item_ids_count": meta.get("source_item_ids_count", 0),
            "response_answer_chars": meta.get("response_answer_chars"),
            "response_event_count": meta.get("response_event_count"),
            "response_tool_call_count": meta.get("response_tool_call_count"),
            "response_display_mode": meta.get("response_display_mode"),
            "ask_quality_band": meta.get("ask_quality_band"),
            "ask_evidence_status": meta.get("ask_evidence_status"),
            "ask_report_readiness": meta.get("ask_report_readiness"),
            "ask_retrieval_owner": meta.get("ask_retrieval_owner"),
            "ask_selected_intent": meta.get("ask_selected_intent"),
            "ask_surface": meta.get("ask_surface"),
            "ask_fallback_from": meta.get("ask_fallback_from"),
            "ask_citation_count": meta.get("ask_citation_count"),
            "ask_source_ref_count": meta.get("ask_source_ref_count"),
            "ask_evidence_result_count": meta.get("ask_evidence_result_count"),
            "ask_graph_path_count": meta.get("ask_graph_path_count"),
            "ask_gap_count": meta.get("ask_gap_count"),
            "ask_conflict_count": meta.get("ask_conflict_count"),
            "ask_query_chars": meta.get("ask_query_chars"),
            "ask_total_ms": meta.get("ask_total_ms"),
            "ask_time_to_first_answer_ms": meta.get("ask_time_to_first_answer_ms"),
        }
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        self._request_meta["logged"] = True


def serve(host: str = "127.0.0.1", port: int = 8765, database_url: str | None = None, *, config: PSKAConfig | None = None) -> None:
    api = PSKAApi(database_url or (config.database.url if config else None), config=config)

    class Handler(PSKARequestHandler):
        pass

    Handler.api = api
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"PSKA Core listening on http://{host}:{port}")
    server.serve_forever()


def _source_refs_from_payload(value: Any) -> list[SourceRef]:
    if not isinstance(value, list):
        return []
    allowed_keys = set(SourceRef.__dataclass_fields__)
    return [
        SourceRef(**{key: item for key, item in ref.items() if key in allowed_keys})
        for ref in value
        if isinstance(ref, dict)
    ]


def _request_refs(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = None
    if path.startswith("/jobs/"):
        remainder = path.removeprefix("/jobs/")
        job_id = remainder.split("/", 1)[0] or None
    if not job_id:
        job_id = payload.get("job_id") or payload.get("pska_job_id")

    source_item_ids = set(_string_list(payload.get("source_item_ids")))
    source_refs = payload.get("source_refs")
    if isinstance(source_refs, list):
        for ref in source_refs:
            if isinstance(ref, dict) and ref.get("source_item_id"):
                source_item_ids.add(str(ref["source_item_id"]))
    scope = payload.get("scope")
    if isinstance(scope, dict):
        source_item_ids.update(_string_list(scope.get("source_item_ids")))

    return {
        "job_id": str(job_id) if job_id else None,
        "source_item_ids_count": len(source_item_ids),
    }


def _response_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    events = trace.get("events") if isinstance(trace.get("events"), list) else []
    tool_calls = trace.get("tool_calls") if isinstance(trace.get("tool_calls"), list) else []
    metrics = {
        "response_answer_chars": len(str(payload.get("answer") or "")),
        "response_event_count": trace.get("event_count") or len(events) or None,
        "response_tool_call_count": len(tool_calls) if tool_calls else None,
        "response_display_mode": payload.get("display_mode"),
    }
    quality = payload.get("quality_signals") if isinstance(payload.get("quality_signals"), dict) else {}
    if quality:
        metrics.update(
            {
                "ask_quality_band": quality.get("quality_band"),
                "ask_evidence_status": quality.get("evidence_status"),
                "ask_report_readiness": quality.get("report_readiness"),
                "ask_retrieval_owner": quality.get("retrieval_owner"),
                "ask_selected_intent": quality.get("selected_intent"),
                "ask_surface": quality.get("surface"),
                "ask_fallback_from": quality.get("fallback_from"),
                "ask_citation_count": quality.get("citation_count"),
                "ask_source_ref_count": quality.get("source_ref_count"),
                "ask_evidence_result_count": quality.get("evidence_result_count"),
                "ask_graph_path_count": quality.get("graph_path_count"),
                "ask_gap_count": quality.get("gap_count"),
                "ask_conflict_count": quality.get("conflict_count"),
                "ask_query_chars": quality.get("query_chars"),
                "ask_total_ms": quality.get("total_ms"),
                "ask_time_to_first_answer_ms": quality.get("time_to_first_answer_ms"),
            }
        )
    return metrics


def _digest_log_entry(job: Any, events: list[Any], claims: list[Any], notes: list[Any], source_ids: set[str], *, source_refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    candidate_summary = _job_candidate_summary(job, events)
    candidate_summary = _digest_candidate_summary_with_persisted(candidate_summary, claims=claims, notes=notes)
    latest_event = events[-1] if events else None
    lineage_refs = list(source_refs or [])
    for item in [*claims, *notes]:
        if isinstance(item, dict):
            lineage_refs.extend(_list_of_dicts(item.get("source_refs")))
        else:
            lineage_refs.extend(_source_refs_payload(getattr(item, "source_refs", [])))
    knowledge_base_lineage = _knowledge_base_lineage_from_source_refs(lineage_refs)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "worker_id": job.worker_id,
        "external_run_id": job.external_run_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "source_item_ids": sorted(source_ids),
        "source_item_count": len(source_ids),
        "source_refs": source_refs or [],
        **knowledge_base_lineage,
        "candidate_summary": candidate_summary,
        "knowledge_claims": claims,
        "digest_notes": notes,
        "latest_event": latest_event,
        "events": events,
        "timeline": [
            {
                "event_type": event.event_type,
                "message": event.message,
                "created_at": event.created_at,
                "detail": event.detail,
            }
            for event in events
        ],
    }


def _digest_logs_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    candidate_totals = {
        "knowledge_claims": 0,
        "digest_notes": 0,
        "hyperedges": 0,
        "review_items": 0,
        "saved_candidates": 0,
        "review_candidates": 0,
    }
    recent_claims: list[dict[str, Any]] = []
    recent_digest_notes: list[dict[str, Any]] = []
    latest_failure: dict[str, Any] | None = None
    for entry in entries:
        status = str(entry.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        summary = entry.get("candidate_summary") if isinstance(entry.get("candidate_summary"), dict) else {}
        for key in candidate_totals:
            candidate_totals[key] += int(summary.get(key) or 0)
        if status == "failed" and latest_failure is None:
            latest_failure = {
                "job_id": entry.get("job_id"),
                "error": entry.get("error"),
                "updated_at": entry.get("updated_at"),
            }
        for claim in _iter_mapping_or_objects(entry.get("knowledge_claims")):
            recent_claims.append(
                {
                    "statement": _object_value(claim, "statement"),
                    "claim_type": _object_value(claim, "claim_type"),
                    "confidence": _object_value(claim, "confidence"),
                    "job_id": entry.get("job_id"),
                }
            )
            if len(recent_claims) >= 5:
                break
        for note in _iter_mapping_or_objects(entry.get("digest_notes")):
            recent_digest_notes.append(
                {
                    "title": _object_value(note, "title"),
                    "synopsis": _object_value(note, "synopsis"),
                    "job_id": entry.get("job_id"),
                }
            )
            if len(recent_digest_notes) >= 5:
                break
    return {
        "status_counts": status_counts,
        "candidate_totals": candidate_totals,
        "recent_claims": recent_claims[:5],
        "recent_digest_notes": recent_digest_notes[:5],
        "latest_failure": latest_failure,
        "has_useful_output": bool(candidate_totals["knowledge_claims"] or candidate_totals["digest_notes"] or candidate_totals["saved_candidates"]),
    }


def _iter_mapping_or_objects(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _object_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _job_candidate_summary(job: Any, events: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    result = job.result if isinstance(job.result, dict) else {}
    candidate_write = result.get("candidate_write") if isinstance(result.get("candidate_write"), dict) else {}
    if candidate_write:
        summary.update(candidate_write)
    for event in events:
        if event.event_type != "candidates_written" or not isinstance(event.detail, dict):
            continue
        summary.update(event.detail)
    return {
        "entities": len(summary.get("entities") or []),
        "hyperedges": len(summary.get("hyperedges") or []),
        "knowledge_claims": len(summary.get("knowledge_claims") or []),
        "digest_notes": len(summary.get("digest_notes") or []),
        "review_items": len(summary.get("review_items") or []),
        "agent_memories": len(summary.get("agent_memories") or []),
        "profile_cards": len(summary.get("profile_cards") or []),
        "saved_candidates": int(summary.get("saved_candidates") or 0),
        "review_candidates": int(summary.get("review_candidates") or len(summary.get("review_items") or [])),
        "warnings": list(summary.get("warnings") or []),
    }


def _digest_candidate_summary_with_persisted(summary: dict[str, Any], *, claims: list[Any], notes: list[Any]) -> dict[str, Any]:
    persisted_claims = len(claims)
    persisted_notes = len(notes)
    if not persisted_claims and not persisted_notes:
        return summary
    enriched = dict(summary)
    enriched["knowledge_claims"] = max(int(enriched.get("knowledge_claims") or 0), persisted_claims)
    enriched["digest_notes"] = max(int(enriched.get("digest_notes") or 0), persisted_notes)
    enriched["persisted_candidate_counts"] = {
        "knowledge_claims": persisted_claims,
        "digest_notes": persisted_notes,
    }
    return enriched


def _api_config(api: Any) -> PSKAConfig:
    config = getattr(api, "config", None)
    if config is not None:
        return config
    config = PSKAConfig(database=DatabaseConfig(url=DEFAULT_DATABASE_URL))
    setattr(api, "config", config)
    return config


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _job_source_item_ids(job) -> set[str]:
    ids = {ref.source_item_id for ref in job.source_refs if ref.source_item_id}
    payload = job.payload if isinstance(job.payload, dict) else {}
    raw_ids = payload.get("source_item_ids")
    if isinstance(raw_ids, list):
        ids.update(str(item) for item in raw_ids if item)
    scope = payload.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("source_item_ids"), list):
        ids.update(str(item) for item in scope["source_item_ids"] if item)
    raw_refs = payload.get("source_refs")
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            if isinstance(ref, dict) and ref.get("source_item_id"):
                ids.add(str(ref["source_item_id"]))
    return ids


def _digest_source_coverage(store: PostgresKnowledgeStore, *, tenant_id: str | None = None) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    priority = {"queued": 0, "running": 1, "succeeded": 2, "failed": 3, "canceled": 4}
    for job in store.list_jobs(tenant_id=tenant_id, job_type=DIGEST_VIA_FASTREACT, limit=10000):
        reason = _digest_coverage_reason(job.status)
        for source_item_id in _job_source_item_ids(job):
            current = coverage.get(source_item_id)
            if current is None or priority.get(job.status, 99) < priority.get(current["job"].status, 99):
                coverage[source_item_id] = {"reason": reason, "job": job}
    return coverage


def _digest_coverage_is_current(source_item: Any, covered: dict[str, Any]) -> bool:
    job = covered["job"]
    if job.status in {"queued", "running"}:
        return True
    source_updated_at = _as_aware(getattr(source_item, "updated_at", None) or source_item.created_at)
    covered_at = _digest_coverage_time(job)
    return covered_at >= source_updated_at


def _digest_coverage_time(job: Any) -> datetime:
    for field in ("finished_at", "updated_at", "started_at", "created_at"):
        value = getattr(job, field, None)
        if value is not None:
            return _as_aware(value)
    return datetime.min.replace(tzinfo=UTC)


def _digest_selection_reason(source_item: Any, coverage: dict[str, dict[str, Any]], *, force: bool) -> str:
    if force:
        return "force_selected"
    covered = coverage.get(source_item.source_item_id)
    if covered and not _digest_coverage_is_current(source_item, covered):
        return "source_changed_since_last_digest"
    return "new_or_triggered_source"


def _digest_coverage_reason(status: str) -> str:
    if status in {"queued", "running"}:
        return "active_digest_job"
    if status == "succeeded":
        return "completed_digest_job"
    if status == "failed":
        return "failed_digest_job_requires_force_or_new_trigger"
    if status == "canceled":
        return "canceled_digest_job_requires_force_or_new_trigger"
    return "covered_by_digest_job"


def _digest_source_explanation(source_item: Any, *, selected: bool, reason: str, job: Any | None) -> dict[str, Any]:
    payload = {
        "source_item_id": source_item.source_item_id,
        "source_channel": source_item.source_channel,
        "title": source_item.title,
        "selected": selected,
        "reason": reason,
        "created_at": source_item.created_at,
        "updated_at": getattr(source_item, "updated_at", source_item.created_at),
    }
    if job is not None:
        payload["covering_job"] = {
            "job_id": job.job_id,
            "status": job.status,
            "job_type": job.job_type,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
        }
    return payload


def _digest_budget_policy(*, limit: int, batch_size: int, force: bool) -> dict[str, Any]:
    return {
        "dedupe": "skip active digest jobs; skip completed/failed/canceled jobs only when they are current for the source update timestamp unless force=true",
        "successful_source_repeat": "skip completed digest sources until force=true or the source changes after the completed job",
        "failed_source_repeat": "skip failed digest sources until force=true or the source changes after the failed attempt to avoid infinite retry loops",
        "frequency": "optional quota_window_seconds/max_jobs_per_window limits new jobs",
        "max_source_items": limit,
        "max_source_items_per_job": batch_size,
        "token_budget": "not enforced yet; digest worker owns model token limits",
        "trigger_policy": "new_or_explicit_source_ids; similarity/tag/entity triggers are reserved for a later policy revision",
        "force": force,
    }


def _digest_schedule_quota(store: PostgresKnowledgeStore, *, owner_user_id: str, tenant_id: str, payload: dict[str, Any], force: bool) -> dict[str, Any]:
    window_seconds = _optional_positive_int(payload.get("quota_window_seconds"))
    max_jobs = _optional_positive_int(payload.get("max_jobs_per_window"))
    if force or not window_seconds or not max_jobs:
        return {
            "enabled": False,
            "limited": False,
            "window_seconds": window_seconds,
            "max_jobs_per_window": max_jobs,
            "jobs_in_window": 0,
            "remaining_jobs": None,
        }
    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    jobs_in_window = 0
    for job in store.list_jobs(tenant_id=tenant_id, job_type=DIGEST_VIA_FASTREACT, limit=10000):
        job_payload = job.payload if isinstance(job.payload, dict) else {}
        if str(job_payload.get("owner_user_id") or "") != owner_user_id:
            continue
        if str(job_payload.get("tenant_id") or job.tenant_id or DEFAULT_TENANT_ID) != tenant_id:
            continue
        created_at = _as_aware(job.created_at)
        if created_at >= cutoff:
            jobs_in_window += 1
    remaining = max(max_jobs - jobs_in_window, 0)
    return {
        "enabled": True,
        "limited": jobs_in_window >= max_jobs,
        "window_seconds": window_seconds,
        "max_jobs_per_window": max_jobs,
        "jobs_in_window": jobs_in_window,
        "remaining_jobs": remaining,
    }


def _files_sync_twitter_archives(store: PostgresKnowledgeStore, config: PSKAConfig, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("skip_twitter_archives"):
        return {"ok": True, "enabled": False, "reason": "skip_twitter_archives", "imported": 0, "skipped": 0, "failed": []}
    tenant_id = str(payload.get("tenant_id") or config.files.tenant_id or DEFAULT_TENANT_ID)
    owner_user_id = str(payload.get("owner_user_id") or config.files.owner_user_id)
    user_sources = config.workspace.user_sources_dir(tenant_id, owner_user_id)
    input_dir = Path(str(payload.get("twitter_archive") or user_sources / "archives" / "twitter")).expanduser()
    archive_root = Path(str(payload.get("archive_root") or user_sources / "imports")).expanduser()
    if not input_dir.exists():
        return {
            "ok": True,
            "enabled": True,
            "input": str(input_dir),
            "archive_root": str(archive_root),
            "zip_count": 0,
            "imported": 0,
            "skipped": 0,
            "failed": [],
        }
    try:
        zip_count = len(list(input_dir.glob("*.zip")))
        result = TwitterZipImporter(
            store,
            archive_root=archive_root,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            space_id=str(payload.get("space_id") or config.files.space_id),
            visibility=Visibility(str(payload.get("visibility") or config.files.visibility)),
            visible_team_ids=[],
            embedding_provider=build_embedding_provider(EmbeddingConfig(provider="disabled")),
        ).import_directory(input_dir)
    except Exception as exc:  # noqa: BLE001 - surface import failures in the sync result.
        return {
            "ok": False,
            "enabled": True,
            "input": str(input_dir),
            "archive_root": str(archive_root),
            "zip_count": 0,
            "imported": 0,
            "skipped": 0,
            "failed": [{"input": str(input_dir), "error": f"{type(exc).__name__}: {exc}"}],
        }
    return {
        "ok": not result.failed,
        "enabled": True,
        "input": str(input_dir),
        "archive_root": str(archive_root),
        "zip_count": zip_count,
        "imported": int(result.imported or 0),
        "skipped": int(result.skipped or 0),
        "failed": result.failed,
        "result": to_jsonable(result),
    }


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _embedding_metrics(chunks: list[Any], config: PSKAConfig | None = None) -> dict[str, Any]:
    if config:
        runtime_config = config.embedding_runtime_config()
        configured_provider = runtime_config.provider
        configured_model = runtime_config.model
    else:
        configured_provider = os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled")
        configured_model = os.environ.get("PSKA_EMBEDDING_MODEL", "BAAI/bge-m3")
    total = len(chunks)
    any_embedding = 0
    current = 0
    by_provider_model: dict[str, int] = {}
    for chunk in chunks:
        metadata = getattr(chunk, "metadata", {}) or {}
        provider = metadata.get("embedding_provider")
        model = metadata.get("embedding_model")
        has_embedding = bool(getattr(chunk, "embedding", None)) or bool(provider and model)
        if not has_embedding:
            continue
        any_embedding += 1
        if provider and model:
            by_provider_model[f"{provider}:{model}"] = by_provider_model.get(f"{provider}:{model}", 0) + 1
        if provider == configured_provider and model == configured_model:
            current += 1
    return {
        "provider": configured_provider,
        "model": configured_model,
        "configured": configured_provider != "disabled",
        "total_chunks": total,
        "embedded_chunks": current,
        "any_embedding_chunks": any_embedding,
        "missing_chunks": max(total - current, 0),
        "coverage": (current / total) if total else 1.0,
        "any_embedding_coverage": (any_embedding / total) if total else 1.0,
        "by_provider_model": by_provider_model,
    }


def _connector_metrics(source_items: list[Any], connector_states: list[Any] | None = None) -> dict[str, Any]:
    channels: dict[str, dict[str, Any]] = {}
    for item in source_items:
        channel = str(getattr(item, "source_channel", "") or "unknown")
        current = channels.setdefault(
            channel,
            {
                "source_items": 0,
                "latest_source_item_id": None,
                "latest_source_item_at": None,
            },
        )
        current["source_items"] += 1
        created_at = getattr(item, "created_at", None)
        latest = current["latest_source_item_at"]
        if latest is None or (created_at is not None and created_at > latest):
            current["latest_source_item_id"] = getattr(item, "source_item_id", None)
            current["latest_source_item_at"] = created_at
    states = connector_states or []
    by_status: dict[str, int] = {}
    for state in states:
        status = str(getattr(state, "sync_status", "unknown") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "source_channels": dict(sorted(channels.items())),
        "source_channel_count": len(channels),
        "total_source_items": len(source_items),
        "state_count": len(states),
        "enabled_state_count": len([state for state in states if getattr(state, "enabled", False)]),
        "state_sync_status": dict(sorted(by_status.items())),
    }


def _console_recent_sources(source_items: list[Any], *, owner_user_id: str, limit: int) -> list[dict[str, Any]]:
    filtered = [
        item
        for item in source_items
        if not owner_user_id or str(getattr(item, "owner_user_id", "")) == owner_user_id
    ]
    filtered.sort(key=lambda item: getattr(item, "created_at", datetime.min.replace(tzinfo=UTC)), reverse=True)
    return [
        {
            "source_item_id": getattr(item, "source_item_id", ""),
            "title": getattr(item, "title", "") or getattr(item, "source_id", ""),
            "source_channel": getattr(item, "source_channel", ""),
            "record_type": getattr(item, "record_type", ""),
            "created_at": getattr(item, "created_at", None),
        }
        for item in filtered[:limit]
    ]


def _console_review_items(items: list[dict[str, Any]], *, status: str, owner_user_id: str, limit: int, store: Any | None = None) -> list[dict[str, Any]]:
    matching = [
        _console_review_item(item, store=store)
        for item in items
        if (not status or item.get("status") == status)
        and (not owner_user_id or item.get("owner_user_id") == owner_user_id)
    ]
    return matching[: max(0, limit)]


def _console_review_item(item: dict[str, Any], *, store: Any | None = None) -> dict[str, Any]:
    proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
    review_type = _console_review_type(item.get("review_type"))
    source_refs = _console_review_source_refs(proposal)
    source_ref_status = "present" if source_refs else "missing"
    apply_supported = _console_review_apply_supported(review_type, proposal)
    apply_ready = apply_supported and _console_review_apply_ready(review_type, source_refs, proposal)
    status = str(item.get("status") or "")
    actions = ["approve", "reject"] if status == "pending" else []
    if status == "pending" and apply_ready:
        actions.insert(1, "approve_apply")
    if status == "approved" and apply_ready:
        actions = ["apply"]
    payload = {
        "review_item_id": item.get("review_item_id"),
        "owner_user_id": item.get("owner_user_id"),
        "review_type": review_type,
        "status": status,
        "title": item.get("title") or review_type,
        "confidence": _console_review_confidence(proposal),
        "source_refs": source_refs,
        "source_ref_status": source_ref_status,
        "proposal": proposal,
        "support_ids": _string_list(proposal.get("support_ids")),
        "support_kinds": _string_list(proposal.get("support_kinds")),
        "quality_tier": proposal.get("quality_tier"),
        "promotion_reason": proposal.get("promotion_reason"),
        "review_eligible": bool(proposal.get("review_eligible")),
        "created_at": item.get("created_at"),
        "recommended_action": _console_review_recommended_action(review_type, apply_ready=apply_ready),
        "recommended_actions": actions,
        "apply_supported": apply_supported,
        "apply_ready": apply_ready,
        "can_apply_now": status == "approved" and apply_ready,
    }
    application_result = _console_review_application_result(store, item) if store else None
    if application_result:
        payload["application_result"] = application_result
    return payload


def _console_review_application_result(store: Any, item: dict[str, Any]) -> dict[str, Any] | None:
    review_item_id = str(item.get("review_item_id") or "")
    if not review_item_id:
        return None
    try:
        review_item = store.get_review_item(review_item_id)
    except Exception:  # noqa: BLE001 - console data should still render if a stale item disappears.
        return None
    return _review_application_result(store, review_item)


def _review_application_result(store: Any, review_item: ReviewItem) -> dict[str, Any]:
    events = store.list_audit_events("review_item", review_item.review_item_id)
    latest = events[-1] if events else None
    metadata = latest.metadata if latest and isinstance(latest.metadata, dict) else {}
    applied = review_item.status == "applied"
    target_ids = {
        key: metadata.get(key)
        for key in ("agent_memory_id", "profile_card_id", "created_hyperedge_id")
        if metadata.get(key)
    }
    promotion_type = metadata.get("promotion_type")
    if not promotion_type and metadata.get("created_hyperedge_id"):
        promotion_type = "hyperedge"
    return {
        "applied": applied,
        "status": review_item.status,
        "review_type": review_item.review_type.value if hasattr(review_item.review_type, "value") else str(review_item.review_type),
        "action": latest.action if latest else None,
        "promotion_type": promotion_type,
        "target_ids": target_ids,
        "source_refs": metadata.get("source_refs") or [],
        "summary": _review_application_summary(review_item, metadata),
        "metadata": metadata,
    }


def _review_application_summary(review_item: ReviewItem, metadata: dict[str, Any]) -> str:
    if review_item.status == "rejected":
        return "Review item was rejected and no long-term knowledge was changed."
    if review_item.status == "approved":
        return "Review item was approved and is ready to apply."
    if review_item.status != "applied":
        return f"Review item status is {review_item.status}."
    if metadata.get("agent_memory_id"):
        return f"Promoted to agent memory {metadata['agent_memory_id']}."
    if metadata.get("profile_card_id"):
        return f"Promoted to profile card {metadata['profile_card_id']}."
    if metadata.get("created_hyperedge_id"):
        return f"Created graph relationship {metadata['created_hyperedge_id']}."
    return "Review item was applied."


def _console_review_type(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "unknown")
    return str(value or "unknown")


def _console_review_confidence(proposal: dict[str, Any]) -> float | None:
    value = proposal.get("confidence")
    candidate = proposal.get("candidate")
    if value is None and isinstance(candidate, dict):
        value = candidate.get("confidence")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _console_review_source_refs(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    refs = proposal.get("source_refs")
    candidate = proposal.get("candidate")
    if not isinstance(refs, list) and isinstance(candidate, dict):
        refs = candidate.get("source_refs")
    if not isinstance(refs, list):
        return []
    allowed = set(SourceRef.__dataclass_fields__)
    normalized: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        normalized_ref = {key: value for key, value in ref.items() if key in allowed and value}
        if normalized_ref:
            normalized.append(normalized_ref)
    return normalized


def _console_review_apply_supported(review_type: str, proposal: dict[str, Any]) -> bool:
    if review_type in {"profile_update", "share_proposal", "relationship_candidate", "memory_candidate"}:
        return True
    return review_type == "low_confidence" and bool(proposal.get("memory_candidate") or proposal.get("text"))


def _console_review_apply_ready(review_type: str, source_refs: list[dict[str, Any]], proposal: dict[str, Any] | None = None) -> bool:
    proposal = proposal or {}
    if review_type == "share_proposal":
        return True
    if review_type == "relationship_candidate":
        try:
            confidence = float(proposal.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return (
            bool(source_refs)
            and bool(str(proposal.get("relation_type") or "").strip())
            and len(_list_of_dicts(proposal.get("members"))) >= 2
            and 0 < confidence <= 1
        )
    if review_type in {"profile_update", "memory_candidate", "low_confidence"}:
        return bool(source_refs)
    return False


def _console_review_recommended_action(review_type: str, *, apply_ready: bool) -> str:
    if apply_ready:
        return "approve_apply"
    if review_type in {"conflict", "action_candidate"}:
        return "inspect_then_approve_or_reject"
    return "approve_or_reject"


def _console_search_summary(payload: dict[str, Any]) -> dict[str, Any]:
    score_debug = payload.get("score_debug") if isinstance(payload.get("score_debug"), dict) else {}
    payload_diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    diagnostics = score_debug.get("diagnostics") if isinstance(score_debug.get("diagnostics"), dict) else payload_diagnostics
    return {
        "request_user_id": payload.get("request_user_id"),
        "results": [
            {
                "source_item_id": _console_result_ref(result, "source_item_id"),
                "document_id": _console_result_ref(result, "document_id"),
                "chunk_id": _console_result_ref(result, "chunk_id"),
                "title": result.get("title"),
                "snippet": _ask_clean_evidence_text(result.get("snippet")) if result.get("snippet") else result.get("snippet"),
                "score": result.get("score"),
                "citation": _console_citation(result.get("citation")),
            }
            for result in _list_of_dicts(payload.get("results"))
        ],
        "citations": [_console_citation(citation) for citation in _list_of_dicts(payload.get("citations"))],
        "graph_paths": [_console_graph_path(path) for path in _list_of_dicts(payload.get("graph_paths"))],
        "diagnostics": {
            "gaps": list(payload.get("gaps") or diagnostics.get("gaps") or []),
            "conflicts": list(payload.get("conflicts") or diagnostics.get("conflicts") or []),
            "sensitivity": list(payload.get("sensitivity") or diagnostics.get("sensitivity") or []),
            "score_debug": diagnostics,
        },
        "memory_context": [_console_memory_context(item) for item in _list_of_dicts(payload.get("memory_context"))],
        "profile_context": [_console_memory_context(item) for item in _list_of_dicts(payload.get("profile_context"))],
    }


def _console_result_ref(result: dict[str, Any], key: str) -> Any:
    citation = result.get("citation") if isinstance(result.get("citation"), dict) else {}
    return result.get(key) or citation.get(key)


def _direct_retrieval_fallback_answer(query: str, retrieval: dict[str, Any]) -> str:
    results = _list_of_dicts(retrieval.get("results"))
    if not results:
        return "FastReAct 暂不可用，PSKA 已切换到 direct retrieval，但没有找到足够可展示的证据。"
    snippets = [str(item.get("snippet") or "").strip() for item in results if str(item.get("snippet") or "").strip()]
    titles = []
    for item in results:
        title = str(item.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
    lead = f"FastReAct 暂不可用，以下是 PSKA direct retrieval 对“{query}”找到的证据摘要。"
    if snippets:
        bullets = "\n".join(f"- {snippet[:220]}" for snippet in snippets[:4])
    else:
        bullets = "- 找到了相关来源，但这些结果没有返回可展示的摘要片段。"
    source_line = f"\n\n主要来源：{'；'.join(titles[:3])}" if titles else ""
    return f"{lead}\n{bullets}{source_line}"


def _console_citation(value: Any) -> dict[str, Any]:
    citation = value if isinstance(value, dict) else {}
    compact = {
        "source_item_id": citation.get("source_item_id"),
        "chunk_id": citation.get("chunk_id"),
        "title": citation.get("title"),
        "url": citation.get("url"),
        "snippet": _ask_clean_evidence_text(citation.get("snippet")) if citation.get("snippet") else citation.get("snippet"),
    }
    if citation.get("document_id") is not None:
        compact["document_id"] = citation.get("document_id")
    if citation.get("passage_window_id") is not None:
        compact["passage_window_id"] = citation.get("passage_window_id")
    return compact


def _console_graph_path(path: dict[str, Any]) -> dict[str, Any]:
    entities = [
        str(entity.get("label") or entity.get("entity_id") or "")
        for entity in _list_of_dicts(path.get("entities"))
    ]
    edges = _list_of_dicts(path.get("edges"))
    return {
        "depth": path.get("depth"),
        "entities": entities,
        "explanation": path.get("explanation"),
        "edge_count": len(edges),
        "grounded_edges": len([edge for edge in edges if edge.get("evidence_citations")]),
    }


def _console_memory_context(item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("text") or item.get("profile") or item.get("profile_delta") or "")
    return {
        "text": text[:240],
        "confidence": item.get("confidence"),
        "citation_count": len(_list_of_dicts(item.get("citations"))),
        "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
    }


def _workspace_chat_status(result: dict[str, Any]) -> dict[str, Any]:
    mode = str(result.get("mode") or "direct")
    if result.get("ok"):
        message = "Direct retrieval completed." if mode == "direct" else "Agentic search completed."
        return {"ok": True, "mode": mode, "message": message}
    fallback = result.get("fallback") if isinstance(result.get("fallback"), dict) else {}
    if mode == "agentic" and fallback:
        return {
            "ok": False,
            "mode": mode,
            "display_mode": "direct_fallback",
            "message": "Agentic search is unavailable; direct retrieval fallback is shown.",
        }
    return {"ok": False, "mode": mode, "message": str((result.get("error") or {}).get("message") or "Search failed.")}


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _ask_requested_intents(raw_intent: Any, raw_routing_mode: Any = None) -> tuple[str, str | None]:
    intent = str(raw_intent or "auto").strip().lower()
    routing_mode = str(raw_routing_mode or "").strip().lower()
    if intent in ASK_EXECUTION_INTENTS:
        execution_intent = intent
        forced_ask_intent = routing_mode if routing_mode in ASK_INTENTS else None
    elif intent in ASK_INTENTS:
        execution_intent = "auto"
        forced_ask_intent = intent
    else:
        raise ValueError("intent must be one of auto, quick, deep or a supported AskIntent")
    return execution_intent, forced_ask_intent


def _ask_understand_payload(
    *,
    query: str,
    intent: str,
    forced_ask_intent: str | None,
    scope: dict[str, Any],
    surface: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    local = decision or _ask_local_intent_guess(query=query, forced_ask_intent=forced_ask_intent, scope=scope, surface=surface)
    ask_intent = str(local.get("intent") or "kb_search")
    scope_applied = _ask_scope_applied(scope, ask_intent=ask_intent)
    rewrite_query = _ask_rewrite_query(query, scope=scope, ask_intent=ask_intent)
    requires_retrieval = local.get("requires_retrieval")
    payload = {
        "schema": "pska.ask_understand.v1",
        "query": query,
        "intent": ask_intent,
        "requested_intent": intent,
        "rewrite_query": rewrite_query,
        "scope_applied": scope_applied,
        "requires_retrieval": requires_retrieval if isinstance(requires_retrieval, bool) else _ask_requires_retrieval(ask_intent),
        "routing_owner": local.get("routing_owner") or "pska_local_intent_guard",
        "routing_mode": local.get("routing_mode") or ("forced" if forced_ask_intent else "auto"),
        "confidence": local.get("confidence"),
        "reasons": local.get("reasons") or [],
        "surface": surface,
    }
    selected_intent = str(local.get("selected_intent") or "").strip().lower()
    if selected_intent in {"quick", "deep"}:
        payload["agentic_selected_intent"] = selected_intent
    if isinstance(local.get("intent_classifier"), dict):
        payload["intent_classifier"] = local["intent_classifier"]
    if local.get("classifier_error"):
        payload["classifier_error"] = str(local.get("classifier_error"))
    return payload


def _ask_direct_intent_decision(
    *,
    query: str,
    execution_intent: str,
    forced_ask_intent: str | None,
    skip_intent_classifier: bool = False,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    has_conversation_history = bool(_list_of_dicts((scope or {}).get("recent_messages")))
    if forced_ask_intent in ASK_NON_RETRIEVAL_INTENTS:
        return {
            "intent": forced_ask_intent,
            "selected_intent": "quick",
            "confidence": 1.0,
            "routing_owner": "user_or_caller_override",
            "reasons": ["explicit_non_retrieval_intent"],
            "requires_retrieval": False,
        }
    text = re.sub(r"\s+", " ", str(query or "").strip().lower())
    if forced_ask_intent is None and _ask_is_greeting_query(text):
        return {
            "intent": "greeting",
            "selected_intent": "quick",
            "confidence": 0.95,
            "routing_owner": "pska_direct_intent_guard",
            "reasons": ["short_greeting"],
            "requires_retrieval": False,
        }
    if forced_ask_intent is None and _ask_is_chitchat_query(text):
        return {
            "intent": "chitchat",
            "selected_intent": "quick",
            "confidence": 0.9,
            "routing_owner": "pska_direct_intent_guard",
            "reasons": ["short_chitchat"],
            "requires_retrieval": False,
        }
    if forced_ask_intent is None and _ask_is_product_help_query(text):
        return {
            "intent": "product_help",
            "selected_intent": "quick",
            "confidence": 0.9,
            "routing_owner": "pska_direct_intent_guard",
            "reasons": ["product_capability_question"],
            "requires_retrieval": False,
        }
    if forced_ask_intent is None and _ask_is_writing_query(text):
        return {
            "intent": "writing",
            "selected_intent": execution_intent if execution_intent in {"quick", "deep"} else "quick",
            "confidence": 0.82,
            "routing_owner": "pska_direct_intent_guard",
            "reasons": ["writing_operation"],
            "requires_retrieval": True,
        }
    if execution_intent in {"quick", "deep"} and (skip_intent_classifier or not has_conversation_history):
        ask_intent = forced_ask_intent if forced_ask_intent in ASK_INTENTS else "kb_search"
        return {
            "intent": ask_intent,
            "selected_intent": execution_intent,
            "confidence": 1.0,
            "routing_owner": "user_or_caller_override",
            "routing_mode": "forced",
            "reasons": ["explicit_execution_intent", *([] if not skip_intent_classifier else ["intent_classifier_skipped"])],
            "requires_retrieval": _ask_requires_retrieval(ask_intent),
        }
    return None


def _ask_agentic_intent_prompt(
    *,
    query: str,
    raw_intent: str,
    forced_ask_intent: str | None,
    scope: dict[str, Any],
    surface: str,
) -> str:
    recent_messages = []
    for message in _list_of_dicts(scope.get("recent_messages"))[-4:]:
        recent_messages.append(
            {
                "role": str(message.get("role") or "")[:24],
                "content": _trim_words(str(message.get("content") or ""), 80),
            }
        )
    scope_summary = {
        "surface": surface,
        "requested_intent": raw_intent,
        "forced_ask_intent": forced_ask_intent,
        "scope_mode": scope.get("mode") or scope.get("scope_mode"),
        "source_item_count": len(_ask_scope_source_item_ids(scope)),
        "attachment_count": len(_string_list(scope.get("attachment_ids"))),
        "context_node_count": len(_list_of_dicts(scope.get("context_nodes"))),
        "recent_messages": recent_messages,
    }
    intents = ", ".join(sorted(ASK_INTENTS))
    return (
        "You are PSKA's agentic Ask intent classifier. Classify the user request only; do not answer it, "
        "do not retrieve evidence, and do not call tools.\n"
        "Return only one JSON object with these keys: "
        'ask_intent, selected_intent, requires_retrieval, confidence, reasons.\n'
        f"ask_intent must be one of: {intents}.\n"
        'selected_intent must be "quick" or "deep".\n'
        "First classify the complete user interaction: greeting, chitchat, product_help, and clarification "
        "are non-retrieval intents and must set requires_retrieval=false with selected_intent=\"quick\". "
        "Treat requested quick/deep as an execution-depth preference only, never as proof that the task "
        "needs evidence retrieval.\n"
        "Use writing when the requested operation is to draft, compose, rewrite, turn evidence into prose, "
        "or produce a report/brief/summary. Use clarification when the request lacks the object or scope "
        "needed to act.\n"
        "Use quick for exact lookup, direct extraction, single-file or selected-attachment questions, "
        "table row/field questions, and ordinary evidence lookup. Use deep for open-ended multi-step "
        "research, synthesis, comparison, conflict analysis, strategy, or explicit relationship/path/graph "
        "analysis across evidence. Do not classify from substrings embedded inside filenames, identifiers, "
        "tenant names, product names, or sample data labels; use the requested operation. If uncertain, "
        "prefer quick so the evidence path stays bounded.\n\n"
        f"Scope summary JSON: {json.dumps(scope_summary, ensure_ascii=False)}\n"
        f"User question: {query}"
    )


def _ask_agentic_intent_fallback_decision(
    *,
    reason: str,
    forced_ask_intent: str | None,
    raw_answer: str | None = None,
) -> dict[str, Any]:
    ask_intent = forced_ask_intent if forced_ask_intent in ASK_INTENTS else "kb_search"
    classifier: dict[str, Any] = {
        "schema": "pska.ask_intent_classifier.v1",
        "status": "fallback",
        "reason": reason,
    }
    if raw_answer:
        classifier["raw_answer_excerpt"] = _trim_words(raw_answer, 80)
    return {
        "intent": ask_intent,
        "selected_intent": "quick",
        "requires_retrieval": _ask_requires_retrieval(ask_intent),
        "confidence": 0.0,
        "routing_owner": "agentic_intent_unavailable_fallback",
        "reasons": [reason, "safe_quick_default"],
        "classifier_error": reason,
        "intent_classifier": classifier,
    }


def _ask_normalize_agentic_intent_decision(
    parsed: dict[str, Any],
    *,
    forced_ask_intent: str | None,
    agentic_service: dict[str, Any],
) -> dict[str, Any]:
    ask_intent = str(parsed.get("ask_intent") or parsed.get("intent") or "").strip().lower()
    if forced_ask_intent in ASK_INTENTS:
        ask_intent = forced_ask_intent
    if ask_intent not in ASK_INTENTS:
        ask_intent = "kb_search"
    selected_intent = str(parsed.get("selected_intent") or parsed.get("execution_intent") or parsed.get("route") or "").strip().lower()
    if selected_intent not in {"quick", "deep"}:
        selected_intent = "quick"
    if ask_intent in ASK_NON_RETRIEVAL_INTENTS:
        selected_intent = "quick"
    confidence_raw = parsed.get("confidence")
    try:
        confidence = max(0.0, min(float(confidence_raw), 1.0))
    except (TypeError, ValueError):
        confidence = 0.5
    reasons = [str(reason)[:80] for reason in (parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []) if str(reason).strip()]
    requires_retrieval = parsed.get("requires_retrieval")
    if not isinstance(requires_retrieval, bool):
        requires_retrieval = _ask_requires_retrieval(ask_intent)
    if ask_intent in ASK_NON_RETRIEVAL_INTENTS:
        requires_retrieval = False
    return {
        "intent": ask_intent,
        "selected_intent": selected_intent,
        "requires_retrieval": requires_retrieval,
        "confidence": confidence,
        "routing_owner": "agentic_intent_classifier",
        "reasons": reasons[:6] or ["agentic_intent_classifier"],
        "intent_classifier": {
            "schema": "pska.ask_intent_classifier.v1",
            "status": "classified",
            "provider": agentic_service.get("provider"),
            "adapter": agentic_service.get("adapter"),
            "selected_intent": selected_intent,
            "ask_intent": ask_intent,
            "confidence": confidence,
        },
    }


def _ask_local_intent_guess(
    *,
    query: str,
    forced_ask_intent: str | None,
    scope: dict[str, Any],
    surface: str,
) -> dict[str, Any]:
    if forced_ask_intent in ASK_INTENTS:
        return {
            "intent": forced_ask_intent,
            "confidence": 1.0,
            "routing_owner": "user_or_caller_override",
            "reasons": ["explicit_routing_mode"],
        }
    text = re.sub(r"\s+", " ", str(query or "").strip().lower())
    if _ask_is_greeting_query(text):
        return {"intent": "greeting", "confidence": 0.95, "reasons": ["short_greeting"]}
    if _ask_is_chitchat_query(text):
        return {"intent": "chitchat", "confidence": 0.9, "reasons": ["short_chitchat"]}
    if _ask_is_product_help_query(text):
        return {"intent": "product_help", "confidence": 0.9, "reasons": ["product_capability_question"]}
    if _ask_is_writing_query(text):
        return {"intent": "writing", "confidence": 0.82, "reasons": ["writing_operation"]}
    return {"intent": "kb_search", "confidence": 0.55, "reasons": ["conservative_kb_search_fallback"]}


def _ask_is_greeting_query(text: str) -> bool:
    normalized = re.sub(r"[\s,，。.!！?？~～]+", "", text)
    direct_greetings = {
        "hi",
        "hello",
        "hey",
        "哈喽",
        "哈罗",
        "你好",
        "您好",
        "嗨",
        "早上好",
        "晚上好",
        "下午好",
    }
    if normalized in direct_greetings:
        return True
    return bool(re.fullmatch(r"(hi|hello|hey|哈喽|哈罗|你好|您好|嗨|早上好|晚上好|下午好)[啊呀哇哈呢]*", normalized))


def _ask_is_chitchat_query(text: str) -> bool:
    normalized = re.sub(r"[\s,，。.!！?？~～]+", "", text)
    if normalized in {"谢谢", "谢谢你", "感谢", "多谢", "好的", "好呀", "好啊", "ok", "okay", "明白", "知道了", "哈哈", "哈哈哈"}:
        return True
    return bool(re.fullmatch(r"(谢谢|感谢|多谢)[你您啊呀哈]*", normalized))


def _ask_is_product_help_query(text: str) -> bool:
    if not text:
        return False
    capability_markers = [
        "你能做什么",
        "能做什么",
        "怎么用",
        "如何使用",
        "使用说明",
        "帮助",
        "help",
        "what can you do",
        "how do i use",
        "how to use",
    ]
    product_markers = ["pska", "你", "系统", "助手", "这个产品", "this app", "this product"]
    return any(marker in text for marker in capability_markers) and any(marker in text for marker in product_markers)


def _ask_is_writing_query(text: str) -> bool:
    if not text:
        return False
    writing_markers = [
        "写一段",
        "写成",
        "写作",
        "文稿",
        "草稿",
        "报告摘要",
        "摘要",
        "brief",
        "draft",
        "compose",
        "summary",
        "rewrite",
    ]
    return any(marker in text for marker in writing_markers)


def _ask_rewrite_query(query: str, *, scope: dict[str, Any], ask_intent: str) -> str:
    if ask_intent == "follow_up":
        recent_messages = _list_of_dicts(scope.get("recent_messages"))
        previous = " ".join(str(message.get("content") or "") for message in recent_messages[-4:])
        if previous.strip():
            return f"{query}\n\nConversation context:\n{_trim_words(previous, 160)}"
    return query


def _ask_scope_for_intent(scope: dict[str, Any], *, ask_intent: str) -> dict[str, Any]:
    scoped = dict(scope or {})
    if ask_intent != "follow_up":
        scoped.pop("recent_messages", None)
        scoped.pop("conversation_summary", None)
    return scoped


def _ask_requires_retrieval(ask_intent: str) -> bool:
    return ask_intent in ASK_RETRIEVAL_INTENTS


def _ask_scope_source_item_ids(scope: dict[str, Any]) -> set[str]:
    source_ids = set(_string_list(scope.get("source_item_ids")))
    source_ids.update(_string_list(scope.get("attachment_source_item_ids")))
    for attachment in _list_of_dicts(scope.get("attachments")):
        source_id = str(attachment.get("source_item_id") or attachment.get("sourceItemId") or "").strip()
        if source_id:
            source_ids.add(source_id)
    return source_ids


def _ask_scope_mode(scope: dict[str, Any], *, ask_intent: str) -> str:
    explicit_mode = str(scope.get("mode") or scope.get("scope_mode") or "").strip().lower()
    if explicit_mode in {"hard", "soft"}:
        return explicit_mode
    if ask_intent == "doc_only":
        return "hard"
    if _string_list(scope.get("attachment_ids")) or _ask_scope_source_item_ids(scope):
        return "hard"
    return "soft"


def _ask_scope_applied(scope: dict[str, Any], *, ask_intent: str) -> dict[str, Any]:
    source_item_ids = sorted(_ask_scope_source_item_ids(scope))
    knowledge_base_source_item_ids = _string_list(scope.get("knowledge_base_source_item_ids"))
    return {
        "mode": _ask_scope_mode(scope, ask_intent=ask_intent),
        "knowledge_base_ids": _knowledge_base_ids_from_scope(scope),
        "knowledge_base_source_item_count": len(knowledge_base_source_item_ids),
        "source_item_ids": source_item_ids,
        "source_item_count": len(source_item_ids),
        "dropped_scope_ids": _string_list(scope.get("dropped_scope_ids")),
        "dropped_source_item_ids": _string_list(scope.get("dropped_source_item_ids")),
        "attachment_ids": _string_list(scope.get("attachment_ids")),
        "allow_expand_scope": bool(scope.get("allow_expand_scope")),
        "conversation_id": scope.get("conversation_id"),
        "context_node_count": len(_list_of_dicts(scope.get("context_nodes"))),
    }


def _retrieval_source_item_ids_arg(source_item_ids: set[str], *, scope_mode: str) -> set[str] | None:
    if scope_mode == "hard":
        return set(source_item_ids)
    return source_item_ids or None


def _ask_no_retrieval_answer(ask_intent: str, query: str) -> tuple[str, str]:
    if ask_intent == "greeting":
        return (
            "你好，我是 PSKA。你可以把资料上传、粘贴或加入资料库，然后让我基于证据回答、做 digest、生成待 Review 的知识和 Evidence Brief。",
            "direct_greeting",
        )
    if ask_intent == "product_help":
        return (
            "我可以帮你做四类事：管理资料库，基于资料问答并保留引用，运行 Digest 生成可审阅的 claims/topics/relationships，以及把已审证据整理成写作节点或 Evidence Brief。普通问答不会自动写入长期知识，只有你明确保存或 Review 通过后才会沉淀。",
            "product_help",
        )
    if ask_intent == "clarification":
        return ("这个问题还缺少对象或范围。你可以指定一份资料、一个主题，或者说明希望我查资料库、只看附件，还是生成写作草稿。", "clarification")
    return ("我可以继续聊，但 PSKA 的强项是基于你的资料给出有引用的回答。给我一个主题或上传资料后，我会按证据回答。", "chitchat")


def _ask_evidence_terms(text: str) -> list[str]:
    text = str(text or "").casefold()
    stopwords = {
        "what",
        "who",
        "where",
        "when",
        "why",
        "how",
        "the",
        "this",
        "that",
        "with",
        "about",
        "please",
        "can",
        "could",
        "would",
        "does",
        "do",
        "prove",
        "answer",
        "knowledge",
        "base",
        "你",
        "我",
        "他",
        "她",
        "它",
        "这个",
        "那个",
        "这些",
        "那些",
        "看看",
        "是谁",
        "什么",
        "什么是",
        "如何",
        "为什么",
        "请",
        "一下",
        "关于",
        "总结",
        "介绍",
        "说明",
        "分析",
        "当前",
        "系统",
        "资料",
        "资料库",
        "知识库",
        "文档",
        "附件",
        "能",
        "能够",
        "可以",
        "证明",
        "能证明",
        "回答",
        "问题",
        "是否",
        "有没有",
        "找到",
        "证据",
    }
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip(" _-.,?!?;:，。！？；：、()[]{}<>《》\"'")
        if len(term) < 2 or term in stopwords or term in seen:
            return
        seen.add(term)
        terms.append(term)

    for token in re.findall(r"[a-z0-9_]{2,}", text):
        add(token)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        normalized = chunk
        for stopword in sorted(stopwords, key=len, reverse=True):
            normalized = normalized.replace(stopword, " ")
        normalized = re.sub(r"[的和与及并、了是吗呢吧啊么在对中上里将把为给从到]", " ", normalized)
        for term in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            add(term)
            for ngram_size in (2, 3, 4):
                if len(term) > ngram_size:
                    for index in range(len(term) - ngram_size + 1):
                        add(term[index : index + ngram_size])
    return terms[:24]


def _ask_query_anchor_terms(query: str, terms: list[str] | None = None) -> list[str]:
    generic_terms = {
        "can",
        "could",
        "would",
        "does",
        "do",
        "prove",
        "answer",
        "evidence",
        "knowledge",
        "base",
        "资料",
        "资料库",
        "知识库",
        "文档",
        "附件",
        "证明",
        "能证明",
        "回答",
        "问题",
        "当前",
        "系统",
        "证据",
        "找到",
        "是否",
        "有没有",
        "多少",
        "如何",
        "什么",
    }
    candidates = list(terms or _ask_evidence_terms(query))
    anchors: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        normalized = str(term or "").strip().casefold()
        if len(normalized) < 2 or normalized in generic_terms or normalized in seen:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
            continue
        seen.add(normalized)
        anchors.append(normalized)
    # Prefer the more specific phrase when both a phrase and its n-grams are present.
    specific: list[str] = []
    for term in anchors:
        if any(term != other and term in other and len(other) > len(term) for other in anchors):
            continue
        specific.append(term)
    return specific[:8]


def _ask_structural_evidence_hits(query: str, text: str) -> list[str]:
    normalized_query = str(query or "").casefold()
    haystack = str(text or "")
    folded = haystack.casefold()
    hits: list[str] = []
    intent_patterns = (
        ("url", ("url", "website", "web site", "link", "网址", "网站", "网页", "链接"), r"(?:https?://|www\.)[^\s|,，。；;]+|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}"),
        ("phone", ("phone", "telephone", "tel", "mobile", "联系电话", "电话", "热线", "手机号"), r"\+?\d[\d\s().-]{5,}\d"),
        ("email", ("email", "e-mail", "mail", "邮箱", "电子邮件", "邮件地址"), r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"),
    )
    for label, markers, pattern in intent_patterns:
        if not any(_ask_query_mentions_structural_marker(normalized_query, marker) for marker in markers):
            continue
        if label == "phone":
            if _extract_phone_values(haystack, max_values=1):
                hits.append(label)
            continue
        if re.search(pattern, folded):
            hits.append(label)
    return hits


def _ask_query_mentions_structural_marker(normalized_query: str, marker: str) -> bool:
    marker = str(marker or "").strip().casefold()
    if not marker:
        return False
    if re.search(r"[a-z0-9]", marker):
        parts = [part for part in re.split(r"\s+", marker) if part]
        pattern = r"\s+".join(re.escape(part) for part in parts) if parts else re.escape(marker)
        return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", normalized_query) is not None
    return marker in normalized_query


def _text_has_negated_label(text: str, label: str) -> bool:
    normalized_label = str(label or "").strip().casefold()
    if len(normalized_label) < 2:
        return False
    haystack = str(text or "").casefold()
    if not haystack:
        return False
    patterns = [re.escape(normalized_label)]
    if re.fullmatch(r"[a-z0-9_-]+", normalized_label):
        patterns.append(r"\b" + re.escape(normalized_label) + r"\b")
    for pattern in patterns:
        for match in re.finditer(pattern, haystack):
            start = max(0, match.start() - 90)
            end = min(len(haystack), match.end() + 90)
            window = haystack[start:end]
            if _negation_window_matches(window):
                return True
    return False


def _negation_window_matches(window: str) -> bool:
    english_markers = [
        "does not mention",
        "do not mention",
        "did not mention",
        "doesn't mention",
        "don't mention",
        "not mention",
        "not mentioned",
        "no mention",
        "not related to",
        "no relation to",
        "unrelated to",
        "not involve",
        "does not involve",
        "did not involve",
        "without mentioning",
        "without reference to",
        "not cite",
        "not cited",
    ]
    chinese_markers = ["没有提到", "未提到", "未提及", "不提及", "不涉及", "没有涉及", "无关", "没有关系", "并非", "不是"]
    return any(marker in window for marker in english_markers) or any(marker in window for marker in chinese_markers)


def _ask_verify_evidence(
    *,
    query: str,
    evidence: dict[str, Any],
    scope: dict[str, Any],
    ask_intent: str,
) -> dict[str, Any]:
    citations = _list_of_dicts(evidence.get("citations"))
    results = _list_of_dicts(evidence.get("results"))
    source_windows = _list_of_dicts(evidence.get("source_windows"))
    allowed_source_ids = _ask_scope_source_item_ids(scope)
    scope_mode = _ask_scope_mode(scope, ask_intent=ask_intent)
    hard_scope = scope_mode == "hard"
    query_terms = _ask_evidence_terms(query)
    anchor_terms = _ask_query_anchor_terms(query, query_terms)
    text_by_key: dict[tuple[str, str], str] = {}
    window_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    windows_by_source: dict[str, list[dict[str, Any]]] = {}
    result_by_source: dict[str, list[dict[str, Any]]] = {}

    def remember_window(window: dict[str, Any]) -> None:
        source_id = str(window.get("source_item_id") or "")
        if not source_id or not str(window.get("text") or "").strip():
            return
        key = (source_id, str(window.get("chunk_id") or window.get("passage_window_id") or ""))
        text_by_key[key] = str(window.get("text") or "")
        window_by_key[key] = window
        windows_by_source.setdefault(source_id, []).append(window)

    for result in results:
        source_id = str(result.get("source_item_id") or result.get("citation", {}).get("source_item_id") or "")
        chunk_id = str(result.get("chunk_id") or result.get("citation", {}).get("chunk_id") or "")
        source_window = result.get("source_window") if isinstance(result.get("source_window"), dict) else {}
        if source_id:
            result_by_source.setdefault(source_id, []).append(result)
            if source_window.get("text"):
                remember_window(source_window)
            elif chunk_id:
                text_by_key[(source_id, chunk_id)] = ""
    for window in source_windows:
        remember_window(window)

    used: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for citation in citations:
        source_id = str(citation.get("source_item_id") or "")
        chunk_id = str(citation.get("chunk_id") or "")
        if hard_scope and (not source_id or source_id not in allowed_source_ids):
            dropped.append({**citation, "drop_reason": "scope_violation"})
            continue
        source_window = citation.get("source_window") if isinstance(citation.get("source_window"), dict) else {}
        if not source_window.get("text"):
            source_window = window_by_key.get((source_id, chunk_id)) or window_by_key.get((source_id, str(citation.get("passage_window_id") or ""))) or {}
        if not source_window.get("text") and source_id in windows_by_source and len(windows_by_source[source_id]) == 1:
            source_window = windows_by_source[source_id][0]
        evidence_text = str(source_window.get("text") or "")
        if not evidence_text.strip():
            dropped.append({**citation, "drop_reason": "missing_source_window"})
            continue
        check_text = "\n".join(
            part
            for part in (
                str(citation.get("title") or ""),
                str(citation.get("snippet") or ""),
                evidence_text,
            )
            if part
        )
        folded_check_text = check_text.casefold()
        evidence_terms = set(_ask_evidence_terms(check_text))
        structural_hits = _ask_structural_evidence_hits(query, check_text)
        support_hits = [term for term in query_terms if term in evidence_terms or term in folded_check_text]
        anchor_hits = [term for term in anchor_terms if term in evidence_terms or term in folded_check_text]
        if structural_hits:
            support_hits.extend(hit for hit in structural_hits if hit not in support_hits)
            anchor_hits.extend(hit for hit in structural_hits if hit not in anchor_hits)
        if ask_intent != "doc_only":
            if anchor_terms and not anchor_hits:
                dropped.append({**citation, "drop_reason": "missing_query_anchor", "query_anchors": anchor_terms[:8]})
                continue
            if anchor_hits and any(_text_has_negated_label(evidence_text, term) for term in anchor_hits):
                dropped.append({**citation, "drop_reason": "negated_context", "query_anchors": anchor_hits[:8]})
                continue
            if query_terms and not support_hits:
                dropped.append({**citation, "drop_reason": "lexically_unsupported"})
                continue
        used.append(
            {
                **citation,
                "source_item_id": source_id,
                "document_id": citation.get("document_id") or source_window.get("document_id"),
                "chunk_id": citation.get("chunk_id") or source_window.get("chunk_id"),
                "passage_window_id": citation.get("passage_window_id") or source_window.get("passage_window_id"),
                "title": citation.get("title") or source_window.get("title"),
                "url": citation.get("url") or source_window.get("url"),
                "snippet": citation.get("snippet") or _ask_clean_evidence_text(evidence_text)[:600],
                "source_window": source_window,
                "support_hits": support_hits[:8],
            }
        )

    no_answer_reasons: list[str] = []
    if hard_scope and not allowed_source_ids:
        no_answer_reasons.append("hard_scope_has_no_source_items")
    if not citations and not results:
        no_answer_reasons.append("no_retrieval_results")
    if citations and not used:
        reasons = [str(item.get("drop_reason") or "dropped") for item in dropped]
        no_answer_reasons.append("all_citations_dropped")
        no_answer_reasons.extend(list(dict.fromkeys(reasons))[:3])
    if not used and results:
        no_answer_reasons.append("no_supporting_citations_after_evidence_check")
    evidence_claims = _ask_clean_facts_from_results(
        [result for result in results if not used or str(result.get("source_item_id") or "") in {str(ref.get("source_item_id") or "") for ref in used}],
        limit=6,
    )
    return {
        "schema": "pska.ask_evidence_check.v1",
        "status": "supported" if used else "insufficient",
        "scope_mode": scope_mode,
        "query_terms": query_terms,
        "query_anchors": anchor_terms,
        "used_citations": used,
        "dropped_citations": dropped,
        "evidence_claims": evidence_claims,
        "no_answer_reasons": list(dict.fromkeys(no_answer_reasons)),
        "supporting_citation_count": len(used),
        "dropped_citation_count": len(dropped),
    }


def _ask_apply_evidence_check(evidence: dict[str, Any], evidence_check: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(evidence)
    used = _list_of_dicts(evidence_check.get("used_citations"))
    used_keys = {_ask_citation_key(ref) for ref in used}
    filtered["citations"] = used
    filtered["source_refs"] = used
    filtered["dropped_citations"] = _list_of_dicts(evidence_check.get("dropped_citations"))
    if used_keys:
        used_source_ids = {str(ref.get("source_item_id") or "") for ref in used}
        filtered["results"] = [
            result
            for result in _list_of_dicts(evidence.get("results"))
            if _ask_citation_key(result.get("citation") if isinstance(result.get("citation"), dict) else result) in used_keys
            or str(result.get("source_item_id") or "") in used_source_ids
        ]
        filtered["source_windows"] = [
            window
            for window in _list_of_dicts(evidence.get("source_windows"))
            if _ask_citation_key(window) in used_keys or str(window.get("source_item_id") or "") in used_source_ids
        ]
    else:
        filtered["results"] = []
        filtered["source_windows"] = []
    filtered["evidence_claims"] = list(evidence_check.get("evidence_claims") or [])
    filtered["no_answer_reasons"] = list(evidence_check.get("no_answer_reasons") or [])
    return filtered


def _ask_apply_evidence_check_to_retrieval(retrieval: dict[str, Any], evidence_check: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(retrieval)
    used = _list_of_dicts(evidence_check.get("used_citations"))
    used_keys = {_ask_citation_key(ref) for ref in used}
    if used_keys:
        used_source_ids = {str(ref.get("source_item_id") or "") for ref in used}
        filtered["results"] = [
            result
            for result in _list_of_dicts(retrieval.get("results"))
            if _ask_citation_key(result.get("citation") if isinstance(result.get("citation"), dict) else result) in used_keys
            or str(result.get("source_item_id") or "") in used_source_ids
        ]
        filtered["source_windows"] = [
            window
            for window in _list_of_dicts(retrieval.get("source_windows"))
            if _ask_citation_key(window) in used_keys or str(window.get("source_item_id") or "") in used_source_ids
        ]
    else:
        filtered["results"] = []
        filtered["source_windows"] = []
    filtered["citations"] = used
    return filtered


def _ask_citation_key(ref: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(ref.get("source_item_id") or ""),
        str(ref.get("document_id") or ""),
        str(ref.get("chunk_id") or ref.get("passage_window_id") or ""),
    )


def _ask_no_answer_from_evidence_check(query: str, evidence_check: dict[str, Any]) -> str:
    reasons = list(evidence_check.get("no_answer_reasons") or [])
    if not reasons:
        reasons = ["evidence_insufficient"]
    if "no_retrieval_results" in reasons:
        detail = "没有检索到能直接支撑该问题的相关片段"
    elif "all_citations_dropped" in reasons or "no_supporting_citations_after_evidence_check" in reasons:
        detail = "检索到的片段没有通过引用支撑校验"
    elif "hard_scope_has_no_source_items" in reasons:
        detail = "当前选择范围没有可检索资料"
    else:
        detail = "现有证据信号不足"
    return f"关键结论：当前资料不足以回答“{query}”。{detail}。请补充相关资料、选择正确附件，或允许扩大检索范围后再问。"


def _ask_route_intent(
    query: str,
    *,
    intent: str,
    ask_intent: str = "kb_search",
    scope: dict[str, Any] | None = None,
    agentic_selected_intent: str | None = None,
) -> str:
    if ask_intent in ASK_NON_RETRIEVAL_INTENTS:
        return "quick"
    if intent in {"quick", "deep"}:
        return intent
    selected = str(agentic_selected_intent or "").strip().lower()
    if selected in {"quick", "deep"}:
        return selected
    return "quick"


def _ask_intent_contract(
    *,
    ask_intent: str,
    selected_intent: str,
    requested_intent: str,
    execution_intent: str,
    scope: dict[str, Any] | None,
    surface: str,
    routing_owner: str = "",
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    requires_evidence = _ask_requires_retrieval(ask_intent)
    requested_depth = execution_intent if execution_intent in {"quick", "deep"} else "auto"
    execution_depth = selected_intent if requires_evidence else "none"
    override_requested = requested_depth in {"quick", "deep"}
    override_applied = bool(override_requested and requires_evidence and selected_intent == requested_depth)
    if not override_requested:
        override_reason = "not_requested"
    elif not requires_evidence:
        override_reason = "non_evidence_intent"
    elif override_applied:
        override_reason = "applied"
    else:
        override_reason = "classifier_or_policy_selected_different_depth"
    return {
        "schema": "pska.ask_intent_contract.v1",
        "interaction_intent": _ask_interaction_intent(ask_intent),
        "task_intent": ask_intent if requires_evidence else "none",
        "ask_intent": ask_intent,
        "requires_evidence": requires_evidence,
        "execution_depth": execution_depth,
        "requested_depth": requested_depth,
        "scope_policy": _ask_scope_mode(scope or {}, ask_intent=ask_intent) if requires_evidence else "none",
        "answer_contract": _ask_answer_contract(ask_intent),
        "quick_deep_applicable": requires_evidence,
        "depth_override": {
            "requested": requested_depth if override_requested else None,
            "applied": override_applied,
            "reason": override_reason,
        },
        "surface": surface,
        "requested_intent": requested_intent,
        "routing_owner": routing_owner or "pska_planner",
        "reasons": list(reasons or [])[:6],
    }


def _ask_interaction_intent(ask_intent: str) -> str:
    if ask_intent in {"greeting", "chitchat", "product_help", "clarification"}:
        return ask_intent
    if ask_intent == "writing":
        return "writing"
    if ask_intent == "graph_research":
        return "graph_research"
    if ask_intent == "follow_up":
        return "follow_up"
    return "evidence_qa"


def _ask_answer_contract(ask_intent: str) -> str:
    if ask_intent in {"greeting", "chitchat"}:
        return "direct_response"
    if ask_intent == "product_help":
        return "product_help"
    if ask_intent == "clarification":
        return "clarification"
    if ask_intent == "writing":
        return "writing_context_answer"
    return "evidence_bound_answer"


def _ask_query_terms(query: str) -> list[str]:
    text = str(query or "").lower()
    seen: set[str] = set()
    terms: list[str] = []

    def add(term: str) -> None:
        term = term.strip(" _-.,?!?;:，。！？；：、()[]{}<>《》\"'")
        if len(term) < 2 or term in seen:
            return
        seen.add(term)
        terms.append(term)

    for term in re.findall(r"[a-z][a-z0-9_-]*", text):
        add(term)

    chinese_stopwords = {
        "是什么",
        "什么是",
        "什么样",
        "怎么样",
        "一个",
        "相关",
        "情况",
        "介绍",
        "哪些",
        "一下",
        "这个",
        "那个",
        "多少",
        "如何",
        "请",
        "深入分析",
        "深入",
        "分析",
        "给出",
        "可引用",
    }
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        normalized = chunk
        for stopword in chinese_stopwords:
            normalized = normalized.replace(stopword, " ")
        normalized = re.sub(r"[的和与及并、]", " ", normalized)
        for term in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            add(term)

    if terms:
        return terms[:6]
    return _graph_query_terms(query)[:6]


def _ask_route_payload(
    *,
    intent: str,
    selected_intent: str,
    retrieval_owner: str,
    surface: str,
    requires_agentic_service_online: bool,
    tool_policy: dict[str, Any],
    query: str,
    fallback_from: str | None = None,
    requested_intent: str | None = None,
    understand: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent_contract = (understand or {}).get("intent_contract") if isinstance((understand or {}).get("intent_contract"), dict) else None
    if intent_contract is None:
        intent_contract = _ask_intent_contract(
            ask_intent=intent,
            selected_intent=selected_intent,
            requested_intent=requested_intent or intent,
            execution_intent=selected_intent if selected_intent in {"quick", "deep"} else "auto",
            scope={},
            surface=surface,
            routing_owner="pska_planner",
            reasons=[],
        )
    payload = {
        "intent": intent,
        "requested_intent": requested_intent or intent,
        "selected_intent": selected_intent,
        "retrieval_owner": retrieval_owner,
        "surface": surface,
        "requires_agentic_service_online": requires_agentic_service_online,
        "tool_policy": tool_policy,
        "tool_profile": ASK_READ_TOOL_PROFILE if retrieval_owner == "fastreact_pska_mcp" else "none",
        "routing_owner": "pska_planner",
        "query_terms": _ask_query_terms(str((understand or {}).get("rewrite_query") or query)),
        "rewrite_query": (understand or {}).get("rewrite_query") or query,
        "scope_applied": (understand or {}).get("scope_applied") or {},
        "intent_contract": intent_contract,
    }
    if understand:
        payload["understand"] = understand
    if fallback_from:
        payload["fallback_from"] = fallback_from
    return payload


def _ask_read_tool_policy(scope_applied: dict[str, Any] | None = None, *, allowed_tools: list[str] | None = None) -> dict[str, Any]:
    scope_applied = scope_applied if isinstance(scope_applied, dict) else {}
    source_item_ids = _string_list(scope_applied.get("source_item_ids"))
    knowledge_base_ids = _string_list(scope_applied.get("knowledge_base_ids"))
    scope_mode = str(scope_applied.get("mode") or scope_applied.get("scope_mode") or ("hard" if source_item_ids or knowledge_base_ids else "soft"))
    policy: dict[str, Any] = {
        "mode": "allowlist",
        "allowed_tools": allowed_tools or ASK_READ_ONLY_TOOLS,
    }
    if source_item_ids or knowledge_base_ids:
        policy["scope"] = {
            "mode": "hard" if scope_mode == "hard" or source_item_ids or knowledge_base_ids else "soft",
            "scope_mode": "hard" if scope_mode == "hard" or source_item_ids or knowledge_base_ids else "soft",
            "knowledge_base_ids": knowledge_base_ids,
            "source_item_ids": source_item_ids,
        }
    return policy


def _ask_route_planner_steps(
    *,
    query: str,
    intent: str,
    selected_intent: str,
    query_terms: list[str],
    started_at: float,
    start_sequence: int,
    include_understand: bool,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    sequence = start_sequence
    if include_understand:
        steps.append(
            _ask_agent_step(
                sequence=sequence,
                phase="understand",
                status="complete",
                title="理解问题",
                detail=_ask_understand_step_detail(query_terms, intent=intent),
                started_at=started_at,
            )
        )
        sequence += 1
    steps.append(
        _ask_agent_step(
            sequence=sequence,
            phase="route",
            status="complete",
            title="选择回答路线",
            detail=_ask_route_step_detail(intent=intent, selected_intent=selected_intent, query_terms=query_terms),
            started_at=started_at,
        )
    )
    return steps


def _ask_understand_step_detail(query_terms: list[str], *, intent: str = "kb_search") -> str:
    if intent in ASK_NON_RETRIEVAL_INTENTS:
        return f"已识别为{_ask_intent_display_name(intent)}，无需资料库检索。"
    if query_terms:
        return f"已抽取检索关键词：{'、'.join(query_terms[:6])}。"
    return "已确认问题和当前租户范围。"


def _ask_route_step_detail(*, intent: str, selected_intent: str, query_terms: list[str]) -> str:
    if intent in ASK_NON_RETRIEVAL_INTENTS:
        return f"任务类型 {_ask_intent_display_name(intent)} 不需要资料库证据，直接回应。"
    route_label = "深入分析" if selected_intent == "deep" else "快速回答"
    intent_label = "自动路由" if intent == "auto" else f"任务类型 {intent}"
    term_text = f"关键词：{'、'.join(query_terms[:6])}；" if query_terms else ""
    if selected_intent == "deep":
        return f"{term_text}{intent_label} 判定需要 {route_label}，由 FastReAct 通过 PSKA 只读工具检索。"
    return f"{term_text}{intent_label} 判定可先走 {route_label}，由 PSKA 检索知识库与图谱。"


def _ask_intent_display_name(intent: str) -> str:
    return {
        "greeting": "问候",
        "chitchat": "闲聊",
        "product_help": "产品帮助",
        "clarification": "澄清请求",
        "kb_search": "资料库检索",
        "doc_only": "附件检索",
        "follow_up": "追问",
        "graph_research": "图谱研究",
        "writing": "写作",
    }.get(intent, intent)


def _ask_quick_search_step(*, sequence: int, query_terms: list[str], top_k: int, started_at: float) -> dict[str, Any]:
    term_text = f"关键词：{'、'.join(query_terms[:6])}；" if query_terms else ""
    return _ask_agent_step(
        sequence=sequence,
        phase="search",
        status="running",
        title="检索知识库与图谱",
        detail=f"{term_text}最多读取 {top_k} 条相关证据。",
        started_at=started_at,
    )


def _ask_quick_read_step(*, sequence: int, evidence: dict[str, Any], started_at: float) -> dict[str, Any]:
    results = _list_of_dicts(evidence.get("results"))
    citations = _list_of_dicts(evidence.get("citations"))
    graph_paths = _list_of_dicts(evidence.get("graph_paths"))
    return _ask_agent_step(
        sequence=sequence,
        phase="read",
        status="complete",
        title="读取证据",
        detail=f"返回 {len(results)} 条证据，{len(citations)} 条引用，{len(graph_paths)} 条图谱路径。",
        evidence_count=len(results),
        source_ref_count=len(citations),
        started_at=started_at,
    )


def _ask_deep_query(*, query: str, surface: str, scope: dict[str, Any]) -> str:
    scope_text = json.dumps(scope or {}, ensure_ascii=False)
    return (
        "Answer this PSKA knowledge question for a user-facing Ask PSKA surface.\n"
        "Use only PSKA read-only retrieval tools exposed in this run. The ask_read profile may include "
        "pska_search, pska_read_evidence_context, pska_graph_context, pska_digest_context, and "
        "pska_index_status. Do not use host filesystem, shell, "
        "write, review mutation, job, or candidate-write tools. Do not mention FastReAct, MCP, GraphRAG, "
        "tool policy, or retrieval mechanics in the answer body. Put diagnostics in trace only.\n"
        "If Scope.understand.scope_applied.mode is hard or source_item_ids are present, every PSKA read tool call "
        "must include those source_item_ids and scope_mode=hard. Do not cite or infer from sources outside that "
        "scope unless allow_expand_scope is explicitly true and you report expansion in trace.\n"
        "For deep research, run a bounded generic research loop: start with pska_search, then read fuller "
        "source windows with pska_read_evidence_context, expand entities/claims with pska_graph_context "
        "when relationships or conflicts matter, inspect pska_digest_context for prior digests, claims, "
        "risks, and open questions when useful, and only then decide whether a targeted follow-up search "
        "is needed. Stop when evidence is sufficient, no new evidence appears, or the iteration budget is reached.\n"
        "Return JSON with keys answer, citations, source_refs, retrieval, and trace. The answer must be "
        "Chinese by default, conclusion-first, useful as report evidence, and grounded in citations when "
        "PSKA returns evidence. If evidence is insufficient, say what is missing.\n\n"
        f"Surface: {surface}\n"
        f"Scope: {scope_text}\n"
        f"User question: {query}"
    )


def _ask_query_with_scope(query: str, scope: dict[str, Any]) -> str:
    context_lines: list[str] = []
    for node in _list_of_dicts(scope.get("context_nodes"))[:8]:
        title = str(node.get("title") or "").strip()
        body = str(node.get("body_markdown") or "").strip()
        node_type = str(node.get("node_type") or "node").strip()
        text = " ".join(part for part in [title, body] if part).strip()
        if text:
            context_lines.append(f"{node_type}: {_trim_words(text, 80)}")
    if not context_lines:
        return query
    return "\n".join([
        query,
        "",
        "Connected writing context:",
        *context_lines,
    ])


def _ask_scope_trace(scope: dict[str, Any]) -> dict[str, Any]:
    context_nodes = _list_of_dicts(scope.get("context_nodes"))
    context_edges = _list_of_dicts(scope.get("context_edges"))
    return {
        "board_id": scope.get("board_id"),
        "node_id": scope.get("node_id"),
        "session_id": scope.get("session_id"),
        "context_model": scope.get("context_model"),
        "context_node_count": len(context_nodes),
        "context_edge_count": len(context_edges),
        "knowledge_base_ids": _knowledge_base_ids_from_scope(scope),
        "knowledge_base_source_item_ids": _string_list(scope.get("knowledge_base_source_item_ids"))[:20],
        "source_item_ids": _string_list(scope.get("source_item_ids"))[:20],
    }


def _ask_evidence_from_retrieval(retrieval: dict[str, Any]) -> dict[str, Any]:
    diagnostics = retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {}
    results = _list_of_dicts(retrieval.get("results"))
    citations = _ask_citations_from_retrieval(retrieval, results)
    return {
        "citations": citations,
        "source_refs": citations,
        "results": results,
        "source_windows": _list_of_dicts(retrieval.get("source_windows")),
        "graph_paths": _list_of_dicts(retrieval.get("graph_paths")),
        "memory_context": _list_of_dicts(retrieval.get("memory_context")),
        "profile_context": _list_of_dicts(retrieval.get("profile_context")),
        "gaps": list(diagnostics.get("gaps") or []),
        "conflicts": list(diagnostics.get("conflicts") or []),
    }


def _ask_citations_from_retrieval(retrieval: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citations = [citation for citation in _list_of_dicts(retrieval.get("citations")) if citation.get("source_item_id")]
    for result in results:
        citation = result.get("citation") if isinstance(result.get("citation"), dict) else {}
        if citation.get("source_item_id"):
            citations.append(_console_citation(citation))
    return _dedupe_source_ref_dicts(citations)


def _dedupe_source_ref_dicts(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for ref in refs:
        key = (ref.get("source_item_id"), ref.get("chunk_id"), ref.get("title"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _ask_retrieval_from_agentic_trace(trace: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "results": [],
        "citations": [],
        "graph_paths": [],
        "memory_context": [],
        "profile_context": [],
        "diagnostics": {"gaps": [], "conflicts": [], "sensitivity": [], "score_debug": {}},
    }
    result_keys: set[tuple[Any, Any, Any]] = set()
    citation_keys: set[tuple[Any, Any]] = set()
    for event in _list_of_dicts(trace.get("events")):
        if str(event.get("type") or "").lower() != "tool_result":
            continue
        if str(event.get("tool_name") or "") not in ASK_READ_ONLY_TOOLS:
            continue
        content = str(event.get("content") or "").strip()
        if not content:
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        retrieval = _console_search_summary(payload)
        for result in _list_of_dicts(retrieval.get("results")):
            key = (result.get("source_item_id"), result.get("chunk_id"), result.get("title"))
            if key in result_keys:
                continue
            result_keys.add(key)
            merged["results"].append(result)
            citation = result.get("citation") if isinstance(result.get("citation"), dict) else {}
            if citation.get("source_item_id"):
                citation_key = (citation.get("source_item_id"), citation.get("chunk_id"))
                if citation_key not in citation_keys:
                    citation_keys.add(citation_key)
                    merged["citations"].append(_console_citation(citation))
        for citation in _list_of_dicts(retrieval.get("citations")):
            if not citation.get("source_item_id"):
                continue
            citation_key = (citation.get("source_item_id"), citation.get("chunk_id"))
            if citation_key in citation_keys:
                continue
            citation_keys.add(citation_key)
            merged["citations"].append(_console_citation(citation))
        for key in ("graph_paths", "memory_context", "profile_context"):
            merged[key].extend(_list_of_dicts(retrieval.get(key)))
        diagnostics = retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {}
        merged_diagnostics = merged["diagnostics"]
        for key in ("gaps", "conflicts", "sensitivity"):
            merged_diagnostics[key].extend(list(diagnostics.get(key) or []))
        if diagnostics.get("score_debug"):
            merged_diagnostics["score_debug"] = diagnostics.get("score_debug")
    merged["citations"] = _dedupe_source_ref_dicts(_list_of_dicts(merged.get("citations")))
    return merged


def _ask_quick_answer(query: str, retrieval: dict[str, Any], *, ask_intent: str = "kb_search") -> str:
    results = _list_of_dicts(retrieval.get("results"))
    if not results:
        return f"关键结论：当前 PSKA 没有找到足够证据回答“{query}”。建议补充相关资料或扩大检索范围后再问。"
    if ask_intent == "writing":
        facts = _ask_requested_label_value_facts_from_results(query, results, limit=6)
        if not facts:
            facts = _ask_clean_facts_from_results(results[:1], limit=6)
        if facts:
            return _ask_quick_writing_answer(query, facts)
    facts = _ask_table_field_facts_from_results(query, results, limit=12)
    if facts and _ask_query_requests_only_values(query):
        return "\n".join(facts)
    if not facts:
        facts = _ask_structured_facts_from_results(query, results, limit=6)
    if not facts:
        facts = _ask_requested_label_value_facts_from_results(query, results, limit=6)
    if not facts:
        facts = _ask_clean_facts_from_results(results, limit=6)
    if not facts:
        return f"关键结论：PSKA 找到了与“{query}”相关的来源，但当前片段不足以整理成可引用结论。请查看证据列表或扩大检索范围。"
    clean_facts = [_ask_trim_sentence_punctuation(fact) for fact in facts if _ask_trim_sentence_punctuation(fact)]
    if len(clean_facts) == 1 and "=" in clean_facts[0]:
        lines = [f"关键结论：根据当前资料，{clean_facts[0]}。"]
    elif len(facts) <= 2:
        lines = [f"关键结论：{'；'.join(clean_facts)}。"]
    else:
        lines = [f"关键结论：{'；'.join(clean_facts)}。"]
    diagnostics = retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {}
    if diagnostics.get("gaps") or diagnostics.get("conflicts"):
        lines.append("不确定性：存在检索缺口或证据冲突，报告中应保留限定表述。")
    return "\n".join(lines)


def _ask_polish_quick_supported_answer(answer: str, *, ask_intent: str) -> str:
    text = str(answer or "").strip()
    if not text:
        return text
    if ask_intent != "writing":
        text = re.sub(r"^(?:关键结论[:：]\s*)?当前资料支持以下结论[:：]\s*", "关键结论：", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Z])", ". ", text)
    return text


def _ask_should_use_deterministic_coverage_guard(query: str, answer: str, deterministic_answer: str, *, ask_intent: str = "kb_search") -> bool:
    if not answer.strip() or not deterministic_answer.strip():
        return False
    if ask_intent == "writing" and _ask_answer_looks_like_raw_evidence_listing(answer):
        return True
    if ask_intent == "writing" and _ask_query_requests_preserved_numbers(query):
        answer_text = answer.casefold()
        return any(value.casefold() not in answer_text for value in _ask_numeric_values(deterministic_answer))
    if not _ask_query_requests_multiple_values(query):
        return False
    answer_text = answer.casefold()
    for value in _ask_numeric_values(deterministic_answer):
        if value.casefold() not in answer_text:
            return True
    return False


def _ask_query_requests_preserved_numbers(query: str) -> bool:
    text = str(query or "").casefold()
    return any(marker in text for marker in ("保留数字", "保留数值", "保留具体数字", "preserve numbers", "keep numbers", "include numbers"))


def _ask_answer_looks_like_raw_evidence_listing(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    if "当前资料支持以下结论" in text:
        return True
    if text.count(" / ") >= 3:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 4 and lines[0].startswith(("关键结论", "结论")):
        short_fact_lines = sum(1 for line in lines[1:] if len(line) <= 180 and re.search(r"\d|=| is |为|：|:", line, flags=re.IGNORECASE))
        return short_fact_lines >= 3
    return False


def _ask_quick_writing_answer(query: str, facts: list[str]) -> str:
    clean_facts = [_ask_trim_sentence_punctuation(fact) for fact in facts if _ask_trim_sentence_punctuation(fact)]
    if not clean_facts:
        return ""
    prefix = "文稿"
    folded = str(query or "").casefold()
    if any(marker in folded for marker in ("摘要", "summary")):
        prefix = "报告摘要"
    elif any(marker in folded for marker in ("brief", "简报")):
        prefix = "简报"
    body = "；".join(clean_facts[:6])
    return f"{prefix}：{body}。"


def _ask_trim_sentence_punctuation(value: str) -> str:
    return str(value or "").strip().strip(" \t\r\n。.!！？；;")


def _ask_query_requests_multiple_values(query: str) -> bool:
    text = str(query or "")
    if any(marker in text for marker in ["、", ",", "，", "/", "以及", "分别"]):
        return True
    return bool(re.search(r"\b(and|plus)\b", text, flags=re.IGNORECASE)) or "和" in text


def _ask_numeric_values(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_-])\d+(?:[.,]\d+)*(?:\s*(?:ms|s|usd|rmb|元|万元|亿元|%))?", str(text or ""), flags=re.IGNORECASE):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def _ask_quick_synthesis_prompt(
    *,
    query: str,
    rewrite_query: str,
    ask_intent: str,
    evidence: dict[str, Any],
    evidence_check: dict[str, Any],
) -> str:
    evidence_items = _ask_quick_synthesis_evidence_items(evidence)
    payload = {
        "question": query,
        "rewrite_query": rewrite_query,
        "evidence_check": {
            "status": evidence_check.get("status"),
            "scope_mode": evidence_check.get("scope_mode"),
            "supporting_citation_count": evidence_check.get("supporting_citation_count"),
        },
        "ask_intent": ask_intent,
        "evidence_claims": list(evidence_check.get("evidence_claims") or [])[:8],
        "evidence": evidence_items,
    }
    writing_instruction = ""
    if ask_intent == "writing":
        writing_instruction = (
            "The user is asking for a writing output. Compose the requested prose, brief, draft, or summary; "
            "respect requested length and format; preserve exact numbers and named entities from evidence. "
            "Do not merely list raw evidence snippets unless the user asked for a list.\n"
        )
    return (
        "You are PSKA's quick-answer final synthesizer. You are called through the agentic service, "
        "but this is not a research loop. Do not call tools, do not retrieve, and do not use outside knowledge.\n"
        f"{writing_instruction}"
        "Use only the evidence JSON below, which PSKA already filtered by ACL, scope, and citation support. "
        "Answer the user's question in natural human language, not as raw evidence snippets. Preserve exact "
        "numbers, units, entity names, dates, row identifiers, and table field values from the evidence. "
        "Every explicitly requested field/value that appears in evidence_claims must be represented in the answer. "
        "If the evidence has multiple candidate values, explain the distinction briefly instead of guessing. "
        "If the evidence is insufficient, say so clearly.\n"
        "Return ONLY JSON: {\"answer\": \"...\"}. The answer should be Chinese by default, concise, "
        "conclusion-first, and suitable for a financial institution user to paste into a note. Use one "
        "sentence when the question asks for a single value or fact; otherwise use at most three short "
        "sentences or three bullets. Do not repeat the user's question, explain PSKA internals, or add "
        "generic caveats when the evidence check is supported.\n\n"
        f"Evidence JSON: {json.dumps(payload, ensure_ascii=False)}"
    )


def _ask_quick_synthesis_evidence_items(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    windows = _list_of_dicts(evidence.get("source_windows"))
    if not windows:
        windows = _list_of_dicts(evidence.get("results"))
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(windows):
        source_id = str(item.get("source_item_id") or "")
        chunk_id = str(item.get("chunk_id") or item.get("passage_window_id") or "")
        title = str(item.get("title") or "")
        key = (source_id, chunk_id, title)
        if key in seen:
            continue
        seen.add(key)
        source_window = item.get("source_window") if isinstance(item.get("source_window"), dict) else {}
        text = str(item.get("text") or item.get("snippet") or source_window.get("text") or "")
        if not text.strip():
            continue
        items.append(
            {
                "ref": f"E{len(items) + 1}",
                "title": title,
                "source_item_id": source_id,
                "chunk_id": chunk_id,
                "text": _ask_clean_evidence_text(text)[:1200],
            }
        )
        if len(items) >= 8:
            break
    return items


def _ask_answer_from_agentic_synthesis(value: Any) -> str:
    parsed = _ask_json_object_from_text(str(value or ""))
    if not isinstance(parsed, dict):
        return ""
    answer = str(parsed.get("answer") or "").strip()
    if len(answer) < 2:
        return ""
    return answer


def _ask_table_field_facts_from_results(query: str, results: list[dict[str, Any]], *, limit: int) -> list[str]:
    for text in _ask_result_text_candidates(results):
        for headers, rows in _ask_markdown_tables(text):
            requested_fields = _ask_requested_table_fields(query, headers)
            if not requested_fields:
                continue
            row = _ask_best_matching_table_row(query, headers, rows)
            if row is None:
                continue
            facts: list[str] = []
            for field in requested_fields:
                value = str(row.get(field) or "").strip()
                if value:
                    facts.append(f"{field} = {value}")
                if len(facts) >= limit:
                    return facts
            if facts:
                return facts
    return []


def _ask_result_text_candidates(results: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for result in results:
        parts = [str(result.get("snippet") or "")]
        source_window = result.get("source_window") if isinstance(result.get("source_window"), dict) else {}
        if source_window.get("text"):
            parts.append(str(source_window.get("text") or ""))
        text = "\n".join(part for part in parts if part)
        if text.strip():
            texts.append(text)
    return texts


def _ask_markdown_tables(text: str) -> list[tuple[list[str], list[dict[str, str]]]]:
    tables: list[tuple[list[str], list[dict[str, str]]]] = []
    block: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if "|" in line and len(_ask_markdown_cells(line)) >= 2:
            block.append(line)
            continue
        if block:
            _ask_append_markdown_table(tables, block)
            block = []
    if block:
        _ask_append_markdown_table(tables, block)
    return tables


def _ask_append_markdown_table(tables: list[tuple[list[str], list[dict[str, str]]]], block: list[str]) -> None:
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in block:
        cells = _ask_markdown_cells(line)
        if not cells or _ask_is_markdown_separator(cells):
            continue
        if header is None:
            header = cells
            continue
        padded = [*cells, *([""] * max(0, len(header) - len(cells)))]
        rows.append({field: padded[index].strip() for index, field in enumerate(header)})
    if header and rows:
        tables.append((header, rows))


def _ask_markdown_cells(line: str) -> list[str]:
    stripped = str(line or "").strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip().strip("`") for cell in stripped.split("|")]


def _ask_is_markdown_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells if cell.strip())


def _ask_requested_table_fields(query: str, headers: list[str]) -> list[str]:
    segment = _ask_output_field_segment(query) or query
    fields = [header for header in headers if _ask_text_mentions_identifier(segment, header)]
    if fields:
        return fields
    return [header for header in headers if _ask_text_mentions_identifier(query, header)]


def _ask_output_field_segment(query: str) -> str:
    text = str(query or "")
    lowered = text.casefold()
    markers = ["只输出", "仅输出", "输出", "only output", "return only", "provide only", "just output"]
    starts = [lowered.find(marker) + len(marker) for marker in markers if lowered.find(marker) >= 0]
    if not starts:
        return ""
    segment = text[min(starts) :]
    return _ask_positive_query_segment(segment)


def _ask_positive_query_segment(query: str) -> str:
    return re.split(r"不要|不需要|不得|请勿|do not|don't|without", str(query or ""), maxsplit=1, flags=re.IGNORECASE)[0]


def _ask_query_requests_only_values(query: str) -> bool:
    folded = str(query or "").casefold()
    return any(marker in folded for marker in ("只输出", "仅输出", "only output", "return only", "just output"))


def _ask_requested_label_value_facts_from_results(query: str, results: list[dict[str, Any]], *, limit: int) -> list[str]:
    labels = _ask_requested_fact_labels(query)
    if not labels:
        return []
    facts_by_label: dict[str, str] = {}
    for text in _ask_result_text_candidates(results):
        clean_text = _ask_clean_evidence_text(text)
        for sentence in _ask_fact_sentences(clean_text):
            for label in labels:
                key = label.casefold()
                if key in facts_by_label:
                    continue
                if _ask_text_mentions_identifier(sentence, label):
                    facts_by_label[key] = sentence
                    break
        if len(facts_by_label) >= min(len(labels), limit):
            break
    facts: list[str] = []
    seen: set[str] = set()
    for label in labels:
        fact = facts_by_label.get(label.casefold())
        if not fact:
            continue
        normalized = re.sub(r"\s+", " ", fact).strip().casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        facts.append(fact)
        if len(facts) >= limit:
            break
    return facts


def _ask_requested_fact_labels(query: str) -> list[str]:
    segment = _ask_output_field_segment(query) or _ask_positive_query_segment(query)
    segment = _ask_question_core_segment(segment)
    if not segment.strip():
        return []
    if _ask_is_writing_query(segment) and not _ask_query_requests_multiple_values(segment):
        return []
    normalized = re.sub(r"\b(?:and|plus)\b", "、", segment, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(?:or)\b", "、", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("以及", "、").replace("及", "、").replace("和", "、")
    normalized = re.sub(r"[,，/;；\n]+", "、", normalized)
    labels: list[str] = []
    seen: set[str] = set()
    for raw_part in normalized.split("、"):
        label = _ask_clean_requested_fact_label(raw_part)
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
        if len(labels) >= 8:
            break
    return labels


def _ask_question_core_segment(text: str) -> str:
    segment = str(text or "")
    for marker in ("分别是什么", "分别是多少", "是什么", "是多少", "有哪些", "多少", "吗", "?","？"):
        index = segment.find(marker)
        if index >= 0:
            segment = segment[:index]
            break
    for marker in ("里的", "中的", "的"):
        index = segment.rfind(marker)
        if index >= 0 and len(segment) - index <= 120:
            candidate = segment[index + len(marker) :]
            if candidate.strip():
                segment = candidate
                break
    return segment


def _ask_clean_requested_fact_label(value: str) -> str:
    label = str(value or "").strip(" \t\r\n'\"“”‘’()（）[]【】{}<>《》:：")
    label = re.sub(r"^(?:请问|请|帮我|告诉我|基于|根据|按照|在|从)\s*", "", label)
    label = re.sub(r"(?:是什么|是多少|有哪些|多少|为何|为什么|吗|呢)$", "", label).strip()
    label = re.sub(r"\b(?:what is|what are|which is|which are|please|show me|tell me)\b", "", label, flags=re.IGNORECASE).strip()
    label = re.sub(r"\s+", " ", label).strip(" \t\r\n'\"“”‘’()（）[]【】{}<>《》:：")
    if not label or len(label) > 64:
        return ""
    if _ask_generic_requested_fact_label(label):
        return ""
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", label):
        return ""
    return label


def _ask_generic_requested_fact_label(label: str) -> bool:
    folded = label.casefold()
    generic_exact = {
        "资料",
        "这份资料",
        "当前资料",
        "信息",
        "内容",
        "答案",
        "结论",
        "结果",
        "字段",
        "数字",
        "数值",
        "报告摘要",
        "摘要",
        "summary",
        "brief",
        "draft",
    }
    if folded in generic_exact:
        return True
    return any(marker in folded for marker in ("写一段", "写成", "改写", "rewrite", "compose", "draft a", "summary of"))


def _ask_text_mentions_identifier(text: str, identifier: str) -> bool:
    needle = str(identifier or "").strip().casefold()
    haystack = str(text or "").casefold()
    if not needle:
        return False
    if re.search(r"[a-z0-9_]", needle):
        return re.search(rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])", haystack) is not None
    return needle in haystack


def _ask_best_matching_table_row(query: str, headers: list[str], rows: list[dict[str, str]]) -> dict[str, str] | None:
    positive_query = _ask_positive_query_segment(query).casefold()
    tokens = _ask_table_query_tokens(positive_query)
    best: dict[str, str] | None = None
    best_score = 0
    for row in rows:
        row_values = [str(row.get(header) or "") for header in headers]
        row_text = " ".join(row_values).casefold()
        score = 0
        for header, value in row.items():
            if value and _ask_text_mentions_identifier(positive_query, header) and str(value).casefold() in positive_query:
                score += 5
        score += sum(1 for token in tokens if token in row_text)
        if score > best_score:
            best = row
            best_score = score
    return best if best_score > 0 else None


def _ask_table_query_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9][a-z0-9_.-]*", str(text or "").casefold()):
        normalized = token.strip("._-")
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        tokens.append(normalized)
    return tokens


def _ask_structured_facts_from_results(query: str, results: list[dict[str, Any]], *, limit: int) -> list[str]:
    requested = set(_ask_structural_evidence_hits(query, "https://example.com info@example.com +1-202-555-0100"))
    if not requested:
        return []
    texts = _ask_result_text_candidates(results)
    combined = "\n".join(texts)
    facts: list[str] = []
    if "url" in requested:
        urls = _extract_structured_values(combined, r"(?:https?://|www\.)[^\s|,，。；;]+|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", max_values=4)
        if urls:
            facts.append("网址：" + "；".join(urls))
    if "phone" in requested:
        phones = _extract_phone_values(combined, max_values=4)
        if phones:
            facts.append("联系电话：" + "；".join(phones))
    if "email" in requested:
        emails = _extract_structured_values(combined.casefold(), r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", max_values=4)
        if emails:
            facts.append("邮箱：" + "；".join(emails))
    return facts[:limit]


def _extract_structured_values(text: str, pattern: str, *, max_values: int) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(pattern, str(text or ""), flags=re.IGNORECASE):
        value = match.group(0).strip(" \t\r\n,，。；;:：()[]{}<>《》\"'")
        if value.casefold().startswith("jwww."):
            value = value[1:]
        key = value.casefold()
        if len(value) < 4 or key in seen:
            continue
        seen.add(key)
        values.append(value)
        if len(values) >= max_values:
            break
    return values


def _extract_phone_values(text: str, *, max_values: int) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\+?\d[\d\s().-]{5,}\d", str(text or "")):
        value = re.sub(r"\s+", " ", match.group(0)).strip(" \t\r\n,，。；;:：()[]{}<>《》\"'")
        digits = re.sub(r"\D", "", value)
        if len(digits) < 7 or len(digits) > 18:
            continue
        if not any(ch in value for ch in "+-()"):
            continue
        key = re.sub(r"\D", "", value)
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
        if len(values) >= max_values:
            break
    return values


def _ask_hydrate_retrieval_source_windows(
    store: Any,
    retrieval: dict[str, Any],
    *,
    query: str,
    tenant_id: str,
    owner_user_id: str,
    max_windows: int = 6,
    max_chars: int = 1800,
) -> dict[str, Any]:
    results = _list_of_dicts(retrieval.get("results"))
    citations = _list_of_dicts(retrieval.get("citations"))
    source_ids = {
        str(ref.get("source_item_id") or "")
        for ref in [*results, *citations]
        if str(ref.get("source_item_id") or "").strip()
    }
    if not source_ids:
        return retrieval
    visible_items = {
        item.source_item_id: item
        for item in store.list_source_items(tenant_id=tenant_id)
        if item.owner_user_id == owner_user_id and item.source_item_id in source_ids and _is_active_lifecycle(item)
    }
    if not visible_items:
        return retrieval
    documents = [
        document
        for document in store.list_documents_for_sources(set(visible_items))
        if getattr(document, "owner_user_id", "") == owner_user_id and _is_active_lifecycle(document)
    ]
    chunks = [
        chunk
        for chunk in store.list_chunks_for_sources(set(visible_items))
        if getattr(chunk, "owner_user_id", "") == owner_user_id and _is_active_lifecycle(chunk)
    ]
    documents_by_id = {str(getattr(document, "document_id", "") or ""): document for document in documents}
    documents_by_source: dict[str, list[Any]] = {}
    chunks_by_id = {str(getattr(chunk, "chunk_id", "") or ""): chunk for chunk in chunks}
    chunks_by_source: dict[str, list[Any]] = {}
    for document in documents:
        documents_by_source.setdefault(str(getattr(document, "source_item_id", "") or ""), []).append(document)
    for chunk in chunks:
        chunks_by_source.setdefault(str(getattr(chunk, "source_item_id", "") or ""), []).append(chunk)
    for source_chunks in chunks_by_source.values():
        source_chunks.sort(key=lambda chunk: (str(getattr(chunk, "document_id", "") or ""), int(getattr(chunk, "ordinal", 0) or 0)))

    source_windows: list[dict[str, Any]] = []
    window_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results[:max_windows]:
        window = _ask_source_window_for_result(
            result,
            items_by_id=visible_items,
            documents_by_id=documents_by_id,
            documents_by_source=documents_by_source,
            chunks_by_id=chunks_by_id,
            chunks_by_source=chunks_by_source,
            query=query,
            max_chars=max_chars,
        )
        if not window:
            continue
        key = (str(window.get("source_item_id") or ""), str(window.get("chunk_id") or window.get("passage_window_id") or ""))
        window_by_key[key] = window
        source_windows.append(window)
        snippet = _ask_clean_evidence_text(str(window.get("text") or ""))[:900]
        result["document_id"] = window.get("document_id")
        result["passage_window_id"] = window.get("passage_window_id")
        result["snippet"] = snippet
        result["source_window"] = window
        citation = result.get("citation") if isinstance(result.get("citation"), dict) else {}
        result["citation"] = {
            **citation,
            "source_item_id": window.get("source_item_id"),
            "document_id": window.get("document_id"),
            "chunk_id": window.get("chunk_id") or citation.get("chunk_id"),
            "passage_window_id": window.get("passage_window_id"),
            "title": citation.get("title") or window.get("title"),
            "url": citation.get("url") or window.get("url"),
            "snippet": snippet[:600],
        }

    hydrated_citations: list[dict[str, Any]] = []
    for citation in citations:
        key = (str(citation.get("source_item_id") or ""), str(citation.get("chunk_id") or citation.get("passage_window_id") or ""))
        window = window_by_key.get(key)
        if not window and citation.get("source_item_id"):
            window = _ask_source_window_for_result(
                citation,
                items_by_id=visible_items,
                documents_by_id=documents_by_id,
                documents_by_source=documents_by_source,
                chunks_by_id=chunks_by_id,
                chunks_by_source=chunks_by_source,
                query=query,
                max_chars=max_chars,
            )
        if window:
            snippet = _ask_clean_evidence_text(str(window.get("text") or ""))[:600]
            hydrated_citations.append(
                {
                    **citation,
                    "document_id": citation.get("document_id") or window.get("document_id"),
                    "passage_window_id": citation.get("passage_window_id") or window.get("passage_window_id"),
                    "title": citation.get("title") or window.get("title"),
                    "url": citation.get("url") or window.get("url"),
                    "snippet": snippet,
                    "source_window": window,
                }
            )
        else:
            hydrated_citations.append(citation)
    retrieval["results"] = results
    retrieval["citations"] = hydrated_citations
    retrieval["source_windows"] = source_windows
    diagnostics = retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {}
    diagnostics["source_window_count"] = len(source_windows)
    retrieval["diagnostics"] = diagnostics
    return retrieval


def _ask_enrich_retrieval_knowledge_bases(
    store: Any,
    retrieval: dict[str, Any],
    *,
    scope: dict[str, Any],
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    selected_knowledge_base_ids = set(_knowledge_base_ids_from_scope(scope))
    lineage_cache: dict[str, dict[str, Any]] = {}

    def lineage_for_source(source_item_id: str) -> dict[str, Any]:
        if not source_item_id:
            return {}
        if source_item_id in lineage_cache:
            return lineage_cache[source_item_id]
        all_ids = sorted(
            store.list_knowledge_base_ids_for_source_item(
                source_item_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
        )
        scoped_ids = [knowledge_base_id for knowledge_base_id in all_ids if not selected_knowledge_base_ids or knowledge_base_id in selected_knowledge_base_ids]
        knowledge_base_ids = scoped_ids or all_ids
        knowledge_base_names: list[str] = []
        for knowledge_base_id in knowledge_base_ids:
            try:
                knowledge_base = store.get_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            except KeyError:
                continue
            name = str(getattr(knowledge_base, "name", "") or getattr(knowledge_base, "slug", "") or knowledge_base_id).strip()
            if name:
                knowledge_base_names.append(name)
        lineage = {
            "knowledge_base_ids": knowledge_base_ids,
            "knowledge_base_names": knowledge_base_names,
        }
        if len(knowledge_base_ids) == 1:
            lineage["knowledge_base_id"] = knowledge_base_ids[0]
        if len(knowledge_base_names) == 1:
            lineage["knowledge_base_name"] = knowledge_base_names[0]
        lineage_cache[source_item_id] = lineage
        return lineage

    def enrich_ref(ref: dict[str, Any]) -> dict[str, Any]:
        source_item_id = str(ref.get("source_item_id") or "").strip()
        if not source_item_id:
            return ref
        lineage = lineage_for_source(source_item_id)
        if not lineage.get("knowledge_base_ids"):
            return ref
        return {**ref, **lineage}

    results: list[dict[str, Any]] = []
    for result in _list_of_dicts(retrieval.get("results")):
        enriched = enrich_ref(result)
        citation = result.get("citation") if isinstance(result.get("citation"), dict) else {}
        if citation:
            enriched["citation"] = enrich_ref(citation)
        source_window = result.get("source_window") if isinstance(result.get("source_window"), dict) else {}
        if source_window:
            enriched["source_window"] = enrich_ref(source_window)
        results.append(enriched)

    enriched = dict(retrieval)
    enriched["results"] = results
    enriched["citations"] = [enrich_ref(citation) for citation in _list_of_dicts(retrieval.get("citations"))]
    enriched["source_windows"] = [enrich_ref(window) for window in _list_of_dicts(retrieval.get("source_windows"))]
    return enriched


def _ask_source_window_for_result(
    ref: dict[str, Any],
    *,
    items_by_id: dict[str, Any],
    documents_by_id: dict[str, Any],
    documents_by_source: dict[str, list[Any]],
    chunks_by_id: dict[str, Any],
    chunks_by_source: dict[str, list[Any]],
    query: str,
    max_chars: int,
) -> dict[str, Any] | None:
    source_item_id = str(ref.get("source_item_id") or "").strip()
    item = items_by_id.get(source_item_id)
    if item is None:
        return None
    chunk = chunks_by_id.get(str(ref.get("chunk_id") or "").strip())
    document = documents_by_id.get(str(ref.get("document_id") or "").strip())
    if document is None and chunk is not None:
        document = documents_by_id.get(str(getattr(chunk, "document_id", "") or ""))
    if document is None:
        source_documents = documents_by_source.get(source_item_id) or []
        document = source_documents[0] if source_documents else None
    source_chunks = chunks_by_source.get(source_item_id) or []
    if chunk is None and source_chunks:
        chunk = _ask_best_chunk_for_window(source_chunks, query=query, fallback_snippet=str(ref.get("snippet") or ""))
    body = str(getattr(document, "body", "") or "") if document is not None else str(getattr(item, "content_text", "") or "")
    anchor_text = str(getattr(chunk, "text", "") or ref.get("snippet") or "")
    if chunk is not None and anchor_text.strip():
        text = query_focused_evidence_snippet(anchor_text, query, max_chars=max_chars)
        if not text:
            text = _ask_neighbor_chunk_window(source_chunks, anchor_chunk=chunk, max_chars=max_chars)
        start = 0
        end = len(text)
        policy = "retrieved_chunk_focused"
    elif source_chunks:
        text = _ask_neighbor_chunk_window(source_chunks, anchor_chunk=chunk, max_chars=max_chars)
        start = 0
        end = len(text)
        policy = "neighbor_chunks"
    elif body:
        text, start, end = _ask_document_window_text(body, anchor_text=anchor_text, query=query, max_chars=max_chars)
        text = query_focused_evidence_snippet(text, query, max_chars=max_chars) or text
        policy = "document_body_around_retrieved_chunk"
    else:
        text = ""
        start = 0
        end = 0
        policy = "empty"
    if not text.strip():
        return None
    document_id = str(getattr(document, "document_id", "") or getattr(chunk, "document_id", "") or "")
    chunk_id = str(getattr(chunk, "chunk_id", "") or ref.get("chunk_id") or "")
    source_title = str(getattr(document, "title", "") or getattr(item, "title", "") or source_item_id)
    ordinal = int(getattr(chunk, "ordinal", 0) or 0)
    return {
        "source_item_id": source_item_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "passage_window_id": f"askpw_{document_id or source_item_id}_{ordinal}_{start}_{end}",
        "title": source_title,
        "url": getattr(item, "url", None),
        "text": text,
        "start_char": start,
        "end_char": end,
        "window_policy": policy,
    }


def _ask_best_chunk_for_window(chunks: list[Any], *, query: str, fallback_snippet: str) -> Any | None:
    if not chunks:
        return None
    anchors = [term.casefold() for term in _ask_query_terms(query)]
    fallback = _ask_clean_evidence_text(fallback_snippet).casefold()

    def score(chunk: Any) -> tuple[int, int]:
        text = str(getattr(chunk, "text", "") or "").casefold()
        anchor_score = sum(1 for anchor in anchors if anchor and anchor in text)
        fallback_score = 1 if fallback and fallback[:80] in text else 0
        return (anchor_score + fallback_score, -int(getattr(chunk, "ordinal", 0) or 0))

    return max(chunks, key=score)


def _ask_document_window_text(body: str, *, anchor_text: str, query: str, max_chars: int) -> tuple[str, int, int]:
    normalized_body = body.casefold()
    candidates = [anchor_text]
    candidates.extend(_ask_query_terms(query))
    index = -1
    for candidate in candidates:
        candidate = _ask_clean_evidence_text(candidate).strip()
        if len(candidate) > 180:
            candidate = candidate[:180]
        if len(candidate) < 2:
            continue
        index = normalized_body.find(candidate.casefold())
        if index >= 0:
            break
    if index < 0:
        index = 0
    start = max(0, index - max_chars // 4)
    end = min(len(body), start + max_chars)
    start, end = _ask_expand_to_paragraph_boundaries(body, start, end, max_chars=max_chars)
    return body[start:end].strip(), start, end


def _ask_expand_to_paragraph_boundaries(body: str, start: int, end: int, *, max_chars: int) -> tuple[int, int]:
    paragraph_start = body.rfind("\n\n", 0, start)
    if paragraph_start >= 0 and end - paragraph_start <= max_chars:
        start = paragraph_start + 2
    paragraph_end = body.find("\n\n", end)
    if paragraph_end >= 0 and paragraph_end - start <= max_chars:
        end = paragraph_end
    return start, end


def _ask_neighbor_chunk_window(chunks: list[Any], *, anchor_chunk: Any | None, max_chars: int) -> str:
    if not chunks:
        return ""
    ordered = sorted(chunks, key=lambda chunk: int(getattr(chunk, "ordinal", 0) or 0))
    if anchor_chunk is None:
        anchor_index = 0
    else:
        anchor_id = str(getattr(anchor_chunk, "chunk_id", "") or "")
        anchor_index = next((index for index, chunk in enumerate(ordered) if str(getattr(chunk, "chunk_id", "") or "") == anchor_id), 0)
    selected = [ordered[anchor_index]]
    left = anchor_index - 1
    right = anchor_index + 1
    while len("\n\n".join(str(getattr(chunk, "text", "") or "") for chunk in selected)) < max_chars and (left >= 0 or right < len(ordered)):
        if left >= 0:
            selected.insert(0, ordered[left])
            left -= 1
        if len("\n\n".join(str(getattr(chunk, "text", "") or "") for chunk in selected)) >= max_chars:
            break
        if right < len(ordered):
            selected.append(ordered[right])
            right += 1
    return "\n\n".join(str(getattr(chunk, "text", "") or "") for chunk in selected)[:max_chars].strip()


def _ask_clean_facts_from_results(results: list[dict[str, Any]], *, limit: int) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for item in results:
        text = _ask_clean_evidence_text(str(item.get("snippet") or ""))
        for sentence in _ask_fact_sentences(text):
            key = re.sub(r"\s+", " ", sentence).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            facts.append(sentence)
            if len(facts) >= limit:
                return facts
    return facts


def _ask_clean_evidence_text(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
        else:
            text = re.sub(r"^\s*---\s+.*?\s+---\s*", "", text, count=1)
    text = re.sub(r"(?<!#)#{1,6}\s+", "", text)
    text = re.sub(r"\s*\|\s*-{2,}\s*(?:\|\s*-{2,}\s*)+\|?", " ", text)
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            if not line:
                continue
        if lowered.startswith(("title:", "type:", "slug:", "aliases:", "date:", "attendees:", "tags:")):
            continue
        if re.fullmatch(r"[-:| ]{5,}", line):
            continue
        cleaned_lines.append(line)
    text = " ".join(cleaned_lines)
    text = re.sub(r"\b#{1,6}\s*", "", text)
    text = re.sub(r"\s*\|\s*", " / ", text)
    text = re.sub(r"\s*/\s*/\s*", " / ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _ask_fact_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[。！？.!?])\s+|\s*[；;]\s+|\n+", text)
    sentences: list[str] = []
    for part in parts:
        sentence = part.strip(" -\t\r\n")
        if not sentence:
            continue
        if len(sentence) < 8 and not re.search(r"\d", sentence):
            continue
        if len(sentence) > 180:
            sentence = sentence[:177].rstrip() + "..."
        if _ask_answer_quality_flags(sentence):
            continue
        sentences.append(sentence)
    return sentences


def _ask_deep_response(
    *,
    query: str,
    intent: str,
    surface: str,
    tenant_id: str,
    owner_user_id: str,
    selected_intent: str,
    understand: dict[str, Any] | None = None,
    agentic: dict[str, Any],
    started_at: float,
    allowed_tools: list[str],
    tool_policy: dict[str, Any] | None = None,
    store: Any,
) -> dict[str, Any]:
    understand = understand or _ask_understand_payload(
        query=query,
        intent=intent,
        forced_ask_intent=None,
        scope={},
        surface=surface,
    )
    ask_intent = str(understand.get("intent") or "kb_search")
    rewrite_query = str(understand.get("rewrite_query") or query)
    trace = agentic.get("trace") if isinstance(agentic.get("trace"), dict) else {}
    retrieval_payload = agentic.get("retrieval") if isinstance(agentic.get("retrieval"), dict) else {}
    retrieval = _console_search_summary(to_jsonable(retrieval_payload)) if retrieval_payload else {}
    if not _list_of_dicts(retrieval.get("results")):
        retrieval = _ask_retrieval_from_agentic_trace(trace)
    if retrieval:
        retrieval = _ask_hydrate_retrieval_source_windows(
            store,
            retrieval,
            query=rewrite_query,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
    evidence = _ask_evidence_from_retrieval(retrieval)
    declared_source_refs = _ask_source_ref_dicts(agentic.get("source_refs"), string_field="source_item_id")
    declared_citation_refs = _ask_source_ref_dicts(agentic.get("citations"), string_field="title")
    declared_refs = declared_source_refs or declared_citation_refs
    fallback_refs = [
        *_list_of_dicts(retrieval.get("citations") if retrieval else []),
        *_list_of_dicts(evidence.get("citations")),
    ]
    raw_refs = declared_refs or fallback_refs
    refs, dropped_refs = _ask_validate_source_refs(raw_refs, store=store, tenant_id=tenant_id, owner_user_id=owner_user_id)
    if refs:
        ref_evidence = _ask_source_refs_as_evidence(
            refs,
            store=store,
            query=rewrite_query,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        if declared_refs:
            evidence = _ask_filter_evidence_to_refs(evidence, refs)
        evidence["results"] = [*_list_of_dicts(evidence.get("results")), *_list_of_dicts(ref_evidence.get("results"))]
        evidence["source_windows"] = [
            *_list_of_dicts(evidence.get("source_windows")),
            *_list_of_dicts(ref_evidence.get("source_windows")),
        ]
        hydrated_refs = _ask_merge_hydrated_source_refs(refs, _list_of_dicts(ref_evidence.get("citations")))
        evidence["citations"] = hydrated_refs or refs
        evidence["source_refs"] = hydrated_refs or refs
    scope_applied = understand.get("scope_applied") if isinstance(understand.get("scope_applied"), dict) else {}
    evidence_check = _ask_verify_evidence(
        query=rewrite_query,
        evidence=evidence,
        scope={"source_item_ids": scope_applied.get("source_item_ids") or [], "mode": scope_applied.get("mode") or "soft"},
        ask_intent=ask_intent,
    )
    evidence = _ask_apply_evidence_check(evidence, evidence_check)
    answer = str(agentic.get("answer") or "").strip()
    answer_type = "deep_answer"
    if evidence_check.get("status") != "supported":
        answer = _ask_no_answer_from_evidence_check(query, evidence_check)
        answer_type = "no_answer"
    trace = {
        **trace,
        "mode": "deep",
        "retrieval_owner": "fastreact_pska_mcp",
        "tool_policy": tool_policy or _ask_read_tool_policy(scope_applied, allowed_tools=allowed_tools),
        "tool_profile": ASK_READ_TOOL_PROFILE,
        "evidence_check": evidence_check,
    }
    agent_steps = _ask_agent_steps_from_events(trace.get("events") if isinstance(trace.get("events"), list) else [])
    if dropped_refs:
        trace["dropped_source_refs"] = dropped_refs
    trace = _ask_public_trace(trace)
    elapsed_ms = _elapsed_ms(started_at)
    return {
        "ok": True,
        "query": query,
        "intent": ask_intent,
        "rewrite_query": rewrite_query,
        "answer": answer,
        "answer_type": answer_type,
        "route": {
            "intent": ask_intent,
            "requested_intent": intent,
            "selected_intent": selected_intent,
            "retrieval_owner": "fastreact_pska_mcp",
            "surface": surface,
            "requires_agentic_service_online": True,
            "tool_policy": tool_policy or _ask_read_tool_policy(scope_applied, allowed_tools=allowed_tools),
            "tool_profile": ASK_READ_TOOL_PROFILE,
            "routing_owner": "pska_planner",
            "query_terms": _ask_query_terms(rewrite_query),
            "rewrite_query": rewrite_query,
            "scope_applied": scope_applied,
            "understand": understand,
            "intent_contract": understand.get("intent_contract") if isinstance(understand.get("intent_contract"), dict) else {},
        },
        "evidence": evidence,
        "citations": evidence["citations"],
        "source_refs": evidence["source_refs"],
        "citation_audit": {
            "used": evidence["citations"],
            "dropped": _list_of_dicts(evidence_check.get("dropped_citations")),
        },
        "evidence_check": evidence_check,
        "evidence_claims": list(evidence_check.get("evidence_claims") or []),
        "no_answer_reasons": list(evidence_check.get("no_answer_reasons") or []),
        "agent_steps": agent_steps,
        "trace": trace,
        "timing": {
            "total_ms": elapsed_ms,
            "time_to_first_answer_ms": elapsed_ms,
            "time_to_first_agent_event_ms": (agent_steps[0].get("elapsed_ms") if agent_steps[0].get("elapsed_ms") is not None else 0)
            if agent_steps
            else None,
        },
        "agentic_service": agentic.get("agentic_service") if isinstance(agentic.get("agentic_service"), dict) else {},
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
    }


def _ask_agent_steps_from_events(events: list[Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if _ask_is_stream_done_event(event):
            continue
        step = _ask_agent_step_from_event(event, sequence=len(steps) + 1, started_at=None)
        if step:
            steps.append(step)
    return steps


def _ask_is_stream_done_event(event: Mapping[str, Any]) -> bool:
    event_type = str(event.get("type") or event.get("event_type") or "").strip().lower()
    content = str(event.get("content") or event.get("data") or "").strip()
    return event_type == "done" or (event_type in {"", "message"} and content == "[DONE]")


def _ask_agent_step_from_event(event: dict[str, Any], *, sequence: int, started_at: float | None) -> dict[str, Any] | None:
    event_type = str(event.get("type") or event.get("event_type") or "").lower()
    tool_name = str(event.get("tool_name") or "")
    elapsed_ms = _elapsed_ms(started_at) if started_at is not None else _number_or_none(event.get("duration_ms"))
    if event_type == "session_start":
        return _ask_agent_step(
            sequence=sequence,
            phase="understand",
            status="complete",
            title="理解问题",
            detail="已确认问题和当前租户范围。",
            raw_event_id=event.get("event_id"),
            elapsed_ms=elapsed_ms,
        )
    if event_type == "think":
        return _ask_agent_step(
            sequence=sequence,
            phase="think",
            status="running",
            title=_ask_think_step_title(event),
            detail=_ask_think_step_detail(event),
            raw_event_id=event.get("event_id"),
            elapsed_ms=elapsed_ms,
        )
    if event_type == "tool_call":
        return _ask_agent_step(
            sequence=sequence,
            phase=_ask_tool_phase(tool_name),
            status="running",
            title=_ask_tool_step_title(tool_name, action="call"),
            detail=_ask_tool_call_detail(event),
            tool_name=tool_name or None,
            tool_call_id=event.get("tool_call_id"),
            raw_event_id=event.get("event_id"),
            elapsed_ms=elapsed_ms,
        )
    if event_type == "tool_result":
        counts = _ask_tool_result_counts(event)
        return _ask_agent_step(
            sequence=sequence,
            phase="read",
            status="complete",
            title=_ask_tool_step_title(tool_name, action="result"),
            detail=_ask_tool_result_detail(counts),
            tool_name=tool_name or None,
            tool_call_id=event.get("tool_call_id"),
            raw_event_id=event.get("event_id"),
            elapsed_ms=elapsed_ms,
            evidence_count=counts.get("evidence_count"),
            source_ref_count=counts.get("source_ref_count"),
        )
    if event_type == "session_end":
        return _ask_agent_step(
            sequence=sequence,
            phase="answer",
            status="complete",
            title="形成回答",
            detail="已完成证据归纳和引用校验。",
            raw_event_id=event.get("event_id"),
            elapsed_ms=elapsed_ms,
        )
    if event_type == "error":
        return _ask_agent_step(
            sequence=sequence,
            phase="error",
            status="error",
            title="分析失败",
            detail="深入分析过程中出现错误。",
            raw_event_id=event.get("event_id"),
            elapsed_ms=elapsed_ms,
        )
    return None


def _ask_agent_step(
    *,
    sequence: int,
    phase: str,
    status: str,
    title: str,
    detail: str,
    tool_name: str | None = None,
    tool_call_id: Any = None,
    evidence_count: Any = None,
    source_ref_count: Any = None,
    raw_event_id: Any = None,
    elapsed_ms: float | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    if elapsed_ms is None and started_at is not None:
        elapsed_ms = _elapsed_ms(started_at)
    return {
        "step_id": f"step_{sequence}",
        "phase": phase,
        "status": status,
        "title": title,
        "detail": detail,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "evidence_count": _number_or_none(evidence_count),
        "source_ref_count": _number_or_none(source_ref_count),
        "elapsed_ms": elapsed_ms,
        "raw_event_id": raw_event_id,
    }


def _ask_progress_from_step(step: dict[str, Any]) -> dict[str, Any]:
    stage = _ask_progress_stage(str(step.get("phase") or ""), str(step.get("tool_name") or ""))
    return {
        "stage": stage,
        "phase": step.get("phase"),
        "status": step.get("status"),
        "title": step.get("title"),
        "detail": step.get("detail"),
        "step_id": step.get("step_id"),
        "tool_name": step.get("tool_name"),
        "elapsed_ms": step.get("elapsed_ms"),
        "evidence_count": step.get("evidence_count"),
        "source_ref_count": step.get("source_ref_count"),
    }


def _ask_progress_stage(phase: str, tool_name: str) -> str:
    phase = phase.strip().lower()
    tool_name = tool_name.strip().lower()
    if phase == "route":
        return "route"
    if phase in {"understand", "think", "inspect"}:
        return "query_understand"
    if phase in {"search", "tool"} or tool_name == "pska_pska_search":
        return "search"
    if phase == "rerank":
        return "rerank"
    if phase == "graph" or tool_name == "pska_pska_graph_context":
        return "graph"
    if phase in {"read", "digest"} or tool_name in {"pska_pska_read_evidence_context", "pska_pska_digest_context"}:
        return "read"
    if phase in {"evidence_check", "answer"}:
        return "evidence_check" if phase == "evidence_check" else "generate"
    if phase == "error":
        return "evidence_check"
    return "generate"


def _ask_think_step_title(event: dict[str, Any]) -> str:
    content = str(event.get("content") or "")
    if "context_compression" in content.lower() or "context_compression" in str(event.get("metadata") or "").lower():
        return "整理上下文"
    return "思考下一步"


def _ask_think_step_detail(event: dict[str, Any]) -> str:
    content = str(event.get("content") or "").strip()
    if content.startswith("[CONTEXT_COMPRESSION]"):
        return "已压缩上下文以继续分析。"
    return "正在判断是否需要继续检索或形成结论。"


def _ask_tool_step_title(tool_name: str, *, action: str) -> str:
    if tool_name == "pska_pska_search":
        return "搜索 PSKA 知识库" if action == "call" else "读取搜索结果"
    if tool_name == "pska_pska_index_status":
        return "检查索引状态" if action == "call" else "读取索引状态"
    if tool_name == "pska_pska_read_evidence_context":
        return "读取原文证据" if action == "call" else "整理原文证据"
    if tool_name == "pska_pska_graph_context":
        return "扩展图谱证据" if action == "call" else "读取图谱关系"
    if tool_name == "pska_pska_digest_context":
        return "读取摘要事实" if action == "call" else "整理摘要事实"
    return "调用工具" if action == "call" else "读取工具结果"


def _ask_tool_phase(tool_name: str) -> str:
    if tool_name == "pska_pska_search":
        return "search"
    if tool_name == "pska_pska_read_evidence_context":
        return "read"
    if tool_name == "pska_pska_graph_context":
        return "graph"
    if tool_name == "pska_pska_digest_context":
        return "digest"
    if tool_name == "pska_pska_index_status":
        return "inspect"
    return "tool"


def _ask_tool_call_detail(event: dict[str, Any]) -> str:
    args = event.get("tool_args") if isinstance(event.get("tool_args"), dict) else {}
    query = str(args.get("query") or "").strip()
    top_k = args.get("top_k")
    pieces = []
    if query:
        pieces.append(f"查询：{query[:120]}")
    if top_k:
        pieces.append(f"top_k={top_k}")
    source_item_ids = _string_list(args.get("source_item_ids"))
    entity_labels = _string_list(args.get("entity_labels"))
    entity_ids = _string_list(args.get("entity_ids"))
    if source_item_ids:
        pieces.append(f"source_refs={len(source_item_ids)}")
    if entity_labels:
        pieces.append(f"实体：{'、'.join(entity_labels[:4])}")
    if entity_ids:
        pieces.append(f"entity_ids={len(entity_ids)}")
    if args.get("job_id"):
        pieces.append("限定 digest/job 上下文")
    return "；".join(pieces) if pieces else "正在读取当前租户可访问的知识证据。"


def _ask_tool_result_detail(counts: dict[str, Any]) -> str:
    if counts.get("error_count"):
        return str(counts.get("error") or "工具调用失败，未返回可用证据。")
    evidence_count = counts.get("evidence_count", 0)
    source_ref_count = counts.get("source_ref_count", 0)
    if evidence_count or source_ref_count:
        return f"返回 {evidence_count} 条证据，{source_ref_count} 条引用。"
    return "已返回工具结果，后续会进行引用校验。"


def _ask_tool_result_counts(event: dict[str, Any]) -> dict[str, Any]:
    content = event.get("content") or event.get("result") or event.get("output")
    error = _ask_tool_error_text(content)
    if error:
        return {
            "evidence_count": 0,
            "source_ref_count": 0,
            "error_count": 1,
            "error": error,
        }
    parsed = _ask_json_object_from_text(str(content or "")) if content is not None else None
    payload = parsed if isinstance(parsed, dict) else {}
    evidence = payload.get("workspace", {}).get("evidence") if isinstance(payload.get("workspace"), dict) else payload.get("evidence")
    if not isinstance(evidence, dict):
        evidence = payload
    results = _list_of_dicts(evidence.get("results"))
    citations = _list_of_dicts(evidence.get("citations")) or _list_of_dicts(payload.get("citations")) or _list_of_dicts(payload.get("source_refs"))
    return {
        "evidence_count": len(results) or len(_list_of_dicts(payload.get("results"))),
        "source_ref_count": len(citations),
        "error_count": 0,
    }


def _ask_tool_error_text(content: Any) -> str | None:
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("[MCP_ERROR]"):
        return text[:240]
    if text.startswith("[ERROR]"):
        return text[:240]
    if "ConnectionResetError" in text or "Connection lost" in text:
        return text[:240]
    parsed = _ask_json_object_from_text(text)
    if isinstance(parsed, dict):
        error = parsed.get("error") or parsed.get("detail")
        if error:
            return str(error)[:240]
    return None


def _ask_json_object_from_text(text: str) -> dict[str, Any] | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any, *, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _ask_with_quality_signals(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    evidence = enriched.get("evidence") if isinstance(enriched.get("evidence"), dict) else {}
    route = enriched.get("route") if isinstance(enriched.get("route"), dict) else {}
    if "source_windows" not in enriched:
        enriched["source_windows"] = _list_of_dicts(evidence.get("source_windows"))
    if "scope_applied" not in enriched:
        enriched["scope_applied"] = route.get("scope_applied") if isinstance(route.get("scope_applied"), dict) else {}
    if not _list_of_dicts(enriched.get("progress")):
        progress = [_ask_progress_from_step(step) for step in _list_of_dicts(enriched.get("agent_steps"))]
        quality = enriched.get("quality_signals") if isinstance(enriched.get("quality_signals"), dict) else {}
        progress.append(_ask_evidence_check_progress(quality))
        enriched["progress"] = progress
    enriched["quality_signals"] = _ask_quality_signals(enriched)
    if enriched.get("progress"):
        enriched["progress"] = [
            _ask_evidence_check_progress(enriched["quality_signals"]) if str(item.get("stage") or "") == "evidence_check" else item
            for item in _list_of_dicts(enriched.get("progress"))
        ]
    return enriched


def _ask_evidence_check_progress(quality_signals: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": "evidence_check",
        "phase": "evidence_check",
        "status": "warning" if quality_signals.get("quality_band") in {"no_answerable_evidence", "needs_citation_review", "failed"} else "complete",
        "title": "证据校验",
        "detail": "已检查引用、证据数量和可回答性。",
        "step_id": "evidence_check",
        "evidence_count": quality_signals.get("evidence_result_count"),
        "source_ref_count": quality_signals.get("citation_count"),
    }


def _agentic_pska_mcp_ok(agentic_check: Any) -> bool:
    if not isinstance(agentic_check, dict):
        return False
    if agentic_check.get("ok") is False:
        return False
    if agentic_check.get("missing_pska_tools"):
        return False
    if agentic_check.get("pska_tools_loaded") is False:
        return False
    ready = agentic_check.get("ready") if isinstance(agentic_check.get("ready"), dict) else {}
    mcp = ready.get("mcp") if isinstance(ready.get("mcp"), dict) else {}
    servers = _list_of_dicts(mcp.get("servers"))
    pska_servers = [
        server
        for server in servers
        if str(server.get("name") or "") == "pska"
        or any(str(tool).startswith("pska_") for tool in (server.get("tools") or []))
    ]
    if pska_servers:
        return all(server.get("alive") is not False for server in pska_servers)
    if mcp.get("ready") is False:
        return False
    return True


def _ask_trace_tool_errors(trace: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for event in _list_of_dicts(trace.get("events")):
        if str(event.get("type") or "").lower() != "tool_result":
            continue
        summary = event.get("result_summary") if isinstance(event.get("result_summary"), dict) else {}
        error = summary.get("error") if isinstance(summary, dict) else None
        if not error:
            error = _ask_tool_error_text(event.get("content") or event.get("result") or event.get("output"))
        if error:
            errors.append(str(error)[:240])
    return list(dict.fromkeys(errors))


def _ask_no_answer_diagnostics(
    *,
    payload: dict[str, Any],
    route: dict[str, Any],
    trace: dict[str, Any],
    citations: list[dict[str, Any]],
    results: list[dict[str, Any]],
    gaps: list[str],
    conflicts: list[str],
    denied_tool_calls: list[Any],
    dropped_source_refs: list[Any],
    answer_chars: int,
) -> dict[str, Any]:
    dimensions: list[dict[str, Any]] = []
    reasons: list[str] = []
    tool_errors = _ask_trace_tool_errors(trace)
    scope_applied = route.get("scope_applied") if isinstance(route.get("scope_applied"), dict) else {}
    knowledge_base_ids = _string_list(scope_applied.get("knowledge_base_ids"))
    scoped_source_item_ids = _string_list(scope_applied.get("source_item_ids"))
    knowledge_base_source_item_count = _int_value(
        scope_applied.get("knowledge_base_source_item_count"),
        fallback=len(_string_list(scope_applied.get("knowledge_base_source_item_ids"))),
    )
    source_item_count = _int_value(scope_applied.get("source_item_count"), fallback=len(scoped_source_item_ids))

    def add(dimension: str, status: str, detail: str) -> None:
        dimensions.append({"dimension": dimension, "status": status, "detail": detail})
        if status not in {"ok", "not_applicable"}:
            reasons.append(status)

    if knowledge_base_ids:
        if knowledge_base_source_item_count == 0:
            add(
                "knowledge_base_scope",
                "selected_knowledge_base_empty",
                f"{len(knowledge_base_ids)} selected knowledge base(s) have no active source items.",
            )
        elif source_item_count == 0:
            add(
                "knowledge_base_scope",
                "selected_scope_empty",
                "Selected knowledge base scope resolved to zero active source items after source filters.",
            )
        elif not citations and not results:
            add(
                "knowledge_base_scope",
                "selected_knowledge_base_no_relevant_chunks",
                f"Selected knowledge base scope resolved to {source_item_count} active source item(s), but no matching chunks were returned.",
            )
        else:
            add(
                "knowledge_base_scope",
                "ok",
                f"Selected knowledge base scope resolved to {source_item_count} active source item(s).",
            )

    if citations:
        add("evidence", "ok", f"{len(citations)} citations are available.")
    elif results:
        add("evidence", "missing_citations", "Retrieval returned candidate chunks, but none were promoted to final citations.")
    else:
        add("evidence", "no_visible_evidence", "No visible citations or retrieval results were returned for this tenant/user scope.")

    if results:
        add("retrieval", "ok", f"{len(results)} retrieval results returned.")
    else:
        add("retrieval", "no_relevant_chunks", "No relevant chunks were returned by the current index/search path.")

    if gaps:
        add("evidence_check", "insufficient_evidence", "; ".join(gaps[:3]))
    elif conflicts:
        add("evidence_check", "conflicts_detected", "; ".join(conflicts[:3]))
    elif citations:
        add("evidence_check", "ok", "Evidence check found citeable support.")
    else:
        add("evidence_check", "not_enough_signal", "There was not enough evidence signal to support a confident answer.")

    if route.get("fallback_from") or trace.get("fallback_reason"):
        add("fastreact", str(trace.get("fallback_reason") or "fallback"), "Deep Ask fell back to PSKA direct retrieval.")
    elif tool_errors and route.get("requires_agentic_service_online"):
        add("fastreact", "tool_channel_error", "FastReAct ran, but one or more PSKA tool calls failed.")
    elif route.get("requires_agentic_service_online"):
        add("fastreact", "ok", "FastReAct handled the deep Ask path.")
    else:
        add("fastreact", "not_applicable", "Quick Ask does not require FastReAct.")

    if denied_tool_calls:
        add("mcp", "tool_denied", f"{len(denied_tool_calls)} tool calls were denied by policy.")
    elif tool_errors:
        add("mcp", "tool_error", "; ".join(tool_errors[:2]))
    elif route.get("retrieval_owner") == "fastreact_pska_mcp":
        add("mcp", "ok", "FastReAct used the PSKA read-only MCP boundary.")
    else:
        add("mcp", "not_applicable", "PSKA direct retrieval did not use MCP.")

    if dropped_source_refs:
        add("permissions", "source_refs_not_visible", "Some source refs were dropped because they were outside the current tenant/user scope.")
    elif not citations and not results:
        add("permissions", "possibly_filtered_or_unindexed", "No visible evidence was available; the data may be unindexed, out of scope, or not ingested.")
    else:
        add("permissions", "ok", "Returned evidence is visible to the represented user.")

    if not answer_chars:
        add("answer", "empty_answer", "No answer text was produced.")
    elif not citations:
        add("answer", "uncited_answer", "Answer text exists but lacks final citations.")
    else:
        add("answer", "ok", "Answer includes citeable evidence.")

    primary = next((reason for reason in reasons if reason not in {"missing_citations", "uncited_answer"}), reasons[0] if reasons else "ok")
    return {
        "schema": "pska.ask_no_answer_diagnostics.v1",
        "primary_reason": primary,
        "reasons": list(dict.fromkeys(reasons)),
        "dimensions": dimensions,
        "display": bool(reasons) or not answer_chars or not citations,
        "query": payload.get("query"),
    }


def _ask_quality_signals(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
    evidence_check = payload.get("evidence_check") if isinstance(payload.get("evidence_check"), dict) else {}
    citations = _list_of_dicts(payload.get("citations")) or _list_of_dicts(evidence.get("citations"))
    source_refs = _list_of_dicts(payload.get("source_refs")) or _list_of_dicts(evidence.get("source_refs"))
    results = _list_of_dicts(evidence.get("results"))
    graph_paths = _list_of_dicts(evidence.get("graph_paths"))
    memory_context = _list_of_dicts(evidence.get("memory_context"))
    profile_context = _list_of_dicts(evidence.get("profile_context"))
    gaps = _ask_note_list(evidence.get("gaps"))
    conflicts = _ask_note_list(evidence.get("conflicts"))
    tool_calls = trace.get("tool_calls") if isinstance(trace.get("tool_calls"), list) else []
    denied_tool_calls = trace.get("denied_tool_calls") if isinstance(trace.get("denied_tool_calls"), list) else []
    dropped_source_refs = trace.get("dropped_source_refs") if isinstance(trace.get("dropped_source_refs"), list) else []
    answer = str(payload.get("answer") or "")
    answer_chars = len(answer)
    if evidence_check.get("status") == "not_applicable" or route.get("retrieval_owner") == "none":
        return {
            "schema": "pska.ask_quality_signals.v1",
            "quality_band": "direct_answer",
            "evidence_status": "not_applicable",
            "report_readiness": "not_ready",
            "flags": [],
            "query_chars": len(str(payload.get("query") or "")),
            "answer_chars": answer_chars,
            "citation_count": 0,
            "source_ref_count": 0,
            "evidence_result_count": 0,
            "graph_path_count": 0,
            "memory_context_count": 0,
            "profile_context_count": 0,
            "gap_count": 0,
            "conflict_count": 0,
            "tool_call_count": 0,
            "denied_tool_call_count": 0,
            "retrieval_owner": route.get("retrieval_owner"),
            "selected_intent": route.get("selected_intent") or route.get("intent"),
            "surface": route.get("surface"),
            "fallback_from": route.get("fallback_from"),
            "no_answer_diagnostics": {
                "schema": "pska.ask_no_answer_diagnostics.v1",
                "primary_reason": "not_applicable",
                "reasons": [],
                "dimensions": [],
                "display": False,
                "query": payload.get("query"),
            },
            "total_ms": timing.get("total_ms"),
            "time_to_first_answer_ms": timing.get("time_to_first_answer_ms"),
            "time_to_first_agent_event_ms": timing.get("time_to_first_agent_event_ms"),
        }
    flags: list[str] = []
    if not answer_chars:
        flags.append("empty_answer")
    if not citations and not results:
        flags.append("no_evidence")
    if not citations and results:
        flags.append("missing_citations")
    if any("insufficient" in gap or "缺" in gap for gap in gaps):
        flags.append("insufficient_evidence")
    if conflicts:
        flags.append("evidence_conflict")
    if route.get("fallback_from") or trace.get("fallback_reason"):
        flags.append("fallback")
    if dropped_source_refs:
        flags.append("dropped_source_refs")
    scope_applied = route.get("scope_applied") if isinstance(route.get("scope_applied"), dict) else {}
    knowledge_base_ids = _string_list(scope_applied.get("knowledge_base_ids"))
    knowledge_base_source_item_count = _int_value(
        scope_applied.get("knowledge_base_source_item_count"),
        fallback=len(_string_list(scope_applied.get("knowledge_base_source_item_ids"))),
    )
    source_item_count = _int_value(
        scope_applied.get("source_item_count"),
        fallback=len(_string_list(scope_applied.get("source_item_ids"))),
    )
    if knowledge_base_ids and knowledge_base_source_item_count == 0:
        flags.append("selected_knowledge_base_empty")
    elif knowledge_base_ids and source_item_count == 0:
        flags.append("selected_scope_empty")
    elif knowledge_base_ids and not citations and not results:
        flags.append("selected_knowledge_base_no_relevant_chunks")
    flags.extend(flag for flag in _ask_answer_quality_flags(answer) if flag not in flags)

    evidence_status = "grounded" if citations else "retrieved_without_citations" if results else "no_evidence"
    if "insufficient_evidence" in flags:
        evidence_status = "insufficient_evidence"
    if "empty_answer" in flags:
        quality_band = "failed"
    elif evidence_status in {"no_evidence", "insufficient_evidence"}:
        quality_band = "no_answerable_evidence"
    elif any(flag in flags for flag in ["evidence_conflict", "fallback", "dropped_source_refs", "raw_evidence_dump", "answer_needs_rewrite"]):
        quality_band = "needs_review"
    elif citations:
        quality_band = "grounded"
    else:
        quality_band = "needs_citation_review"
    no_answer_diagnostics = _ask_no_answer_diagnostics(
        payload=payload,
        route=route,
        trace=trace,
        citations=citations,
        results=results,
        gaps=gaps,
        conflicts=conflicts,
        denied_tool_calls=denied_tool_calls,
        dropped_source_refs=dropped_source_refs,
        answer_chars=answer_chars,
    )

    return {
        "schema": "pska.ask_quality_signals.v1",
        "quality_band": quality_band,
        "evidence_status": evidence_status,
        "report_readiness": _ask_report_readiness(quality_band),
        "flags": flags,
        "query_chars": len(str(payload.get("query") or "")),
        "answer_chars": answer_chars,
        "citation_count": len(citations),
        "source_ref_count": len(source_refs),
        "evidence_result_count": len(results),
        "graph_path_count": len(graph_paths),
        "memory_context_count": len(memory_context),
        "profile_context_count": len(profile_context),
        "gap_count": len(gaps),
        "conflict_count": len(conflicts),
        "tool_call_count": len(tool_calls),
        "denied_tool_call_count": len(denied_tool_calls),
        "retrieval_owner": route.get("retrieval_owner"),
        "selected_intent": route.get("selected_intent") or route.get("intent"),
        "surface": route.get("surface"),
        "fallback_from": route.get("fallback_from"),
        "no_answer_diagnostics": no_answer_diagnostics,
        "total_ms": timing.get("total_ms"),
        "time_to_first_answer_ms": timing.get("time_to_first_answer_ms"),
        "time_to_first_agent_event_ms": timing.get("time_to_first_agent_event_ms"),
    }


def _ask_report_readiness(quality_band: str) -> str:
    if quality_band == "grounded":
        return "ready_with_citations"
    if quality_band == "needs_review":
        return "needs_human_review"
    if quality_band == "needs_citation_review":
        return "needs_citation_review"
    if quality_band == "failed":
        return "failed"
    return "not_ready"


def _ask_answer_quality_flags(answer: str) -> list[str]:
    text = str(answer or "")
    if not text.strip():
        return []
    flags: list[str] = []
    if re.search(r"(^|\n)\s*---\s*(\n|$)", text) or re.search(
        r"(^|\n)\s*(title|type|slug|aliases|attendees|date)\s*:",
        text,
        flags=re.IGNORECASE,
    ):
        flags.append("raw_evidence_dump")
    if text.count("|") >= 6 or re.search(r"(^|\n)\s*\|?\s*-{3,}\s*\|", text):
        flags.append("raw_evidence_dump")
    if len(text) > 1400 and ("可引用来源" in text or "PSKA 找到了" in text):
        flags.append("answer_needs_rewrite")
    if "raw_evidence_dump" in flags and "answer_needs_rewrite" not in flags:
        flags.append("answer_needs_rewrite")
    return flags


def _ask_note_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    notes: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(item.get("reason") or item.get("message") or item.get("detail") or item)
        else:
            text = str(item)
        text = text.strip().lower()
        if text:
            notes.append(text)
    return notes


def _ask_source_ref_dicts(value: Any, *, string_field: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            ref = {
                key: item.get(key)
                for key in [
                    "source_item_id",
                    "document_id",
                    "chunk_id",
                    "passage_window_id",
                    "message_id",
                    "path",
                    "url",
                    "title",
                    "snippet",
                    "score",
                ]
                if item.get(key)
            }
            if ref:
                refs.append(ref)
            continue
        if isinstance(item, str) and item.strip():
            refs.append({string_field: item.strip()})
    return refs


def _ask_source_refs_as_evidence(
    refs: list[dict[str, Any]],
    *,
    store: Any,
    query: str,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    if not refs:
        return {"results": [], "citations": [], "source_windows": []}
    retrieval = {
        "results": [dict(ref) for ref in refs if ref.get("source_item_id")],
        "citations": [dict(ref) for ref in refs if ref.get("source_item_id")],
        "diagnostics": {},
    }
    retrieval = _ask_hydrate_retrieval_source_windows(
        store,
        retrieval,
        query=query,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )
    return _ask_evidence_from_retrieval(retrieval)


def _ask_merge_hydrated_source_refs(refs: list[dict[str, Any]], hydrated_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not refs:
        return []
    hydrated_by_source: dict[str, dict[str, Any]] = {}
    for ref in hydrated_refs:
        source_item_id = str(ref.get("source_item_id") or "")
        if source_item_id and source_item_id not in hydrated_by_source:
            hydrated_by_source[source_item_id] = ref
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        source_item_id = str(ref.get("source_item_id") or "")
        if not source_item_id or source_item_id in seen:
            continue
        seen.add(source_item_id)
        hydrated = hydrated_by_source.get(source_item_id) or {}
        merged.append({**ref, **hydrated})
    return merged


def _ask_filter_evidence_to_refs(evidence: dict[str, Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
    source_ids = {str(ref.get("source_item_id")) for ref in refs if ref.get("source_item_id")}
    if not source_ids:
        return evidence
    filtered = dict(evidence)
    results = [
        result
        for result in _list_of_dicts(evidence.get("results"))
        if str(result.get("source_item_id") or (result.get("citation") if isinstance(result.get("citation"), dict) else {}).get("source_item_id") or "")
        in source_ids
    ]
    if results:
        filtered["results"] = results
    graph_paths = [
        path
        for path in _list_of_dicts(evidence.get("graph_paths"))
        if _ask_graph_path_has_source_ref(path, source_ids)
    ]
    if graph_paths:
        filtered["graph_paths"] = graph_paths
    return filtered


def _ask_graph_path_has_source_ref(path: dict[str, Any], source_ids: set[str]) -> bool:
    for edge in _list_of_dicts(path.get("edges")):
        for ref in _list_of_dicts(edge.get("source_refs")):
            if str(ref.get("source_item_id") or "") in source_ids:
                return True
    return False


def _ask_validate_source_refs(
    refs: list[dict[str, Any]],
    *,
    store: Any,
    tenant_id: str,
    owner_user_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not refs:
        return [], []
    allowed_items = {
        item.source_item_id: item
        for item in store.list_source_items(tenant_id=tenant_id)
        if getattr(item, "owner_user_id", "") == owner_user_id
    }
    items_by_title = {
        str(getattr(item, "title", "") or "").strip().lower(): item
        for item in allowed_items.values()
        if str(getattr(item, "title", "") or "").strip()
    }
    chunks_by_id = {}
    first_chunk_by_source: dict[str, Any] = {}
    if allowed_items:
        for chunk in store.list_chunks_for_sources(set(allowed_items)):
            chunks_by_id[getattr(chunk, "chunk_id", "")] = chunk
            first_chunk_by_source.setdefault(getattr(chunk, "source_item_id", ""), chunk)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        source_item_id = str(ref.get("source_item_id") or "").strip()
        if not source_item_id and ref.get("title"):
            item_by_title = items_by_title.get(str(ref.get("title") or "").strip().lower())
            if item_by_title:
                source_item_id = item_by_title.source_item_id
        if not source_item_id:
            dropped.append({"reason": "missing_source_item_id"})
            continue
        item = allowed_items.get(source_item_id)
        if item is None:
            dropped.append({"source_item_id": source_item_id, "reason": "tenant_or_owner_mismatch"})
            continue
        chunk_id = str(ref.get("chunk_id") or "").strip()
        chunk = chunks_by_id.get(chunk_id) or first_chunk_by_source.get(source_item_id)
        hydrated = {
            **ref,
            "source_item_id": source_item_id,
            "title": ref.get("title") or getattr(item, "title", None),
            "url": ref.get("url") or getattr(item, "url", None),
        }
        if not hydrated.get("snippet"):
            snippet_source = getattr(chunk, "text", None) if chunk else getattr(item, "content_text", "")
            hydrated["snippet"] = _ask_clean_evidence_text(str(snippet_source or ""))[:260]
        key = "|".join(str(hydrated.get(name) or "") for name in ["source_item_id", "chunk_id", "title", "url", "snippet"])
        if key in seen:
            continue
        seen.add(key)
        kept.append({key: value for key, value in hydrated.items() if value is not None})
    return kept, dropped


def _ask_public_trace(trace: dict[str, Any]) -> dict[str, Any]:
    public = dict(trace)
    events = trace.get("events")
    if isinstance(events, list):
        public["events"] = [_ask_public_trace_event(event) for event in _list_of_dicts(events)]
    return public


def _ask_public_trace_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or event.get("event_type") or "").strip().lower()
    public = {
        key: event.get(key)
        for key in ["schema", "type", "event_id", "sequence", "parent_event_id", "run_id", "tool_name", "tool_call_id", "duration_ms"]
        if event.get(key) is not None
    }
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    safe_metadata = {
        key: metadata.get(key)
        for key in [
            "action",
            "duration_ms",
            "has_tool_calls",
            "llm_usage",
            "model",
            "decision_level",
            "approved",
            "tool_policy_scope_applied",
            "tool_policy_denied",
            "denial_reason",
        ]
        if metadata.get(key) is not None
    }
    tool_policy = metadata.get("tool_policy") if isinstance(metadata.get("tool_policy"), dict) else {}
    if tool_policy:
        safe_policy = {
            key: tool_policy.get(key)
            for key in ["mode", "allowed_tools"]
            if tool_policy.get(key) is not None
        }
        policy_scope = tool_policy.get("scope") if isinstance(tool_policy.get("scope"), dict) else {}
        if policy_scope:
            safe_policy["scope"] = {
                key: policy_scope.get(key)
                for key in ["mode", "scope_mode", "knowledge_base_ids", "source_item_ids"]
                if policy_scope.get(key) is not None
            }
        if safe_policy:
            safe_metadata["tool_policy"] = safe_policy
    if safe_metadata:
        public["metadata"] = safe_metadata
    if event_type == "tool_call":
        args = event.get("tool_args") if isinstance(event.get("tool_args"), dict) else {}
        public["tool_args"] = {
            key: args.get(key)
            for key in [
                "query",
                "top_k",
                "max_results",
                "knowledge_base_ids",
                "scope_mode",
                "source_item_ids",
                "document_ids",
                "chunk_ids",
                "entity_ids",
                "entity_labels",
                "job_id",
            ]
            if args.get(key) is not None
        }
        scope = args.get("scope") if isinstance(args.get("scope"), dict) else {}
        if scope:
            public["tool_args"]["scope"] = {
                key: scope.get(key)
                for key in ["mode", "scope_mode", "knowledge_base_ids", "source_item_ids"]
                if scope.get(key) is not None
            }
    elif event_type == "tool_result":
        public["result_summary"] = _ask_tool_result_counts(event)
    elif event_type == "think":
        public["content"] = _ask_think_step_detail(event)
    elif event_type == "session_end":
        public["content"] = "Final answer returned."
    elif event_type == "error":
        public["content"] = "Agentic analysis failed."
    return public


def _ask_sse_events(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
    events: list[tuple[str, dict[str, Any]]] = [
        ("route", {"route": payload.get("route") or {}, "timing": timing}),
    ]
    for step in _list_of_dicts(payload.get("agent_steps")):
        events.append(("progress", {"progress": _ask_progress_from_step(step), "timing": timing}))
        events.append(("agent_step", {"step": step, "timing": timing}))
    quality_signals = payload.get("quality_signals") if isinstance(payload.get("quality_signals"), dict) else {}
    events.append(
        (
            "progress",
            {
                "progress": _ask_evidence_check_progress(quality_signals),
                "timing": timing,
            },
        )
    )
    events.extend(
        [
            (
                "evidence",
                {
                    "evidence": payload.get("evidence") or {},
                    "citations": payload.get("citations") or [],
                    "citation_audit": payload.get("citation_audit") or {},
                    "evidence_check": payload.get("evidence_check") or {},
                    "quality_signals": payload.get("quality_signals") or {},
                },
            ),
            (
                "answer_delta",
                {
                    "delta": str(payload.get("answer") or ""),
                    "answer_type": payload.get("answer_type"),
                    "time_to_first_answer_ms": timing.get("time_to_first_answer_ms"),
                },
            ),
            ("trace", {"trace": payload.get("trace") or {}, "agentic_service": payload.get("agentic_service") or {}}),
            (
                "done",
                {
                    "ok": payload.get("ok") is not False,
                    "answer": str(payload.get("answer") or ""),
                    "citations": payload.get("citations") or [],
                    "source_refs": payload.get("source_refs") or [],
                    "evidence": payload.get("evidence") or {},
                    "citation_audit": payload.get("citation_audit") or {},
                    "intent": payload.get("intent") or (payload.get("route") or {}).get("intent"),
                    "rewrite_query": payload.get("rewrite_query"),
                    "scope_applied": (payload.get("route") or {}).get("scope_applied") if isinstance(payload.get("route"), dict) else {},
                    "answer_type": payload.get("answer_type"),
                    "evidence_check": payload.get("evidence_check") or {},
                    "evidence_claims": payload.get("evidence_claims") or [],
                    "no_answer_reasons": payload.get("no_answer_reasons") or [],
                    "source_windows": payload.get("source_windows") or _list_of_dicts((payload.get("evidence") or {}).get("source_windows")),
                    "agent_steps": payload.get("agent_steps") or [],
                    "progress": payload.get("progress") or [],
                    "trace": payload.get("trace") or {},
                    "timing": timing,
                    "quality_signals": payload.get("quality_signals") or {},
                },
            ),
        ]
    )
    return events


def _ask_scope_applied_from_payload(store: Any, payload: dict[str, Any], *, tenant_id: str, owner_user_id: str) -> dict[str, Any]:
    raw_scope = _scope_from_payload(payload)
    if not raw_scope:
        return {}
    scope = _resolve_knowledge_base_scope(store, raw_scope, tenant_id=tenant_id, owner_user_id=owner_user_id)
    scope = _ask_scope_for_intent(scope, ask_intent="kb_search")
    return _ask_scope_applied(scope, ask_intent="kb_search")


def _ask_scope_applied_from_result(result: dict[str, Any]) -> dict[str, Any]:
    route = result.get("route") if isinstance(result.get("route"), dict) else {}
    scope_applied = route.get("scope_applied") if isinstance(route.get("scope_applied"), dict) else {}
    if not scope_applied and isinstance(result.get("scope_applied"), dict):
        scope_applied = result["scope_applied"]
    return dict(scope_applied or {})


def _ask_scope_applied_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    ask_scope = metadata.get("ask_scope") if isinstance(metadata.get("ask_scope"), dict) else {}
    if not ask_scope and isinstance(metadata.get("knowledge_base_scope"), dict):
        ask_scope = metadata["knowledge_base_scope"]
    return dict(ask_scope or {})


def _ask_conversation_metadata_with_scope(metadata: dict[str, Any], scope_applied: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata or {})
    if not scope_applied:
        return updated
    scope = dict(scope_applied)
    updated["ask_scope"] = scope
    updated["knowledge_base_scope"] = scope
    updated["knowledge_base_ids"] = _string_list(scope.get("knowledge_base_ids"))
    updated["scope_mode"] = str(scope.get("mode") or "soft")
    return updated


def _ask_conversation_payload(conversation: Any) -> dict[str, Any]:
    metadata = dict(getattr(conversation, "metadata", {}) or {})
    scope_applied = _ask_scope_applied_from_metadata(metadata)
    return {
        "conversation_id": getattr(conversation, "conversation_id", ""),
        "tenant_id": getattr(conversation, "tenant_id", DEFAULT_TENANT_ID),
        "owner_user_id": getattr(conversation, "owner_user_id", ""),
        "title": getattr(conversation, "title", ""),
        "status": getattr(conversation, "status", "active"),
        "summary": getattr(conversation, "summary", ""),
        "metadata": metadata,
        "scope_applied": scope_applied,
        "knowledge_base_ids": _string_list(scope_applied.get("knowledge_base_ids")),
        "created_at": getattr(conversation, "created_at", None),
        "updated_at": getattr(conversation, "updated_at", None),
    }


def _ask_message_payload(message: Any) -> dict[str, Any]:
    metadata = dict(getattr(message, "metadata", {}) or {})
    scope_applied = _ask_scope_applied_from_metadata(metadata)
    return {
        "message_id": getattr(message, "message_id", ""),
        "conversation_id": getattr(message, "conversation_id", ""),
        "tenant_id": getattr(message, "tenant_id", DEFAULT_TENANT_ID),
        "owner_user_id": getattr(message, "owner_user_id", ""),
        "role": getattr(message, "role", ""),
        "content": getattr(message, "content", ""),
        "run_id": getattr(message, "run_id", None),
        "citations": list(getattr(message, "citations", []) or []),
        "source_refs": list(getattr(message, "source_refs", []) or []),
        "metadata": metadata,
        "scope_applied": scope_applied,
        "knowledge_base_ids": _string_list(scope_applied.get("knowledge_base_ids")),
        "created_at": getattr(message, "created_at", None),
    }


def _ask_run_payload(run: Any) -> dict[str, Any]:
    result = dict(getattr(run, "result", {}) or {})
    route = dict(getattr(run, "route", {}) or {})
    scope_applied = _ask_scope_applied_from_result({"result": result, "route": route, **result})
    return {
        "run_id": getattr(run, "run_id", ""),
        "conversation_id": getattr(run, "conversation_id", ""),
        "tenant_id": getattr(run, "tenant_id", DEFAULT_TENANT_ID),
        "owner_user_id": getattr(run, "owner_user_id", ""),
        "query": getattr(run, "query", ""),
        "status": getattr(run, "status", ""),
        "result": result,
        "route": route,
        "scope_applied": scope_applied,
        "knowledge_base_ids": _string_list(scope_applied.get("knowledge_base_ids")),
        "evidence_check": dict(getattr(run, "evidence_check", {}) or {}),
        "prompt_profile_id": getattr(run, "prompt_profile_id", None),
        "prompt_profile_version": getattr(run, "prompt_profile_version", None),
        "started_at": getattr(run, "started_at", None),
        "finished_at": getattr(run, "finished_at", None),
    }


def _safe_update_ask_run_progress(store: Any, run_id: str, *, result: dict[str, Any]) -> None:
    try:
        store.update_ask_run_progress(run_id, result=result)
    except Exception:
        pass


def _add_ask_failure_message(
    store: Any,
    *,
    conversation: Any,
    run: Any,
    owner_user_id: str,
    tenant_id: str,
    result: dict[str, Any],
    prompt_lineage: dict[str, Any],
) -> None:
    answer = str(result.get("answer") or "").strip()
    error = str(result.get("error") or "Ask PSKA stream failed")
    store.add_ask_message(
        AskMessage(
            message_id=f"askmsg_{uuid4().hex}",
            conversation_id=getattr(conversation, "conversation_id", ""),
            owner_user_id=owner_user_id,
            role="assistant",
            content=answer or f"Ask PSKA 运行未完成：{error}",
            run_id=getattr(run, "run_id", None),
            citations=_list_of_dicts(result.get("citations")),
            source_refs=_list_of_dicts(result.get("source_refs")),
            metadata={
                "quality_signals": result.get("quality_signals") or {},
                "prompt_profile": prompt_lineage,
                "status": "failed",
                "error": error,
            },
            tenant_id=tenant_id,
        )
    )


def _ask_message_scope(message: Any) -> dict[str, Any]:
    content = str(getattr(message, "content", "") or "")
    return {
        "role": getattr(message, "role", ""),
        "content": _trim_words(content, 80),
        "message_id": getattr(message, "message_id", ""),
        "run_id": getattr(message, "run_id", None),
    }


def _empty_ask_stream_result(*, query: str, conversation_id: str, run_id: str, prompt_lineage: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "status": "running",
        "answer": "",
        "citations": [],
        "source_refs": [],
        "citation_audit": {"used": [], "dropped": []},
        "evidence_check": {},
        "answer_type": None,
        "no_answer_reasons": [],
        "progress": [],
        "agent_steps": [],
        "timing": {},
        "evidence": {},
        "trace": {},
        "quality_signals": {},
        "conversation_id": conversation_id,
        "run_id": run_id,
        **prompt_lineage,
    }


def _accumulate_ask_stream_result(result: dict[str, Any], event_name: str, event_payload: dict[str, Any]) -> None:
    if event_name == "route":
        result["route"] = event_payload.get("route") or {}
        result["timing"] = event_payload.get("timing") or result.get("timing") or {}
    elif event_name == "progress":
        result.setdefault("progress", []).append(event_payload.get("progress") or {})
        if event_payload.get("timing"):
            result["timing"] = event_payload["timing"]
    elif event_name == "agent_step":
        result.setdefault("agent_steps", []).append(event_payload.get("step") or {})
    elif event_name == "evidence":
        result["evidence"] = event_payload.get("evidence") or {}
        result["citations"] = _list_of_dicts(event_payload.get("citations"))
        result["source_refs"] = _list_of_dicts((event_payload.get("evidence") or {}).get("source_refs")) or result["citations"]
        result["citation_audit"] = event_payload.get("citation_audit") or {}
        result["evidence_check"] = event_payload.get("evidence_check") or {}
        result["quality_signals"] = event_payload.get("quality_signals") or {}
    elif event_name == "answer_delta":
        result["answer"] = str(result.get("answer") or "") + str(event_payload.get("delta") or "")
        if event_payload.get("answer_type"):
            result["answer_type"] = event_payload["answer_type"]
    elif event_name == "trace":
        result["trace"] = event_payload.get("trace") or {}
        result["agentic_service"] = event_payload.get("agentic_service") or {}
    elif event_name == "error":
        result["ok"] = False
        result["error"] = str(event_payload.get("error") or "Ask PSKA stream failed")
        result["status"] = "failed"
    elif event_name == "done":
        result["ok"] = event_payload.get("ok") is not False
        for key in (
            "intent",
            "rewrite_query",
            "scope_applied",
            "answer_type",
            "evidence_check",
            "evidence_claims",
            "no_answer_reasons",
            "source_windows",
            "trace",
            "error",
            "status",
        ):
            if event_payload.get(key) is not None:
                result[key] = event_payload[key]
        for key in ("agent_steps", "progress"):
            if event_payload.get(key):
                result[key] = event_payload[key]
        if event_payload.get("citations") is not None:
            result["citations"] = _list_of_dicts(event_payload.get("citations"))
        if event_payload.get("source_refs") is not None:
            result["source_refs"] = _list_of_dicts(event_payload.get("source_refs"))
        if event_payload.get("citation_audit") is not None:
            result["citation_audit"] = event_payload.get("citation_audit") or {}
        if event_payload.get("evidence") is not None:
            result["evidence"] = event_payload.get("evidence") or {}
        if event_payload.get("timing"):
            result["timing"] = event_payload["timing"]
        if event_payload.get("quality_signals"):
            result["quality_signals"] = event_payload["quality_signals"]


def _workspace_owner_user_id(context: RequestContext | None, requested_owner_user_id: str | None) -> str:
    if context is None:
        return requested_owner_user_id or "user_primary"
    if context.caller == "agent_service":
        return context.represented_user_id or "agent_service"
    return requested_owner_user_id or context.represented_user_id or context.user_id


def _writing_request_scope(
    context: RequestContext | None,
    payload: dict[str, Any] | None = None,
    *,
    requested_owner_user_id: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    payload = dict(payload or {})
    if context:
        return context.apply_to_payload(payload), context.tenant_id, _workspace_owner_user_id(context, None)
    owner = requested_owner_user_id or payload.get("owner_user_id") or payload.get("represented_user_id") or payload.get("user_id")
    return payload, str(payload.get("tenant_id") or DEFAULT_TENANT_ID), str(owner or "user_primary")


def _tenant_id_for_request(context: RequestContext | None, requested_tenant_id: str | None = None) -> str:
    return requested_tenant_id or (context.tenant_id if context else None) or DEFAULT_TENANT_ID


def _prompt_profile_id(*, tenant_id: str, scope: str, owner_user_id: str | None, profile_type: str) -> str:
    basis = f"{tenant_id}:{scope}:{owner_user_id or 'tenant'}:{profile_type}"
    return f"pp_{uuid5(NAMESPACE_URL, basis).hex}"


def _prompt_profile_default_name(profile_type: str, scope: str) -> str:
    return f"{scope} {profile_type} profile"


def _default_prompt_profiles_payload(*, tenant_id: str) -> dict[str, dict[str, Any]]:
    return {
        profile_type: {
            "prompt_profile_id": _prompt_profile_id(tenant_id=tenant_id, scope="system", owner_user_id=None, profile_type=profile_type),
            "profile_type": profile_type,
            "scope": "system",
            "name": _prompt_profile_default_name(profile_type, "system"),
            "current_version": 1,
            "config": dict(config),
        }
        for profile_type, config in DEFAULT_PROMPT_PROFILE_CONFIGS.items()
    }


def _effective_prompt_profiles(store: Any, *, tenant_id: str, owner_user_id: str) -> dict[str, Any]:
    profiles = store.list_prompt_profiles(tenant_id=tenant_id, owner_user_id=owner_user_id)
    by_type: dict[str, dict[str, Any]] = _default_prompt_profiles_payload(tenant_id=tenant_id)
    precedence = {"system": 0, "tenant": 1, "user": 2}
    selected_precedence = {profile_type: 0 for profile_type in PROMPT_PROFILE_TYPES}
    for profile in profiles:
        profile_type = str(getattr(profile, "profile_type", "") or "")
        scope = str(getattr(profile, "scope", "") or "")
        if profile_type not in PROMPT_PROFILE_TYPES:
            continue
        rank = precedence.get(scope, -1)
        if rank < selected_precedence.get(profile_type, 0):
            continue
        current = dict(by_type.get(profile_type) or {})
        merged_config = {**dict(current.get("config") or {}), **dict(getattr(profile, "config", {}) or {})}
        current.update(_prompt_profile_payload(profile))
        current["config"] = merged_config
        by_type[profile_type] = current
        selected_precedence[profile_type] = rank
    return by_type


def _prompt_profile_payload(profile: Any) -> dict[str, Any]:
    return {
        "prompt_profile_id": getattr(profile, "prompt_profile_id", None),
        "profile_type": getattr(profile, "profile_type", None),
        "scope": getattr(profile, "scope", None),
        "name": getattr(profile, "name", None),
        "owner_user_id": getattr(profile, "owner_user_id", None),
        "status": getattr(profile, "status", None),
        "current_version": getattr(profile, "current_version", None),
        "config": dict(getattr(profile, "config", {}) or {}),
        "created_at": getattr(profile, "created_at", None),
        "updated_at": getattr(profile, "updated_at", None),
        "tenant_id": getattr(profile, "tenant_id", None),
    }


def _scope_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    scope = dict(payload.get("scope") or {}) if isinstance(payload.get("scope"), dict) else {}
    knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
    if knowledge_base_ids:
        scope["knowledge_base_ids"] = knowledge_base_ids
    source_item_ids = _string_list(payload.get("source_item_ids"))
    if source_item_ids:
        scope["source_item_ids"] = sorted(set([*_string_list(scope.get("source_item_ids")), *source_item_ids]))
    return scope


def _knowledge_base_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    ids.extend(_string_list(payload.get("knowledge_base_id")))
    ids.extend(_string_list(payload.get("knowledge_base_ids")))
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    ids.extend(_string_list(scope.get("knowledge_base_id")))
    ids.extend(_string_list(scope.get("knowledge_base_ids")))
    return list(dict.fromkeys(item for item in ids if item))


def _knowledge_base_ids_from_scope(scope: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    ids.extend(_string_list(scope.get("knowledge_base_id")))
    ids.extend(_string_list(scope.get("knowledge_base_ids")))
    return list(dict.fromkeys(item for item in ids if item))


def _knowledge_bases_for_payload(
    store: Any,
    payload: dict[str, Any],
    *,
    tenant_id: str,
    owner_user_id: str,
    actor_user_id: str,
    default_space_id: str | None = None,
) -> list[Any]:
    knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
    if knowledge_base_ids:
        return [
            _get_accessible_knowledge_base(store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            for knowledge_base_id in knowledge_base_ids
        ]
    return [
        store.ensure_default_knowledge_base(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_by_user_id=actor_user_id,
            default_space_id=default_space_id,
        )
    ]


def _knowledge_bases_for_source_or_payload(
    store: Any,
    payload: dict[str, Any],
    source: Any,
    *,
    actor_user_id: str,
) -> list[Any]:
    knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
    if not knowledge_base_ids:
        knowledge_base_ids = sorted(
            store.list_knowledge_base_ids_for_source(
                source.knowledge_source_id,
                tenant_id=source.tenant_id,
                owner_user_id=source.owner_user_id,
            )
        )
    if knowledge_base_ids:
        return [
            _get_accessible_knowledge_base(
                store,
                knowledge_base_id,
                tenant_id=source.tenant_id,
                owner_user_id=source.owner_user_id,
            )
            for knowledge_base_id in knowledge_base_ids
        ]
    return [
        store.ensure_default_knowledge_base(
            tenant_id=source.tenant_id,
            owner_user_id=source.owner_user_id,
            created_by_user_id=actor_user_id,
            default_space_id=None,
        )
    ]


def _bind_source_to_knowledge_bases(
    store: Any,
    source: Any,
    *,
    knowledge_bases: list[Any],
    source_item_ids: list[str],
    actor_user_id: str,
    membership_type: str,
) -> None:
    for knowledge_base in knowledge_bases:
        store.add_knowledge_base_source(
            KnowledgeBaseSource(
                knowledge_base_id=knowledge_base.knowledge_base_id,
                knowledge_source_id=source.knowledge_source_id,
                tenant_id=source.tenant_id,
                owner_user_id=source.owner_user_id,
                added_by_user_id=actor_user_id,
                metadata={"bound_by": "workspace_api", "membership_type": membership_type},
            )
        )
        for source_item_id in source_item_ids:
            store.add_knowledge_base_source_item(
                KnowledgeBaseSourceItem(
                    knowledge_base_id=knowledge_base.knowledge_base_id,
                    source_item_id=source_item_id,
                    tenant_id=source.tenant_id,
                    owner_user_id=source.owner_user_id,
                    added_by_user_id=actor_user_id,
                    membership_type=membership_type,
                    metadata={"bound_by": "workspace_api", "knowledge_source_id": source.knowledge_source_id},
                )
            )


def _resolve_knowledge_base_scope(store: Any, scope: dict[str, Any], *, tenant_id: str, owner_user_id: str) -> dict[str, Any]:
    resolved = dict(scope or {})
    knowledge_base_ids = _knowledge_base_ids_from_scope(resolved)
    if not knowledge_base_ids:
        return resolved
    for knowledge_base_id in knowledge_base_ids:
        _get_accessible_knowledge_base(store, knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
    scoped_source_item_ids = store.list_knowledge_base_source_item_ids(
        set(knowledge_base_ids),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )
    has_explicit_source_item_ids = "source_item_ids" in resolved
    requested_source_item_ids = set(_string_list(resolved.get("source_item_ids")))
    source_item_ids = requested_source_item_ids & scoped_source_item_ids if has_explicit_source_item_ids else set(scoped_source_item_ids)
    dropped_source_item_ids = sorted(requested_source_item_ids - source_item_ids) if has_explicit_source_item_ids else []
    resolved["knowledge_base_ids"] = knowledge_base_ids
    resolved["knowledge_base_source_item_ids"] = sorted(scoped_source_item_ids)
    resolved["source_item_ids"] = sorted(source_item_ids)
    resolved["dropped_source_item_ids"] = dropped_source_item_ids
    resolved["dropped_scope_ids"] = dropped_source_item_ids
    if not str(resolved.get("mode") or resolved.get("scope_mode") or "").strip():
        resolved["mode"] = "hard"
    return resolved


def _get_accessible_knowledge_base(store: Any, knowledge_base_id: str, *, tenant_id: str, owner_user_id: str) -> Any:
    try:
        return store.get_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
    except KeyError as exc:
        raise PermissionError("knowledge base is not accessible") from exc


def _knowledge_base_scope_for_ids(store: Any, knowledge_base_ids: list[str], *, tenant_id: str, owner_user_id: str) -> dict[str, Any]:
    ids = list(dict.fromkeys(item for item in _string_list(knowledge_base_ids) if item))
    if not ids:
        return {}
    return _resolve_knowledge_base_scope(
        store,
        {"knowledge_base_ids": ids, "mode": "hard"},
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )


def _knowledge_scope_applied(scope: dict[str, Any]) -> dict[str, Any]:
    source_item_ids = _string_list(scope.get("source_item_ids"))
    knowledge_base_source_item_ids = _string_list(scope.get("knowledge_base_source_item_ids"))
    return {
        "mode": str(scope.get("mode") or scope.get("scope_mode") or ("hard" if _knowledge_base_ids_from_scope(scope) else "all")),
        "knowledge_base_ids": _knowledge_base_ids_from_scope(scope),
        "knowledge_base_source_item_count": len(knowledge_base_source_item_ids),
        "source_item_count": len(source_item_ids),
        "dropped_scope_ids": _string_list(scope.get("dropped_scope_ids")),
        "dropped_source_item_ids": _string_list(scope.get("dropped_source_item_ids")),
    }


def _writing_board_metadata_with_knowledge_scope(
    store: Any,
    payload: dict[str, Any],
    *,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
    metadata_scope = metadata.get("knowledge_base_scope") if isinstance(metadata.get("knowledge_base_scope"), dict) else {}
    knowledge_base_ids = _knowledge_base_ids_from_payload(payload)
    if not knowledge_base_ids:
        knowledge_base_ids = _string_list(metadata.get("knowledge_base_ids"))
    if not knowledge_base_ids:
        knowledge_base_ids = _knowledge_base_ids_from_scope(metadata_scope)
    if knowledge_base_ids:
        scope = _resolve_knowledge_base_scope(
            store,
            {"knowledge_base_ids": knowledge_base_ids, "mode": str(metadata_scope.get("mode") or metadata.get("knowledge_base_scope_mode") or "hard")},
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
        )
        metadata["knowledge_base_ids"] = _knowledge_base_ids_from_scope(scope)
        metadata["knowledge_base_scope"] = _knowledge_scope_applied(scope)
    elif metadata_scope:
        metadata["knowledge_base_scope"] = {
            "mode": str(metadata_scope.get("mode") or "all"),
            "knowledge_base_ids": [],
            "source_item_count": 0,
        }
    return metadata


def _source_refs_source_item_ids(source_refs: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for ref in source_refs:
        source_item_id = str(ref.get("source_item_id") or "").strip()
        if source_item_id:
            ids.add(source_item_id)
    return ids


def _source_refs_match_source_item_ids(source_refs: list[dict[str, Any]], source_item_ids: set[str]) -> bool:
    return bool(source_item_ids and (_source_refs_source_item_ids(source_refs) & source_item_ids))


def _review_item_matches_source_item_ids(item: Any, source_item_ids: set[str]) -> bool:
    proposal = getattr(item, "proposal", {}) or {}
    source_refs = _console_review_source_refs(proposal if isinstance(proposal, dict) else {})
    return _source_refs_match_source_item_ids(source_refs, source_item_ids)


def _enrich_review_items_knowledge_bases(
    store: Any,
    items: list[dict[str, Any]],
    *,
    tenant_id: str,
    owner_user_id: str,
    selected_knowledge_base_ids: list[str],
) -> list[dict[str, Any]]:
    selected_ids = set(selected_knowledge_base_ids)
    lineage_cache: dict[str, dict[str, Any]] = {}

    def lineage_for_source(source_item_id: str) -> dict[str, Any]:
        if not source_item_id:
            return {}
        if source_item_id in lineage_cache:
            return lineage_cache[source_item_id]
        all_ids = sorted(
            store.list_knowledge_base_ids_for_source_item(
                source_item_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
        )
        scoped_ids = [knowledge_base_id for knowledge_base_id in all_ids if not selected_ids or knowledge_base_id in selected_ids]
        knowledge_base_ids = scoped_ids or all_ids
        knowledge_base_names: list[str] = []
        for knowledge_base_id in knowledge_base_ids:
            try:
                knowledge_base = store.get_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
            except KeyError:
                continue
            name = str(getattr(knowledge_base, "name", "") or getattr(knowledge_base, "slug", "") or knowledge_base_id).strip()
            if name:
                knowledge_base_names.append(name)
        lineage: dict[str, Any] = {
            "knowledge_base_ids": knowledge_base_ids,
            "knowledge_base_names": knowledge_base_names,
        }
        if len(knowledge_base_ids) == 1:
            lineage["knowledge_base_id"] = knowledge_base_ids[0]
        if len(knowledge_base_names) == 1:
            lineage["knowledge_base_name"] = knowledge_base_names[0]
        lineage_cache[source_item_id] = lineage
        return lineage

    enriched_items: list[dict[str, Any]] = []
    for item in items:
        enriched = dict(item)
        item_knowledge_base_ids: list[str] = []
        item_knowledge_base_names: list[str] = []
        refs: list[dict[str, Any]] = []
        for ref in _list_of_dicts(item.get("source_refs")):
            source_item_id = str(ref.get("source_item_id") or "").strip()
            lineage = lineage_for_source(source_item_id)
            if lineage.get("knowledge_base_ids"):
                ref = {**ref, **lineage}
                item_knowledge_base_ids.extend(_string_list(lineage.get("knowledge_base_ids")))
                item_knowledge_base_names.extend(_string_list(lineage.get("knowledge_base_names")))
            refs.append(ref)
        enriched["source_refs"] = refs
        item_knowledge_base_ids = list(dict.fromkeys(item_knowledge_base_ids))
        item_knowledge_base_names = list(dict.fromkeys(item_knowledge_base_names))
        if item_knowledge_base_ids:
            enriched["knowledge_base_ids"] = item_knowledge_base_ids
        if item_knowledge_base_names:
            enriched["knowledge_base_names"] = item_knowledge_base_names
        if len(item_knowledge_base_ids) == 1:
            enriched["knowledge_base_id"] = item_knowledge_base_ids[0]
        if len(item_knowledge_base_names) == 1:
            enriched["knowledge_base_name"] = item_knowledge_base_names[0]
        enriched_items.append(enriched)
    return enriched_items


def _knowledge_base_payload(store: Any, knowledge_base: Any, *, include_source_item_ids: bool = False) -> dict[str, Any]:
    source_item_ids = store.list_knowledge_base_source_item_ids(
        {knowledge_base.knowledge_base_id},
        tenant_id=knowledge_base.tenant_id,
        owner_user_id=knowledge_base.owner_user_id,
    )
    source_items = [
        item
        for item in store.list_source_items(tenant_id=knowledge_base.tenant_id)
        if item.source_item_id in source_item_ids and item.owner_user_id == knowledge_base.owner_user_id and _is_active_lifecycle(item)
    ]
    documents = [document for document in store.list_documents_for_sources(source_item_ids) if _is_active_lifecycle(document)]
    chunks = [chunk for chunk in store.list_chunks_for_sources(source_item_ids) if _is_active_lifecycle(chunk)]
    embedded_chunks = [chunk for chunk in chunks if getattr(chunk, "embedding", None)]
    embedding_models = sorted(
        {
            _knowledge_base_embedding_label(chunk)
            for chunk in chunks
            if _knowledge_base_embedding_label(chunk)
        }
    )
    source_ids = _knowledge_base_source_ids(store, knowledge_base, source_items)
    sync_runs = _knowledge_base_sync_runs(store, knowledge_base, source_ids)
    processing_spans = _knowledge_base_processing_spans(store, knowledge_base, source_item_ids, source_ids)
    offline_states = _knowledge_base_offline_states(store, knowledge_base, source_item_ids)
    digest_notes = store.list_digest_notes(
        owner_user_id=knowledge_base.owner_user_id,
        tenant_id=knowledge_base.tenant_id,
        source_item_ids=source_item_ids,
        limit=1,
    )
    processing_active = [span for span in processing_spans if str(getattr(span, "status", "")).lower() in {"pending", "running", "processing"}]
    processing_failed = [span for span in processing_spans if str(getattr(span, "status", "")).lower() in {"failed", "error"}]
    failed_sync_runs = [run for run in sync_runs if str(getattr(run, "status", "")).lower() == "failed" or int(getattr(run, "failed", 0) or 0) > 0]
    embedding_coverage = (len(embedded_chunks) / len(chunks)) if chunks else 0.0
    processing_status = (
        "empty"
        if not source_item_ids
        else "failed"
        if processing_failed or failed_sync_runs
        else "processing"
        if processing_active
        else "ready"
        if chunks
        else "pending"
    )
    offline_dirty = [state for state in offline_states if str(getattr(state, "status", "")).lower() in {"dirty", "pending", "failed"}]
    last_sync_at = _latest_datetime([getattr(run, "finished_at", None) or getattr(run, "started_at", None) for run in sync_runs])
    last_processing_at = _latest_datetime([getattr(span, "finished_at", None) or getattr(span, "started_at", None) for span in processing_spans])
    last_digest_at = _latest_datetime([getattr(note, "created_at", None) for note in digest_notes])
    payload = to_jsonable(knowledge_base)
    payload["counts"] = {
        "source_items": len(source_items) or len(source_item_ids),
        "documents": len(documents),
        "chunks": len(chunks),
        "active_chunks": len(chunks),
        "embedded_chunks": len(embedded_chunks),
        "processing_spans": len(processing_spans),
        "failed_processing_spans": len(processing_failed),
        "offline_index_states": len(offline_states),
        "offline_index_dirty": len(offline_dirty),
    }
    payload["readiness"] = {
        **dict(getattr(knowledge_base, "readiness", {}) or {}),
        "has_source_items": bool(source_item_ids),
        "has_documents": bool(documents),
        "has_chunks": bool(chunks),
        "retrieval_ready": bool(chunks),
        "processing_status": processing_status,
        "source_item_count": len(source_items) or len(source_item_ids),
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "active_chunk_count": len(chunks),
        "embedded_chunk_count": len(embedded_chunks),
        "embedding_coverage": round(embedding_coverage, 4),
        "embedding_models": embedding_models,
        "embedding_status": "complete" if chunks and len(embedded_chunks) == len(chunks) else "partial" if embedded_chunks else "missing" if chunks else "not_applicable",
        "processing_count": len(processing_active),
        "failed_processing_count": len(processing_failed) + len(failed_sync_runs),
        "offline_index_state_count": len(offline_states),
        "offline_index_dirty_count": len(offline_dirty),
        "offline_index_fresh": bool(offline_states) and not offline_dirty,
        "last_sync_at": last_sync_at,
        "last_processing_at": last_processing_at,
        "last_digest_at": last_digest_at,
        "last_error": _knowledge_base_last_error(processing_spans, sync_runs),
    }
    payload["capabilities"] = {
        "rag_scope": "source_item_membership",
        "document_retrieval": bool(chunks),
        "multi_kb_selectable": True,
    }
    if include_source_item_ids:
        payload["source_item_ids"] = sorted(source_item_ids)
    return payload


def _knowledge_base_embedding_label(chunk: Any) -> str:
    metadata = getattr(chunk, "metadata", {}) or {}
    provider = str(metadata.get("embedding_provider") or "").strip()
    model = str(metadata.get("embedding_model") or "").strip()
    return "/".join(part for part in [provider, model] if part)


def _knowledge_base_source_ids(store: Any, knowledge_base: Any, source_items: list[Any]) -> set[str]:
    source_ids = {str(getattr(item, "source_id", "") or "") for item in source_items}
    source_ids.discard("")
    for source in store.list_knowledge_sources(tenant_id=knowledge_base.tenant_id, owner_user_id=knowledge_base.owner_user_id):
        if knowledge_base.knowledge_base_id in store.list_knowledge_base_ids_for_source(
            source.knowledge_source_id,
            tenant_id=knowledge_base.tenant_id,
            owner_user_id=knowledge_base.owner_user_id,
        ):
            source_ids.add(source.knowledge_source_id)
    return source_ids


def _knowledge_base_sync_runs(store: Any, knowledge_base: Any, source_ids: set[str]) -> list[Any]:
    runs: list[Any] = []
    seen: set[str] = set()
    for source_id in sorted(source_ids):
        for run in store.list_sync_runs(
            tenant_id=knowledge_base.tenant_id,
            owner_user_id=knowledge_base.owner_user_id,
            knowledge_source_id=source_id,
            limit=5,
        ):
            run_id = str(getattr(run, "sync_run_id", "") or "")
            if run_id and run_id in seen:
                continue
            if run_id:
                seen.add(run_id)
            runs.append(run)
    return runs


def _knowledge_base_processing_spans(store: Any, knowledge_base: Any, source_item_ids: set[str], source_ids: set[str]) -> list[Any]:
    spans: list[Any] = []
    seen: set[str] = set()
    for source_item_id in sorted(source_item_ids):
        for span in store.list_processing_spans(tenant_id=knowledge_base.tenant_id, source_item_id=source_item_id, limit=10):
            span_id = str(getattr(span, "processing_span_id", "") or "")
            if span_id and span_id in seen:
                continue
            if span_id:
                seen.add(span_id)
            spans.append(span)
    for source_id in sorted(source_ids):
        for span in store.list_processing_spans(tenant_id=knowledge_base.tenant_id, knowledge_source_id=source_id, limit=10):
            span_id = str(getattr(span, "processing_span_id", "") or "")
            if span_id and span_id in seen:
                continue
            if span_id:
                seen.add(span_id)
            spans.append(span)
    return spans


def _knowledge_base_offline_states(store: Any, knowledge_base: Any, source_item_ids: set[str]) -> list[Any]:
    states: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for source_item_id in sorted(source_item_ids):
        for state in store.list_offline_index_states(tenant_id=knowledge_base.tenant_id, source_item_id=source_item_id, limit=None):
            key = (str(getattr(state, "object_type", "") or ""), str(getattr(state, "object_id", "") or ""))
            if key in seen:
                continue
            seen.add(key)
            states.append(state)
    return states


def _latest_datetime(values: list[Any]) -> str | None:
    datetimes = [value for value in values if isinstance(value, datetime)]
    if not datetimes:
        return None
    return max(datetimes).isoformat()


def _knowledge_base_last_error(processing_spans: list[Any], sync_runs: list[Any]) -> str | None:
    errors: list[tuple[datetime | None, str]] = []
    for span in processing_spans:
        error = str(getattr(span, "error", "") or "").strip()
        if error:
            errors.append((getattr(span, "finished_at", None) or getattr(span, "started_at", None), error))
    for run in sync_runs:
        error = str(getattr(run, "error", "") or "").strip()
        if error:
            errors.append((getattr(run, "finished_at", None) or getattr(run, "started_at", None), error))
    if not errors:
        return None
    return sorted(errors, key=lambda item: (item[0] or datetime.min.replace(tzinfo=UTC)).timestamp(), reverse=True)[0][1]


def _knowledge_base_slug(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().casefold()).strip("-")
    return normalized[:80] or "knowledge-base"


def _prompt_profile_lineage(store: Any, *, tenant_id: str, owner_user_id: str, profile_type: str) -> dict[str, Any]:
    effective = _effective_prompt_profiles(store, tenant_id=tenant_id, owner_user_id=owner_user_id)
    profile = dict(effective.get(profile_type) or {})
    return {
        "prompt_profile_id": profile.get("prompt_profile_id"),
        "prompt_profile_version": profile.get("current_version"),
        "prompt_profile_type": profile_type,
        "prompt_profile_scope": profile.get("scope"),
    }


def _assert_job_context_tenant(job: Any, context: RequestContext | None) -> None:
    if context and getattr(job, "tenant_id", DEFAULT_TENANT_ID) != context.tenant_id:
        raise PermissionError("job tenant mismatch")


def _workspace_source_matches(item: Any, chunks: list[Any], *, source_channel: str, query: str) -> bool:
    if source_channel and getattr(item, "source_channel", "") != source_channel:
        return False
    if not query:
        return True
    haystack = " ".join(
        [
            str(getattr(item, "title", "")),
            str(getattr(item, "source_id", "")),
            str(getattr(item, "content_text", "")),
            " ".join(str(getattr(chunk, "text", "")) for chunk in chunks),
        ]
    ).lower()
    return query in haystack


def _workspace_chunk_matches(chunk: Any, *, query: str) -> bool:
    return not query or query in str(getattr(chunk, "text", "")).lower()


def _workspace_source(item: Any, chunks: list[Any]) -> dict[str, Any]:
    text = str(getattr(item, "content_text", "") or "")
    return {
        "source_item_id": getattr(item, "source_item_id", ""),
        "source_channel": getattr(item, "source_channel", ""),
        "record_type": getattr(item, "record_type", ""),
        "source_id": getattr(item, "source_id", ""),
        "title": getattr(item, "title", "") or getattr(item, "source_id", ""),
        "url": getattr(item, "url", None),
        "created_at": getattr(item, "created_at", None),
        "snippet": text[:320],
        "chunk_count": len(chunks),
        "document_ids": sorted({getattr(chunk, "document_id", "") for chunk in chunks if getattr(chunk, "document_id", "")}),
    }


def _workspace_chunk(chunk: Any) -> dict[str, Any]:
    text = str(getattr(chunk, "text", "") or "")
    return {
        "chunk_id": getattr(chunk, "chunk_id", ""),
        "document_id": getattr(chunk, "document_id", ""),
        "source_item_id": getattr(chunk, "source_item_id", ""),
        "ordinal": getattr(chunk, "ordinal", 0),
        "snippet": text[:420],
    }


def _workspace_documents(chunks: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        document_id = str(getattr(chunk, "document_id", "") or "")
        if not document_id:
            continue
        current = grouped.setdefault(
            document_id,
            {
                "document_id": document_id,
                "source_item_id": getattr(chunk, "source_item_id", ""),
                "chunk_count": 0,
                "first_snippet": str(getattr(chunk, "text", "") or "")[:240],
            },
        )
        current["chunk_count"] += 1
    return list(grouped.values())


def _reader_document_payload(document: Any, *, max_chars: int) -> dict[str, Any]:
    body = str(getattr(document, "body", "") or "")
    truncated = len(body) > max_chars
    return {
        "document_id": getattr(document, "document_id", ""),
        "source_item_id": getattr(document, "source_item_id", ""),
        "title": getattr(document, "title", "") or getattr(document, "document_id", ""),
        "body": body[:max_chars],
        "body_truncated": truncated,
        "body_chars": len(body),
        "metadata": to_jsonable(getattr(document, "metadata", {}) or {}),
        "lifecycle_status": _lifecycle_status(document),
    }


def _reader_chunk_payload(chunk: Any) -> dict[str, Any]:
    return {
        "chunk_id": getattr(chunk, "chunk_id", ""),
        "document_id": getattr(chunk, "document_id", ""),
        "source_item_id": getattr(chunk, "source_item_id", ""),
        "ordinal": int(getattr(chunk, "ordinal", 0) or 0),
        "text": str(getattr(chunk, "text", "") or ""),
        "text_chars": len(str(getattr(chunk, "text", "") or "")),
        "metadata": to_jsonable(getattr(chunk, "metadata", {}) or {}),
        "lifecycle_status": _lifecycle_status(chunk),
    }


def _reader_passage_window_payload(window: PassageWindow) -> dict[str, Any]:
    return {
        "passage_window_id": window.passage_window_id,
        "source_item_id": window.source_item_id,
        "document_id": window.document_id,
        "ordinal": window.ordinal,
        "title": window.title,
        "text": window.text,
        "start_char": window.start_char,
        "end_char": window.end_char,
        "token_estimate": window.token_estimate,
        "metadata": to_jsonable(window.metadata),
    }


def _passage_windows_for_documents(documents: list[Any], chunks: list[Any], *, target_tokens: int = 24000) -> list[PassageWindow]:
    chunks_by_document: dict[str, list[Any]] = {}
    for chunk in chunks:
        chunks_by_document.setdefault(str(getattr(chunk, "document_id", "") or ""), []).append(chunk)
    windows: list[PassageWindow] = []
    max_chars = max(1, target_tokens * 4)
    for document in documents:
        document_id = str(getattr(document, "document_id", "") or "")
        body = str(getattr(document, "body", "") or "")
        if not body:
            body = "\n\n".join(str(getattr(chunk, "text", "") or "") for chunk in chunks_by_document.get(document_id, []))
        if not body:
            continue
        spans = _passage_spans(body, max_chars=max_chars)
        for ordinal, (start, end) in enumerate(spans):
            text = body[start:end]
            windows.append(
                PassageWindow(
                    passage_window_id=f"pw_{document_id}_{ordinal}",
                    source_item_id=str(getattr(document, "source_item_id", "") or ""),
                    document_id=document_id,
                    owner_user_id=str(getattr(document, "owner_user_id", "") or ""),
                    ordinal=ordinal,
                    title=str(getattr(document, "title", "") or document_id),
                    text=text,
                    start_char=start,
                    end_char=end,
                    token_estimate=_estimate_tokens(text),
                    metadata={
                        "windowing_policy": "document_full" if len(spans) == 1 else "paragraph_window",
                        "document_title": getattr(document, "title", "") or document_id,
                    },
                )
            )
    return windows


def _passage_spans(text: str, *, max_chars: int) -> list[tuple[int, int]]:
    if len(text) <= max_chars:
        return [(0, len(text))]
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        split_at = text.rfind("\n\n", start, end)
        if split_at <= start + max_chars // 2:
            split_at = end
        spans.append((start, split_at))
        start = split_at
        while start < len(text) and text[start].isspace():
            start += 1
    return spans


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _topic_normalized_label(label: str) -> str:
    text = str(label or "").casefold().strip()
    text = re.sub(r"[\s/_|,，。.!！?？;；:：()（）\\[\\]{}<>《》\"'`]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.isdigit() or len(text) > 80:
        return ""
    return text


def _topic_stable_id(*, tenant_id: str, owner_user_id: str, normalized_label: str) -> str:
    return f"topic_{uuid5(NAMESPACE_URL, f'pska.topic:{tenant_id}:{owner_user_id}:{normalized_label}').hex}"


def _topic_mention_stable_id(
    *,
    tenant_id: str,
    owner_user_id: str,
    topic_id: str,
    source_item_id: str,
    artifact_id: str,
) -> str:
    return f"topicmention_{uuid5(NAMESPACE_URL, f'pska.topic_mention:{tenant_id}:{owner_user_id}:{topic_id}:{source_item_id}:{artifact_id}').hex}"


def _artifact_support_stable_id(
    *,
    tenant_id: str,
    owner_user_id: str,
    artifact_type: str,
    artifact_id: str,
    support_type: str,
    source_item_id: str,
    chunk_id: str = "",
) -> str:
    return f"support_{uuid5(NAMESPACE_URL, f'pska.support:{tenant_id}:{owner_user_id}:{artifact_type}:{artifact_id}:{support_type}:{source_item_id}:{chunk_id}').hex}"


def _linking_review_stable_id(*, tenant_id: str, owner_user_id: str, topic_id: str, source_refs: list[dict[str, Any]]) -> str:
    source_key = ",".join(sorted(str(ref.get("source_item_id") or "") for ref in source_refs))
    return f"rev_link_{uuid5(NAMESPACE_URL, f'pska.linking_review:{tenant_id}:{owner_user_id}:{topic_id}:{source_key}').hex}"


def _knowledge_topic_payload(
    topic: Any,
    *,
    mentions: list[Any] | None = None,
    supports: list[Any] | None = None,
    source_items: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mentions = mentions or []
    supports = supports or []
    source_items = source_items or {}
    if source_items:
        active_source_ids = set(source_items)
        mentions = [mention for mention in mentions if str(getattr(mention, "source_item_id", "") or "") in active_source_ids]
        supports = [support for support in supports if str(getattr(support, "source_item_id", "") or "") in active_source_ids]
    quality = _topic_quality_fields(topic, mentions, supports)
    eligible_mentions = [mention for mention in mentions if _topic_mention_review_eligible(mention)]
    diagnostic_refs = _topic_source_refs_from_mentions(mentions)
    strong_refs = _topic_source_refs_from_mentions(eligible_mentions)
    display_refs = strong_refs if quality["review_eligible"] else diagnostic_refs
    source_ids = sorted({str(ref.get("source_item_id") or "") for ref in display_refs if ref.get("source_item_id")})
    return {
        "topic_id": getattr(topic, "topic_id", ""),
        "tenant_id": getattr(topic, "tenant_id", DEFAULT_TENANT_ID),
        "owner_user_id": getattr(topic, "owner_user_id", ""),
        "label": getattr(topic, "label", ""),
        "normalized_label": getattr(topic, "normalized_label", ""),
        "topic_type": getattr(topic, "topic_type", "topic"),
        "description": getattr(topic, "description", ""),
        "confidence": getattr(topic, "confidence", 0.0),
        "producer": getattr(topic, "producer", ""),
        "metadata": dict(getattr(topic, "metadata", {}) or {}),
        "created_at": getattr(topic, "created_at", None),
        "updated_at": getattr(topic, "updated_at", None),
        "mention_count": len(mentions),
        "support_count": len(supports),
        "source_count": len(source_ids),
        "source_refs": display_refs,
        "diagnostic_source_refs": diagnostic_refs,
        "strong_source_refs": strong_refs,
        **quality,
        "sources": [
            {
                "source_item_id": source_id,
                "title": str(getattr(source_items.get(source_id), "title", "") or source_id),
                "source_type": str(getattr(source_items.get(source_id), "source_type", "") or ""),
            }
            for source_id in source_ids[:12]
        ],
        "mentions": [_topic_mention_payload(mention) for mention in mentions[:20]],
    }


def _topic_mention_payload(mention: Any) -> dict[str, Any]:
    return {
        "topic_mention_id": getattr(mention, "topic_mention_id", ""),
        "topic_id": getattr(mention, "topic_id", ""),
        "tenant_id": getattr(mention, "tenant_id", DEFAULT_TENANT_ID),
        "owner_user_id": getattr(mention, "owner_user_id", ""),
        "source_item_id": getattr(mention, "source_item_id", ""),
        "document_id": getattr(mention, "document_id", None),
        "chunk_id": getattr(mention, "chunk_id", None),
        "artifact_type": getattr(mention, "artifact_type", ""),
        "artifact_id": getattr(mention, "artifact_id", ""),
        "mention_text": getattr(mention, "mention_text", ""),
        "confidence": getattr(mention, "confidence", 0.0),
        "producer": getattr(mention, "producer", ""),
        "metadata": dict(getattr(mention, "metadata", {}) or {}),
        "created_at": getattr(mention, "created_at", None),
    }


def _topic_source_refs_from_mentions(mentions: list[Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for mention in mentions:
        mention_metadata = dict(getattr(mention, "metadata", {}) or {})
        metadata_refs = _list_of_dicts(mention_metadata.get("source_refs"))
        if metadata_refs:
            for metadata_ref in metadata_refs:
                source_item_id = str(metadata_ref.get("source_item_id") or getattr(mention, "source_item_id", "") or "")
                if not source_item_id:
                    continue
                ref = {
                    **metadata_ref,
                    "source_item_id": source_item_id,
                    "document_id": metadata_ref.get("document_id") or getattr(mention, "document_id", None),
                    "chunk_id": metadata_ref.get("chunk_id") or getattr(mention, "chunk_id", None),
                    "mention_text": metadata_ref.get("mention_text") or getattr(mention, "mention_text", ""),
                }
                key = (source_item_id, str(ref.get("document_id") or ""), str(ref.get("chunk_id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
            continue
        source_item_id = str(getattr(mention, "source_item_id", "") or "")
        if not source_item_id:
            continue
        ref = {
            "source_item_id": source_item_id,
            "document_id": getattr(mention, "document_id", None),
            "chunk_id": getattr(mention, "chunk_id", None),
            "mention_text": getattr(mention, "mention_text", ""),
        }
        key = (source_item_id, str(ref.get("document_id") or ""), str(ref.get("chunk_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def _topic_mention_review_eligible(mention: Any) -> bool:
    metadata = getattr(mention, "metadata", {}) if isinstance(getattr(mention, "metadata", {}), dict) else {}
    return (
        bool(metadata.get("review_eligible"))
        and str(metadata.get("quality_tier") or "").lower() == "strong"
        and bool(_list_of_dicts(metadata.get("source_refs")))
    )


def _topic_quality_fields(topic: Any, mentions: list[Any], supports: list[Any]) -> dict[str, Any]:
    metadata = dict(getattr(topic, "metadata", {}) or {})
    mention_support_kinds = {
        kind
        for mention in mentions
        for kind in _string_list((getattr(mention, "metadata", {}) or {}).get("support_kinds"))
    }
    support_kinds = sorted(set(_string_list(metadata.get("support_kinds"))) | mention_support_kinds)
    eligible_mentions = [mention for mention in mentions if _topic_mention_review_eligible(mention)]
    active_strong_supports = [
        support
        for support in supports
        if getattr(support, "status", "active") == "active"
        and str((getattr(support, "metadata", {}) or {}).get("quality_tier") or "").lower() == "strong"
    ]
    review_eligible = bool(metadata.get("review_eligible")) and len({getattr(mention, "source_item_id", "") for mention in eligible_mentions}) >= 2
    quality_tier = "strong" if review_eligible or active_strong_supports else str(metadata.get("quality_tier") or "diagnostic")
    return {
        "quality_tier": quality_tier,
        "support_kinds": support_kinds,
        "promotion_reason": metadata.get("promotion_reason") or ("shared_strong_support" if review_eligible else "diagnostic_only"),
        "review_eligible": review_eligible,
        "diagnostics": metadata.get("diagnostics")
        or {
            "lexical_only": quality_tier != "strong",
            "strong_source_count": len({getattr(mention, "source_item_id", "") for mention in eligible_mentions}),
        },
    }


def _topic_paths_from_topic_payloads(topics: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for topic in topics:
        if not bool(topic.get("review_eligible")) or str(topic.get("quality_tier") or "") != "strong":
            continue
        refs = _list_of_dicts(topic.get("source_refs"))
        source_ids = sorted({str(ref.get("source_item_id") or "") for ref in refs if ref.get("source_item_id")})
        if len(source_ids) < 2:
            continue
        paths.append(
            {
                "path_id": f"topic_path_{topic.get('topic_id')}",
                "path_type": "shared_topic",
                "topic_id": topic.get("topic_id"),
                "topic_label": topic.get("label"),
                "source_item_ids": source_ids,
                "source_refs": refs,
                "confidence": topic.get("confidence"),
                "summary": f"{len(source_ids)} 个资料条目通过主题“{topic.get('label')}”相连。",
            }
        )
        if len(paths) >= limit:
            break
    return paths


def _linking_topic_candidates_for_source(
    *,
    item: Any,
    documents: list[Any],
    chunks: list[Any],
    claims: list[Any],
    digest_notes: list[Any],
    max_topics: int,
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    source_negation_text = _source_negation_guard_text(documents=documents, chunks=chunks)

    def add_labels(
        text: str,
        *,
        weight: float,
        artifact_type: str,
        artifact_id: str,
        support_kind: str,
        quality_tier: str,
        review_eligible: bool,
        promotion_reason: str,
        document_id: str | None = None,
        chunk_id: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        for label in _topic_labels_from_text(text):
            normalized = _topic_normalized_label(label)
            if not normalized:
                continue
            if _topic_label_is_generic(normalized):
                continue
            if _text_has_negated_label(text, label) or _text_has_negated_label(text, normalized):
                continue
            if _text_has_negated_label(source_negation_text, label) or _text_has_negated_label(source_negation_text, normalized):
                continue
            refs = source_refs or [
                {
                    "source_item_id": getattr(item, "source_item_id", ""),
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "mention_text": _trim_words(_ask_clean_evidence_text(text), 40),
                }
            ]
            current = stats.setdefault(
                normalized,
                {
                    "label": label,
                    "score": 0.0,
                    "sources": set(),
                    "support_kinds": set(),
                    "support_artifacts": [],
                    "source_refs": [],
                    "promotion_reasons": [],
                    "mention_text": "",
                    "artifact_type": artifact_type,
                    "artifact_id": artifact_id,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "quality_tier": "diagnostic",
                    "review_eligible": False,
                },
            )
            current["score"] = float(current.get("score") or 0.0) + weight
            current["sources"].add(artifact_type)
            current["support_kinds"].add(support_kind)
            current["support_artifacts"].append(
                {
                    "artifact_type": artifact_type,
                    "artifact_id": artifact_id,
                    "support_kind": support_kind,
                    "quality_tier": quality_tier,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                }
            )
            current["source_refs"].extend(ref for ref in refs if ref.get("source_item_id"))
            if promotion_reason:
                current["promotion_reasons"].append(promotion_reason)
            if quality_tier == "strong":
                current["quality_tier"] = "strong"
            if review_eligible:
                current["review_eligible"] = True
                current["artifact_type"] = artifact_type
                current["artifact_id"] = artifact_id
                current["document_id"] = document_id
                current["chunk_id"] = chunk_id
            if not current.get("mention_text"):
                current["mention_text"] = _trim_words(_ask_clean_evidence_text(text), 40)

    title_text = str(getattr(item, "title", "") or "")
    add_labels(
        title_text,
        weight=2.2,
        artifact_type="source_item",
        artifact_id=getattr(item, "source_item_id", ""),
        support_kind="source_title",
        quality_tier="strong",
        review_eligible=True,
        promotion_reason="source_title",
    )
    for document in documents[:10]:
        document_id = getattr(document, "document_id", None)
        document_title = str(getattr(document, "title", "") or "")
        add_labels(
            document_title,
            weight=1.8,
            artifact_type="document",
            artifact_id=getattr(document, "document_id", "") or getattr(item, "source_item_id", ""),
            support_kind="document_title",
            quality_tier="strong",
            review_eligible=True,
            promotion_reason="document_title",
            document_id=document_id,
        )
        for heading in _document_heading_texts(str(getattr(document, "body", "") or ""))[:12]:
            add_labels(
                heading,
                weight=1.6,
                artifact_type="document",
                artifact_id=getattr(document, "document_id", "") or getattr(item, "source_item_id", ""),
                support_kind="document_heading",
                quality_tier="strong",
                review_eligible=True,
                promotion_reason="document_heading",
                document_id=document_id,
            )
        body_text = str(getattr(document, "body", "") or "")[:3000]
        add_labels(
            body_text,
            weight=0.6,
            artifact_type="document",
            artifact_id=getattr(document, "document_id", "") or getattr(item, "source_item_id", ""),
            support_kind="document_body_lexical",
            quality_tier="diagnostic",
            review_eligible=False,
            promotion_reason="diagnostic_document_body",
            document_id=document_id,
        )
    for chunk in sorted(chunks, key=lambda value: int(getattr(value, "ordinal", 0) or 0))[:80]:
        text = str(getattr(chunk, "text", "") or "")[:1800]
        add_labels(
            text,
            weight=0.5,
            artifact_type="chunk",
            artifact_id=getattr(chunk, "chunk_id", "") or getattr(item, "source_item_id", ""),
            support_kind="chunk_lexical",
            quality_tier="diagnostic",
            review_eligible=False,
            promotion_reason="diagnostic_chunk_lexical",
            document_id=getattr(chunk, "document_id", None),
            chunk_id=getattr(chunk, "chunk_id", None),
        )
    for claim in claims[:20]:
        text = " ".join(
            str(getattr(claim, field, "") or "")
            for field in ("subject", "predicate", "object", "statement", "evidence_text")
        )
        add_labels(
            text,
            weight=2.0,
            artifact_type="knowledge_claim",
            artifact_id=getattr(claim, "knowledge_claim_id", "") or getattr(item, "source_item_id", ""),
            support_kind="knowledge_claim",
            quality_tier="strong",
            review_eligible=bool(_source_refs_payload(getattr(claim, "source_refs", []))),
            promotion_reason="source_ref_claim",
            source_refs=_source_refs_payload(getattr(claim, "source_refs", [])),
        )
    for note in digest_notes[:12]:
        text = " ".join(
            [
                str(getattr(note, "title", "") or ""),
                str(getattr(note, "synopsis", "") or ""),
                " ".join(str(value) for value in getattr(note, "key_points", []) or []),
                " ".join(str(value) for value in getattr(note, "open_questions", []) or []),
            ]
        )
        add_labels(
            text,
            weight=1.9,
            artifact_type="digest_note",
            artifact_id=getattr(note, "digest_note_id", "") or getattr(item, "source_item_id", ""),
            support_kind="digest_note",
            quality_tier="strong",
            review_eligible=bool(_source_refs_payload(getattr(note, "source_refs", []))),
            promotion_reason="source_ref_digest",
            source_refs=_source_refs_payload(getattr(note, "source_refs", [])),
        )

    candidates = []
    for normalized, data in stats.items():
        score = float(data.get("score") or 0.0)
        if score < 0.5:
            continue
        support_kinds = sorted(data.get("support_kinds") or [])
        support_artifacts = _dedupe_support_artifacts(data.get("support_artifacts") or [])
        source_refs = _dedupe_source_ref_dicts(_list_of_dicts(data.get("source_refs")))
        quality_tier = str(data.get("quality_tier") or "diagnostic")
        review_eligible = bool(data.get("review_eligible")) and bool(source_refs) and quality_tier == "strong"
        candidates.append(
            {
                "label": data.get("label") or normalized,
                "normalized_label": normalized,
                "confidence": min(0.95, 0.28 + score * 0.08 + len(support_kinds) * 0.04 + (0.12 if review_eligible else 0.0)),
                "mention_text": data.get("mention_text") or "",
                "artifact_type": data.get("artifact_type") or "source_item",
                "artifact_id": data.get("artifact_id") or getattr(item, "source_item_id", ""),
                "document_id": data.get("document_id"),
                "chunk_id": data.get("chunk_id"),
                "score": score,
                "quality_tier": quality_tier,
                "support_kinds": support_kinds,
                "support_artifacts": support_artifacts,
                "source_refs": source_refs,
                "promotion_reason": next(iter(data.get("promotion_reasons") or []), "diagnostic"),
                "review_eligible": review_eligible,
            }
        )
    candidates.sort(key=lambda candidate: (float(candidate.get("score") or 0.0), float(candidate.get("confidence") or 0.0)), reverse=True)
    return candidates[:max_topics]


def _source_negation_guard_text(*, documents: list[Any], chunks: list[Any]) -> str:
    parts: list[str] = []
    for document in documents[:10]:
        parts.append(str(getattr(document, "title", "") or ""))
        parts.append(str(getattr(document, "body", "") or "")[:3000])
    for chunk in sorted(chunks, key=lambda value: int(getattr(value, "ordinal", 0) or 0))[:20]:
        parts.append(str(getattr(chunk, "text", "") or "")[:1200])
    return "\n".join(part for part in parts if part)


def _document_heading_texts(text: str) -> list[str]:
    headings = []
    for line in str(text or "").splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            headings.append(match.group(1).strip())
    return headings


def _topic_label_is_generic(normalized_label: str) -> bool:
    label = str(normalized_label or "").casefold().strip()
    if not label:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?", label):
        return True
    generic = {
        "article",
        "also",
        "and",
        "attachment",
        "chunk",
        "content",
        "data",
        "digest",
        "doc",
        "document",
        "evidence",
        "file",
        "first",
        "for",
        "graph",
        "heading",
        "in",
        "integration",
        "is",
        "memo",
        "note",
        "of",
        "project",
        "report",
        "review",
        "second",
        "source",
        "summary",
        "system",
        "text",
        "the",
        "to",
        "topic",
        "use",
        "uses",
        "资料",
        "文档",
        "文件",
        "材料",
        "主题",
        "内容",
        "摘要",
        "总结",
        "系统",
        "项目",
        "报告",
        "证据",
        "图谱",
    }
    return label in generic


def _dedupe_support_artifacts(values: list[Any]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        item = {key: value.get(key) for key in ["artifact_type", "artifact_id", "support_kind", "quality_tier", "document_id", "chunk_id"] if value.get(key)}
        key = (
            str(item.get("artifact_type") or ""),
            str(item.get("artifact_id") or ""),
            str(item.get("support_kind") or ""),
            str(item.get("chunk_id") or item.get("document_id") or ""),
        )
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _topic_labels_from_text(text: str) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for term in _ask_evidence_terms(text):
        normalized = _topic_normalized_label(term)
        if not normalized or normalized in seen:
            continue
        if len(normalized) < 2 or len(normalized) > 40:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
            continue
        seen.add(normalized)
        labels.append(term)
        if len(labels) >= 24:
            break
    return labels


def _workspace_graph_nodes_edges(
    *,
    source_items: list[Any],
    documents: list[Any],
    passage_windows: list[PassageWindow],
    claims: list[Any],
    digest_notes: list[Any],
    memories: list[Any],
    review_items: list[Any],
    entities: list[Any],
    hyperedges: list[tuple[Any, list[Any]]],
    topics: list[Any],
    topic_mentions: list[Any],
    artifact_supports: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    passage_by_document = {window.document_id: window for window in passage_windows}
    passage_by_source = {window.source_item_id: window for window in passage_windows}
    entity_by_id = {getattr(entity, "entity_id", ""): entity for entity in entities}

    def add_node(node_id: str, node_type: str, label: str, summary: str = "", **extra: Any) -> None:
        if not node_id or node_id in nodes:
            return
        nodes[node_id] = {
            "id": node_id,
            "type": node_type,
            "label": label,
            "summary": summary,
            **extra,
        }

    def add_edge(source: str, target: str, edge_type: str, label: str | None = None, **extra: Any) -> None:
        if source not in nodes or target not in nodes:
            return
        edge_id = f"{source}->{target}:{edge_type}:{label or ''}"
        if edge_id in edges:
            return
        edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "type": edge_type,
            "label": label or edge_type,
            **extra,
        }

    def add_phrase(text: Any, source_node_id: str, edge_type: str) -> None:
        phrase = " ".join(str(text or "").strip().split())
        if not phrase or len(phrase) > 120 or source_node_id not in nodes:
            return
        phrase_id = f"phrase:{uuid5(NAMESPACE_URL, phrase.casefold()).hex}"
        add_node(
            phrase_id,
            "phrase",
            phrase,
            "query/entity phrase seed",
            object_type="phrase",
            object_id=phrase,
        )
        add_edge(source_node_id, phrase_id, edge_type)

    for item in source_items:
        add_node(
            f"source:{item.source_item_id}",
            "source",
            item.title or item.source_id or item.source_item_id,
            str(getattr(item, "content_text", "") or "")[:240],
            object_type="source_item",
            object_id=item.source_item_id,
            source_refs=[{"source_item_id": item.source_item_id}],
        )
    for document in documents:
        document_id = getattr(document, "document_id", "")
        source_id = getattr(document, "source_item_id", "")
        add_node(
            f"document:{document_id}",
            "document",
            getattr(document, "title", "") or document_id,
            str(getattr(document, "body", "") or "")[:240],
            object_type="document",
            object_id=document_id,
            source_refs=[{"source_item_id": source_id, "document_id": document_id}],
        )
        add_edge(f"source:{source_id}", f"document:{document_id}", "contains")
    for window in passage_windows:
        add_node(
            f"passage:{window.passage_window_id}",
            "passage",
            window.title,
            window.text[:260],
            object_type="passage_window",
            object_id=window.passage_window_id,
            token_estimate=window.token_estimate,
            source_refs=[{"source_item_id": window.source_item_id, "document_id": window.document_id, "passage_window_id": window.passage_window_id}],
        )
        add_edge(f"document:{window.document_id}", f"passage:{window.passage_window_id}", "contains")
    for claim in claims:
        claim_id = getattr(claim, "knowledge_claim_id", "")
        source_refs = _source_refs_payload(getattr(claim, "source_refs", []))
        add_node(
            f"claim:{claim_id}",
            "claim",
            getattr(claim, "statement", "") or claim_id,
            getattr(claim, "evidence_text", "") or "",
            object_type="knowledge_claim",
            object_id=claim_id,
            confidence=getattr(claim, "confidence", 0.0),
            source_refs=source_refs,
        )
        _add_source_ref_edges(add_edge, source_refs, f"claim:{claim_id}", "grounds", passage_by_document, passage_by_source)
        add_phrase(getattr(claim, "subject", ""), f"claim:{claim_id}", "mentions")
        add_phrase(getattr(claim, "object", ""), f"claim:{claim_id}", "mentions")
    for note in digest_notes:
        note_id = getattr(note, "digest_note_id", "")
        source_refs = _source_refs_payload(getattr(note, "source_refs", []))
        add_node(
            f"digest:{note_id}",
            "digest",
            getattr(note, "title", "") or note_id,
            getattr(note, "synopsis", "") or "",
            object_type="digest_note",
            object_id=note_id,
            confidence=getattr(note, "confidence", 0.0),
            source_refs=source_refs,
        )
        _add_source_ref_edges(add_edge, source_refs, f"digest:{note_id}", "summarizes", passage_by_document, passage_by_source)
        for claim in claims[:20]:
            if _source_refs_overlap(source_refs, _source_refs_payload(getattr(claim, "source_refs", []))):
                add_edge(f"digest:{note_id}", f"claim:{getattr(claim, 'knowledge_claim_id', '')}", "summarizes")
    for memory in memories:
        memory_id = getattr(memory, "agent_memory_id", "")
        source_refs = _source_refs_payload(getattr(memory, "source_refs", []))
        add_node(
            f"memory:{memory_id}",
            "memory",
            getattr(memory, "text", "") or memory_id,
            f"confidence {float(getattr(memory, 'confidence', 0.0) or 0.0):.2f}",
            object_type="agent_memory",
            object_id=memory_id,
            confidence=getattr(memory, "confidence", 0.0),
            source_refs=source_refs,
        )
        _add_source_ref_edges(add_edge, source_refs, f"memory:{memory_id}", "remembered_from", passage_by_document, passage_by_source)
    for review in review_items:
        review_id = getattr(review, "review_item_id", "")
        proposal = getattr(review, "proposal", {}) if isinstance(getattr(review, "proposal", {}), dict) else {}
        review_type = str(getattr(getattr(review, "review_type", ""), "value", getattr(review, "review_type", "")))
        node_type = "memory_suggestion" if "memory" in review_type or "profile" in review_type else "action"
        source_refs = _source_refs_payload(proposal.get("source_refs") or proposal.get("sourceRefs") or [])
        add_node(
            f"{node_type}:{review_id}",
            node_type,
            getattr(review, "title", "") or proposal.get("plain_text_summary") or review_id,
            proposal.get("plain_text_summary") or proposal.get("summary") or review_type,
            object_type="review_item",
            object_id=review_id,
            source_refs=source_refs,
        )
        _add_source_ref_edges(add_edge, source_refs, f"{node_type}:{review_id}", "needs_review_from", passage_by_document, passage_by_source)
    for topic in topics:
        topic_id = getattr(topic, "topic_id", "")
        topic_topic_mentions = [mention for mention in topic_mentions if getattr(mention, "topic_id", "") == topic_id]
        topic_supports = [support for support in artifact_supports if getattr(support, "topic_id", "") == topic_id or getattr(support, "artifact_id", "") == topic_id]
        quality = _topic_quality_fields(topic, topic_topic_mentions, topic_supports)
        add_node(
            f"topic:{topic_id}",
            "topic",
            getattr(topic, "label", "") or topic_id,
            getattr(topic, "description", "") or f"confidence {float(getattr(topic, 'confidence', 0.0) or 0.0):.2f}",
            object_type="knowledge_topic",
            object_id=topic_id,
            confidence=getattr(topic, "confidence", 0.0),
            source_refs=_topic_source_refs_from_mentions([mention for mention in topic_topic_mentions if _topic_mention_review_eligible(mention)])
            or _topic_source_refs_from_mentions(topic_topic_mentions),
            **quality,
        )
    for mention in topic_mentions:
        topic_node_id = f"topic:{getattr(mention, 'topic_id', '')}"
        source_id = str(getattr(mention, "source_item_id", "") or "")
        if topic_node_id not in nodes or not source_id:
            continue
        mention_metadata = dict(getattr(mention, "metadata", {}) or {})
        source_ref = {
            "source_item_id": source_id,
            "document_id": getattr(mention, "document_id", None),
            "chunk_id": getattr(mention, "chunk_id", None),
        }
        passage = passage_by_document.get(str(getattr(mention, "document_id", "") or "")) or passage_by_source.get(source_id)
        if passage and f"passage:{passage.passage_window_id}" in nodes:
            add_edge(
                f"passage:{passage.passage_window_id}",
                topic_node_id,
                "mentions_topic",
                "mentions_topic",
                confidence=getattr(mention, "confidence", 0.0),
                source_refs=[source_ref],
                quality_tier=mention_metadata.get("quality_tier") or "diagnostic",
                review_eligible=bool(mention_metadata.get("review_eligible")),
            )
        add_edge(
            f"source:{source_id}",
            topic_node_id,
            "mentions_topic",
            "mentions_topic",
            confidence=getattr(mention, "confidence", 0.0),
            source_refs=[source_ref],
            quality_tier=mention_metadata.get("quality_tier") or "diagnostic",
            review_eligible=bool(mention_metadata.get("review_eligible")),
        )
    for support in artifact_supports:
        if getattr(support, "artifact_type", "") != "review_item" or getattr(support, "support_type", "") != "shared_topic_source":
            continue
        topic_node_id = f"topic:{getattr(support, 'topic_id', '')}"
        review_node_ids = [
            f"action:{getattr(support, 'artifact_id', '')}",
            f"memory_suggestion:{getattr(support, 'artifact_id', '')}",
        ]
        for review_node_id in review_node_ids:
            if topic_node_id in nodes and review_node_id in nodes:
                add_edge(topic_node_id, review_node_id, "supports_review_candidate", "supports_review_candidate")
    for entity in entities:
        entity_id = getattr(entity, "entity_id", "")
        add_node(
            f"entity:{entity_id}",
            "entity",
            getattr(entity, "label", "") or entity_id,
            getattr(entity, "entity_type", "") or "entity",
            object_type="entity",
            object_id=entity_id,
        )
        add_phrase(getattr(entity, "label", ""), f"entity:{entity_id}", "links_to")
    for edge, members in hyperedges:
        hyperedge_id = getattr(edge, "hyperedge_id", "")
        source_refs = _source_refs_payload(getattr(edge, "source_refs", []))
        fact_id = f"fact:{hyperedge_id}"
        statement = _fact_statement(edge, members, entity_by_id)
        add_node(
            fact_id,
            "fact",
            statement or getattr(edge, "relation_type", "") or hyperedge_id,
            getattr(edge, "evidence_text", "") or statement,
            object_type="fact",
            object_id=hyperedge_id,
            confidence=getattr(edge, "confidence", 0.0),
            source_refs=source_refs,
            relation_type=getattr(edge, "relation_type", ""),
            hyperedge_id=hyperedge_id,
        )
        add_node(
            f"hyperedge:{hyperedge_id}",
            "hyperedge",
            getattr(edge, "relation_type", "") or hyperedge_id,
            getattr(edge, "evidence_text", "") or "",
            object_type="hyperedge",
            object_id=hyperedge_id,
            confidence=getattr(edge, "confidence", 0.0),
            source_refs=source_refs,
        )
        add_edge(fact_id, f"hyperedge:{hyperedge_id}", "represented_by", "represented_by")
        _add_source_ref_edges(add_edge, source_refs, f"hyperedge:{hyperedge_id}", "evidence", passage_by_document, passage_by_source)
        _add_source_ref_edges(add_edge, source_refs, fact_id, "grounds", passage_by_document, passage_by_source)
        for member in members:
            entity_id = getattr(member, "entity_id", "")
            entity = entity_by_id.get(entity_id)
            if entity:
                add_node(f"entity:{entity_id}", "entity", getattr(entity, "label", "") or entity_id, getattr(entity, "entity_type", "") or "entity", object_type="entity", object_id=entity_id)
            add_edge(f"entity:{entity_id}", f"hyperedge:{hyperedge_id}", "member", getattr(member, "role", "") or "member")
            add_edge(f"entity:{entity_id}", fact_id, "participates_in", getattr(member, "role", "") or "participant")
        for claim in claims[:40]:
            if _source_refs_overlap(source_refs, _source_refs_payload(getattr(claim, "source_refs", []))):
                add_edge(f"claim:{getattr(claim, 'knowledge_claim_id', '')}", f"hyperedge:{hyperedge_id}", "formalizes")
                add_edge(f"claim:{getattr(claim, 'knowledge_claim_id', '')}", fact_id, "formalizes")
        for note in digest_notes[:20]:
            if _source_refs_overlap(source_refs, _source_refs_payload(getattr(note, "source_refs", []))):
                add_edge(f"digest:{getattr(note, 'digest_note_id', '')}", f"hyperedge:{hyperedge_id}", "suggests_relationship")
                add_edge(f"digest:{getattr(note, 'digest_note_id', '')}", fact_id, "suggests_relationship")
    return list(nodes.values()), list(edges.values())


def _filter_workspace_graph_projection(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    node_types: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not node_types:
        return nodes, edges
    filtered_nodes = [node for node in nodes if str(node.get("type") or "") in node_types]
    node_ids = {str(node.get("id") or "") for node in filtered_nodes}
    filtered_edges = [
        edge
        for edge in edges
        if str(edge.get("source") or "") in node_ids and str(edge.get("target") or "") in node_ids
    ]
    return filtered_nodes, filtered_edges


def _workspace_graph_subgraph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    node_id: str,
    hops: int,
    node_types: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    node_by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    if node_id not in node_by_id:
        return [], []
    adjacency: dict[str, list[dict[str, Any]]] = {item: [] for item in node_by_id}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in adjacency and target in adjacency:
            adjacency[source].append(edge)
            adjacency[target].append(edge)
    selected_ids = {node_id}
    frontier = {node_id}
    for _ in range(hops):
        next_frontier: set[str] = set()
        for current in frontier:
            for edge in adjacency.get(current, []):
                neighbor = str(edge.get("target") if edge.get("source") == current else edge.get("source") or "")
                if neighbor and neighbor not in selected_ids:
                    selected_ids.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    filtered_nodes = [node for node in nodes if str(node.get("id") or "") in selected_ids]
    if node_types:
        filtered_nodes = [node for node in filtered_nodes if str(node.get("type") or "") in node_types or str(node.get("id") or "") == node_id]
    filtered_ids = {str(node.get("id") or "") for node in filtered_nodes}
    filtered_edges = [
        edge
        for edge in edges
        if str(edge.get("source") or "") in filtered_ids and str(edge.get("target") or "") in filtered_ids
    ]
    return filtered_nodes, filtered_edges


def _workspace_graph_search_nodes(
    nodes: list[dict[str, Any]],
    *,
    query: str,
    node_types: set[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    terms = [term for term in re.split(r"\s+", query.casefold().strip()) if term]
    if not terms:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for node in nodes:
        node_type = str(node.get("type") or "")
        if node_types and node_type not in node_types:
            continue
        haystack = " ".join(
            str(value or "")
            for value in (
                node.get("id"),
                node.get("label"),
                node.get("summary"),
                node.get("object_type"),
                node.get("object_id"),
            )
        ).casefold()
        matched = sum(1 for term in terms if term in haystack)
        if not matched:
            continue
        exact = 2.0 if query.casefold().strip() in haystack else 0.0
        score = matched + exact + (_graph_node_priority(node_type) * 0.05)
        scored.append((score, node))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "id": node.get("id"),
            "type": node.get("type"),
            "label": node.get("label"),
            "summary": node.get("summary"),
            "score": round(score, 3),
        }
        for score, node in scored[:limit]
    ]


def _workspace_graph_evidence_path(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    node_id: str,
) -> dict[str, Any]:
    evidence_labels = {"contains", "grounds", "evidence", "summarizes", "formalizes", "remembered_from", "needs_review_from", "suggests_relationship"}
    node_by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    evidence_edges = [
        edge
        for edge in edges
        if str(edge.get("label") or edge.get("type") or "") in evidence_labels
    ]
    evidence_node_ids = {node_id}
    for edge in evidence_edges:
        if edge.get("source") in evidence_node_ids or edge.get("target") in evidence_node_ids:
            evidence_node_ids.add(str(edge.get("source") or ""))
            evidence_node_ids.add(str(edge.get("target") or ""))
    evidence_nodes = [node_by_id[item] for item in evidence_node_ids if item in node_by_id]
    return {
        "node_id": node_id,
        "nodes": evidence_nodes,
        "edges": [
            edge
            for edge in evidence_edges
            if str(edge.get("source") or "") in evidence_node_ids and str(edge.get("target") or "") in evidence_node_ids
        ],
        "evidence_node_count": sum(1 for node in evidence_nodes if node.get("type") in {"source", "document", "passage"}),
        "understanding_node_count": sum(1 for node in evidence_nodes if node.get("type") not in {"source", "document", "passage"}),
    }


def _workspace_graph_insights(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    degree: dict[str, int] = {node_id: 0 for node_id in node_by_id}
    incoming: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_by_id}
    outgoing: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_by_id}
    evidence_edge_labels = {"contains", "grounds", "evidence", "summarizes", "formalizes", "remembered_from", "needs_review_from"}
    graph_edge_labels = {"formalizes", "member", "participates_in", "represented_by", "suggests_relationship", "links_to", "mentions"}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in degree:
            degree[source] += 1
            outgoing[source].append(edge)
        if target in degree:
            degree[target] += 1
            incoming[target].append(edge)
    type_counts: dict[str, int] = {}
    grounded_by_type: dict[str, int] = {}
    for node in nodes:
        node_type = str(node.get("type") or "unknown")
        node_id = str(node.get("id") or "")
        type_counts[node_type] = type_counts.get(node_type, 0) + 1
        source_refs = node.get("source_refs") if isinstance(node.get("source_refs"), list) else []
        has_evidence_edge = any(str(edge.get("label") or edge.get("type") or "") in evidence_edge_labels for edge in incoming.get(node_id, []) + outgoing.get(node_id, []))
        if source_refs or has_evidence_edge:
            grounded_by_type[node_type] = grounded_by_type.get(node_type, 0) + 1
    central_nodes = sorted(nodes, key=lambda node: (degree.get(str(node.get("id")), 0), _graph_node_priority(str(node.get("type") or ""))), reverse=True)[:12]
    clusters = _workspace_graph_topic_clusters(node_by_id, edges, degree)
    guided_tour = _workspace_graph_guided_tour(nodes, edges, degree, clusters)
    return {
        "layer_coverage": {
            "evidence": sum(type_counts.get(item, 0) for item in ("source", "document", "passage")),
            "understanding": sum(type_counts.get(item, 0) for item in ("claim", "digest")),
            "semantic": sum(type_counts.get(item, 0) for item in ("entity", "phrase", "fact", "hyperedge")),
            "review": sum(type_counts.get(item, 0) for item in ("memory", "memory_suggestion", "action")),
            "exploration": len(nodes),
        },
        "evidence_health": {
            "grounded_nodes": sum(grounded_by_type.values()),
            "total_nodes": len(nodes),
            "grounded_ratio": round(sum(grounded_by_type.values()) / max(1, len(nodes)), 3),
            "grounded_by_type": grounded_by_type,
            "evidence_edge_count": sum(1 for edge in edges if str(edge.get("label") or edge.get("type") or "") in evidence_edge_labels),
            "semantic_edge_count": sum(1 for edge in edges if str(edge.get("label") or edge.get("type") or "") in graph_edge_labels),
        },
        "central_nodes": [_graph_insight_node(node, degree.get(str(node.get("id")), 0)) for node in central_nodes],
        "topic_clusters": clusters,
        "guided_tour": guided_tour,
    }


def _workspace_graph_topic_clusters(
    node_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    degree: dict[str, int],
) -> list[dict[str, Any]]:
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_by_id}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)
    visited: set[str] = set()
    clusters: list[dict[str, Any]] = []
    for node_id in sorted(node_by_id, key=lambda item: degree.get(item, 0), reverse=True):
        if node_id in visited:
            continue
        queue = [node_id]
        component: list[str] = []
        visited.add(node_id)
        while queue and len(component) < 60:
            current = queue.pop(0)
            component.append(current)
            for neighbor in sorted(adjacency.get(current, set()), key=lambda item: degree.get(item, 0), reverse=True):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(component) < 3:
            continue
        cluster_nodes = [node_by_id[item] for item in component if item in node_by_id]
        anchor_nodes = sorted(cluster_nodes, key=lambda node: (degree.get(str(node.get("id")), 0), _graph_node_priority(str(node.get("type") or ""))), reverse=True)[:5]
        title = _workspace_graph_cluster_title(anchor_nodes)
        clusters.append(
            {
                "cluster_id": f"cluster:{len(clusters) + 1}",
                "title": title,
                "summary": _workspace_graph_cluster_summary(cluster_nodes, anchor_nodes),
                "node_count": len(cluster_nodes),
                "edge_count": sum(1 for edge in edges if edge.get("source") in component and edge.get("target") in component),
                "types": _graph_type_counts(cluster_nodes),
                "anchor_nodes": [_graph_insight_node(node, degree.get(str(node.get("id")), 0)) for node in anchor_nodes],
            }
        )
        if len(clusters) >= 8:
            break
    return clusters


def _workspace_graph_guided_tour(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    degree: dict[str, int],
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    tour: list[dict[str, Any]] = []
    digest_nodes = sorted([node for node in nodes if node.get("type") == "digest"], key=lambda node: degree.get(str(node.get("id")), 0), reverse=True)
    claim_nodes = sorted([node for node in nodes if node.get("type") == "claim"], key=lambda node: degree.get(str(node.get("id")), 0), reverse=True)
    fact_nodes = sorted([node for node in nodes if node.get("type") == "fact"], key=lambda node: degree.get(str(node.get("id")), 0), reverse=True)
    passage_nodes = sorted([node for node in nodes if node.get("type") == "passage"], key=lambda node: degree.get(str(node.get("id")), 0), reverse=True)
    if clusters:
        tour.append(
            {
                "title": "先看最大的主题簇",
                "reason": clusters[0].get("summary"),
                "node_ids": [node.get("id") for node in clusters[0].get("anchor_nodes", []) if isinstance(node, dict)],
            }
        )
    if digest_nodes:
        tour.append(
            {
                "title": "从 digest 开始理解这批资料",
                "reason": "Digest 节点把 claims、facts 和原文 passage 串成可读结论。",
                "node_ids": [str(digest_nodes[0].get("id"))],
            }
        )
    if fact_nodes:
        tour.append(
            {
                "title": "检查最核心的事实/关系",
                "reason": "Fact 节点是 HippoRAG-style 检索的语义跳点，应该能回溯到 claim 和 passage。",
                "node_ids": [str(fact_nodes[0].get("id"))],
            }
        )
    if claim_nodes and passage_nodes:
        evidence_targets = [
            str(edge.get("target"))
            for edge in edges
            if str(edge.get("source")) == str(passage_nodes[0].get("id")) or str(edge.get("target")) == str(passage_nodes[0].get("id"))
        ]
        tour.append(
            {
                "title": "验证证据链",
                "reason": "从 passage 到 claim/fact/digest 的边说明理解结果来自哪里。",
                "node_ids": [str(passage_nodes[0].get("id")), *[item for item in evidence_targets if item in node_by_id][:3]],
            }
        )
    return tour[:5]


def _workspace_graph_cluster_title(anchor_nodes: list[dict[str, Any]]) -> str:
    for node in anchor_nodes:
        if node.get("type") in {"digest", "fact", "claim", "entity", "phrase"} and node.get("label"):
            return str(node.get("label"))[:80]
    if anchor_nodes:
        return str(anchor_nodes[0].get("label") or anchor_nodes[0].get("id") or "Topic cluster")[:80]
    return "Topic cluster"


def _workspace_graph_cluster_summary(cluster_nodes: list[dict[str, Any]], anchor_nodes: list[dict[str, Any]]) -> str:
    type_counts = _graph_type_counts(cluster_nodes)
    anchors = [str(node.get("label") or node.get("id") or "") for node in anchor_nodes[:3] if node.get("label") or node.get("id")]
    layer_bits = ", ".join(f"{key} {value}" for key, value in sorted(type_counts.items()) if value)
    if anchors:
        return f"围绕 {' / '.join(anchors)} 展开，包含 {layer_bits}。"
    return f"包含 {layer_bits}。"


def _graph_type_counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes:
        node_type = str(node.get("type") or "unknown")
        counts[node_type] = counts.get(node_type, 0) + 1
    return counts


def _graph_node_priority(node_type: str) -> int:
    return {
        "digest": 9,
        "fact": 8,
        "claim": 7,
        "entity": 6,
        "phrase": 5,
        "passage": 4,
        "document": 3,
        "source": 2,
    }.get(node_type, 1)


def _graph_insight_node(node: dict[str, Any], degree: int) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "label": node.get("label"),
        "summary": node.get("summary"),
        "degree": degree,
    }


def _fact_statement(edge: Any, members: list[Any], entity_by_id: dict[str, Any]) -> str:
    relation = str(getattr(edge, "relation_type", "") or "").strip()
    labels = []
    for member in members[:4]:
        entity = entity_by_id.get(getattr(member, "entity_id", ""))
        label = str(getattr(entity, "label", "") or getattr(member, "entity_id", "") or "").strip()
        role = str(getattr(member, "role", "") or "").strip()
        if label and role:
            labels.append(f"{role}: {label}")
        elif label:
            labels.append(label)
    evidence = str(getattr(edge, "evidence_text", "") or "").strip()
    if labels and relation:
        return f"{relation} ({'; '.join(labels)})"
    if evidence:
        return evidence[:220]
    return relation


def _graph_agentic_contract() -> dict[str, Any]:
    return {
        "pattern": "hipporag_style_agentic_graphrag",
        "offline_agentic_required": ["knowledge_extraction", "digest"],
        "online_agentic_required": ["query_understanding", "graph_expansion_decisions", "fact_filtering", "answer_synthesis"],
        "online_loop": [
            "use deterministic lexical/vector passage seeds",
            "inspect entity/fact/claim graph seeds and paths",
            "decide whether previous/next passage windows are needed",
            "decide whether connected entity/fact/claim neighbors are needed",
            "issue follow-up PSKA searches when evidence is insufficient",
            "filter irrelevant or unsupported facts before final answer",
        ],
        "trace_keys": [
            "retrieval_plan",
            "query_understanding",
            "iterations",
            "expansion_decisions",
            "graph_paths_used",
            "fact_relevance_filter",
            "evidence_check",
            "gaps",
            "conflicts",
        ],
    }


def _graph_agentic_query(query: str, deterministic: dict[str, Any]) -> str:
    seed_payload = {
        "query": query,
        "deterministic_seeds": _compact_graph_agentic_seeds(deterministic),
        "agentic_contract": _graph_agentic_contract(),
    }
    return (
        "Synthesize a PSKA GraphRAG answer from already-retrieved deterministic seeds.\n"
        f"ACTUAL USER QUESTION: {query}\n\n"
        "The JSON payload below is the evidence package; its query field is the only user question. "
        "Do not substitute an unrelated query. Do not answer with a PSKA system/index/workspace overview. "
        "First try to answer from deterministic_seeds.supporting_passages, top_facts, graph_paths, citations, "
        "and source refs. Use PSKA search tools only when the seeds are clearly insufficient, and avoid redundant searches. "
        "If the seeds already answer the question, synthesize from those seeds without calling PSKA tools. "
        "Think like HippoRAG: use passage/entity/fact/claim seeds, decide whether to inspect adjacent "
        "passage windows or graph neighbors, run follow-up searches if needed, filter irrelevant facts, "
        "and return a cited answer. Return valid JSON with answer, retrieval, trace, source_refs, and citations. "
        "The answer must be Chinese, substantive, about 400-800 Chinese characters when evidence is available, "
        "and organized as: key conclusions, risks, next actions, and uncertainty. "
        "The answer must directly address the named entities and fields in ACTUAL USER QUESTION. "
        "Start answer with the user's substantive conclusion; do not open with GraphRAG/retrieval/graph-path status. "
        "Keep retrieval diagnostics, graph path counts, expansion decisions, and tool-status notes in trace, not in answer. "
        "Use citation/source_ref identifiers from the provided seeds wherever possible. "
        "If a PSKA tool call fails or times out, do not stop and do not return a tool-failure report as the answer: "
        "synthesize the best grounded answer from the provided seeds and record "
        "the tool failure in trace.gaps or trace.evidence_check. "
        "In trace include expansion_decisions explaining whether previous/next passage windows or graph "
        "neighbors were queried or intentionally skipped.\n\n"
        f"{json.dumps(seed_payload, ensure_ascii=False)}"
    )


def _graph_agentic_repair_query(query: str, deterministic: dict[str, Any], first_agentic: dict[str, Any]) -> str:
    seed_payload = {
        "query": query,
        "previous_agentic_answer": str(first_agentic.get("answer") or "")[:1200],
        "previous_trace": first_agentic.get("trace") if isinstance(first_agentic.get("trace"), dict) else {},
        "deterministic_seeds": _compact_graph_agentic_seeds(deterministic),
        "required_output": {
            "answer_language": "zh",
            "minimum_chinese_characters": 300,
            "target_chinese_characters": "500-900",
            "must_include": [
                "key conclusions grounded in citations",
                "risks or caveats if present",
                "next actions or review suggestions if present",
                "uncertainty / evidence gaps",
                "citation or source_ref identifiers from seeds",
            ],
        },
    }
    return (
        "Repair the previous PSKA GraphRAG answer. The prior answer was too short for the product QA gate. "
        "Do not run broad redundant searches unless the provided seeds are insufficient. Use the deterministic seeds below "
        "as grounded evidence and produce valid JSON with answer, retrieval, trace, source_refs, and citations. "
        "Do not substitute an unrelated query. If PSKA tools failed, answer from the deterministic seeds instead of "
        "returning a tool-failure report. "
        "The repaired answer must be Chinese, at least 300 Chinese characters, specific to the user's question, "
        "and organized into key conclusions, risks/caveats, next actions, and uncertainty. "
        "Start the repaired answer with the user's substantive conclusion; do not open with GraphRAG/retrieval/graph-path status. "
        "Keep retrieval diagnostics and graph path counts in trace, not in answer. "
        "If you cannot improve the answer, explain the evidence gap in trace.evidence_check but still synthesize "
        "the richest grounded answer possible from the seeds.\n\n"
        f"{json.dumps(seed_payload, ensure_ascii=False)}"
    )


def _compact_graph_agentic_seeds(deterministic: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_seeds": deterministic.get("query_seeds") or {},
        "path_summary": deterministic.get("path_summary") or {},
        "top_facts": [_compact_graph_fact(item) for item in _list_of_dicts(deterministic.get("top_facts"))[:8]],
        "supporting_passages": [_compact_supporting_passage(item) for item in _list_of_dicts(deterministic.get("supporting_passages"))[:8]],
        "citations": [_compact_citation(item) for item in _list_of_dicts(deterministic.get("citations"))[:8]],
        "graph_paths": [_compact_graph_path(item) for item in _list_of_dicts(deterministic.get("graph_paths"))[:6]],
        "filtered_out_facts": [_compact_graph_fact(item) for item in _list_of_dicts(deterministic.get("filtered_out_facts"))[:5]],
        "gaps": deterministic.get("gaps") or [],
        "conflicts": deterministic.get("conflicts") or [],
        "sensitivity": deterministic.get("sensitivity") or [],
    }


def _compact_supporting_passage(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "result_id": item.get("result_id"),
        "source_item_id": item.get("source_item_id"),
        "title": item.get("title"),
        "snippet": str(item.get("snippet") or "")[:700],
        "score": item.get("score"),
        "source": item.get("source"),
        "source_refs": item.get("source_refs") or [],
    }


def _compact_citation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_item_id": item.get("source_item_id"),
        "chunk_id": item.get("chunk_id"),
        "title": item.get("title"),
        "url": item.get("url"),
    }


def _compact_graph_fact(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "fact_id": item.get("fact_id") or item.get("hyperedge_id") or item.get("path_id"),
        "statement": item.get("statement") or item.get("summary") or item.get("explanation"),
        "relation_type": item.get("relation_type"),
        "evidence_text": str(item.get("evidence_text") or "")[:360],
        "source_refs": item.get("source_refs") or [],
        "why_it_matters": item.get("why_it_matters"),
        "relevance_status": item.get("relevance_status"),
        "relevance_score": item.get("relevance_score"),
        "filter_reason": item.get("filter_reason"),
    }


def _compact_graph_path(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path_id": item.get("path_id"),
        "depth": item.get("depth"),
        "explanation": item.get("explanation"),
        "score": item.get("score"),
        "entities": [
            {
                "entity_id": entity.get("entity_id"),
                "label": entity.get("label"),
                "entity_type": entity.get("entity_type"),
            }
            for entity in _list_of_dicts(item.get("entities"))[:6]
        ],
        "edges": [
            {
                "hyperedge_id": edge.get("hyperedge_id"),
                "relation_type": edge.get("relation_type"),
                "evidence_text": str(edge.get("evidence_text") or "")[:360],
                "confidence": edge.get("confidence"),
                "source_refs": edge.get("source_refs") or [],
            }
            for edge in _list_of_dicts(item.get("edges"))[:4]
        ],
    }


def _agentic_fact_filter_payload(agentic: dict[str, Any], deterministic: dict[str, Any]) -> dict[str, Any]:
    retrieval = agentic.get("retrieval") if isinstance(agentic.get("retrieval"), dict) else {}
    trace = agentic.get("trace") if isinstance(agentic.get("trace"), dict) else {}
    relevance = trace.get("fact_relevance_filter")
    if not isinstance(relevance, dict):
        relevance = retrieval.get("fact_relevance_filter") if isinstance(retrieval.get("fact_relevance_filter"), dict) else {}
    kept = _list_of_dicts(relevance.get("kept_facts")) or _list_of_dicts(retrieval.get("top_facts"))
    filtered = _list_of_dicts(relevance.get("filtered_out_facts")) or _list_of_dicts(retrieval.get("filtered_out_facts"))
    if not kept and not filtered:
        return {}
    path_summary = dict(deterministic.get("path_summary") if isinstance(deterministic.get("path_summary"), dict) else {})
    path_summary.update(
        {
            "kept_fact_count": len(kept),
            "filtered_fact_count": len(filtered),
            "filter_mode": "agentic_llm_relevance",
        }
    )
    return {
        "top_facts": kept,
        "filtered_out_facts": filtered,
        "path_summary": path_summary,
    }


def _graph_agentic_answer_payload(agentic: dict[str, Any], deterministic: dict[str, Any]) -> dict[str, Any]:
    agentic_answer = str(agentic.get("answer") or "").strip()
    deterministic_answer = str(deterministic.get("answer") or "").strip()
    if len(agentic_answer) >= 300 or not deterministic_answer:
        return {
            "answer": agentic_answer,
            "answer_mode": "agentic_synthesis",
            "agentic_answer": agentic_answer,
        }
    return {
        "answer": deterministic_answer,
        "answer_mode": "deterministic_synthesis_for_short_agentic",
        "agentic_answer": agentic_answer,
        "answer_warning": "Agentic answer was shorter than the product QA threshold; PSKA synthesized a grounded fallback from deterministic seeds.",
    }


def _agentic_graph_answer_too_short(agentic: dict[str, Any], deterministic: dict[str, Any]) -> bool:
    answer = str(agentic.get("answer") or "").strip()
    deterministic_answer = str(deterministic.get("answer") or "").strip()
    return bool(deterministic_answer) and 0 < len(answer) < 300


def _merge_graph_agentic_repair(first_agentic: dict[str, Any], repair_agentic: dict[str, Any]) -> dict[str, Any]:
    merged = dict(repair_agentic)
    first_trace = first_agentic.get("trace") if isinstance(first_agentic.get("trace"), dict) else {}
    repair_trace = repair_agentic.get("trace") if isinstance(repair_agentic.get("trace"), dict) else {}
    merged_trace = dict(repair_trace)
    merged_trace["repair"] = {
        "attempted": True,
        "accepted": True,
        "first_answer_chars": len(str(first_agentic.get("answer") or "")),
        "repaired_answer_chars": len(str(repair_agentic.get("answer") or "")),
        "first_trace": first_trace,
    }
    merged["trace"] = merged_trace
    merged["agentic_service"] = {
        **(first_agentic.get("agentic_service") if isinstance(first_agentic.get("agentic_service"), dict) else {}),
        **(repair_agentic.get("agentic_service") if isinstance(repair_agentic.get("agentic_service"), dict) else {}),
    }
    merged["source_refs"] = _merge_source_ref_dicts(
        first_agentic.get("source_refs") if isinstance(first_agentic.get("source_refs"), list) else [],
        repair_agentic.get("source_refs") if isinstance(repair_agentic.get("source_refs"), list) else [],
    )
    retrieval = repair_agentic.get("retrieval") if isinstance(repair_agentic.get("retrieval"), dict) else {}
    if first_agentic.get("retrieval") and isinstance(first_agentic.get("retrieval"), dict):
        retrieval = {**first_agentic["retrieval"], **retrieval}
    merged["retrieval"] = retrieval
    return merged


def _graph_agentic_repair_summary(repair_agentic: dict[str, Any], answer_payload: dict[str, Any]) -> dict[str, Any]:
    if repair_agentic.get("ok") is False:
        return {
            "attempted": True,
            "accepted": False,
            "error": repair_agentic.get("error"),
            "final_answer_mode": answer_payload.get("answer_mode"),
        }
    repaired_chars = len(str(repair_agentic.get("answer") or ""))
    accepted = answer_payload.get("answer_mode") == "agentic_synthesis" and repaired_chars >= 300
    return {
        "attempted": True,
        "accepted": accepted,
        "repaired_answer_chars": repaired_chars,
        "final_answer_mode": answer_payload.get("answer_mode"),
    }


def _merge_source_ref_dicts(*groups: list[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group:
            if not isinstance(ref, dict):
                continue
            key = json.dumps(ref, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(ref)
    return merged


def _agentic_graph_unusable_reason(agentic: dict[str, Any]) -> str | None:
    answer = str(agentic.get("answer") or "")
    trace = agentic.get("trace") if isinstance(agentic.get("trace"), dict) else {}
    haystack = " ".join(
        [
            answer,
            json.dumps(trace.get("events") or [], ensure_ascii=False)[:4000],
        ]
    ).lower()
    failure_markers = [
        "mcp tools are unavailable",
        "pska tools are unreachable",
        "mcp request timeout",
        "mcp tools timed out",
        "tools timed out",
        "retrieval failed",
        "no evidence retrieved",
        "mcp transport",
        "readuntil",
        "unable to complete pska search",
        "message also truncated",
        "full query truncated",
        "question was truncated",
        "query was truncated",
        "full query was not received",
        "question was not received",
        "pska knowledge base overview",
        "pska knowledge base is operational",
        "knowledge base is operational",
        "pska knowledge base currently contains",
        "pska knowledge base processed",
        "source items across benchmark workspace",
        "source documents. key themes",
        "extracted 151 entities",
        "entities, 52 hyperedges",
        "fastreact serves as pska",
        "pending review items spanning",
        "pska search tools are unavailable",
    ]
    for marker in failure_markers:
        if marker in haystack:
            return marker
    if not answer.strip() and not trace:
        return "empty_agentic_answer"
    return None


def _agentic_graph_query_mismatch_reason(query: str, agentic: dict[str, Any], deterministic: dict[str, Any]) -> str | None:
    deterministic_answer = str(deterministic.get("answer") or "").strip()
    if not deterministic_answer:
        return None
    answer = str(agentic.get("answer") or "").casefold()
    query_lower = query.casefold()
    required_phrase_groups = [
        (("负责人", "lead"), ("负责人", "lead")),
        (("下一步", "行动", "next step", "action"), ("下一步", "行动", "next step", "action")),
        (("状态", "status"), ("状态", "status")),
        (("arr",), ("arr",)),
    ]
    for query_terms, answer_terms in required_phrase_groups:
        if any(term in query_lower for term in query_terms) and not any(term in answer for term in answer_terms):
            return "agentic_answer_missed_query_fields"
    anchors = [
        term.casefold()
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[0-9][0-9,.]*", query)
        if term.casefold() not in {"the", "and", "for", "with", "from", "what", "who", "why", "how", "please"}
    ]
    field_anchors = [
        anchor
        for anchor in anchors
        if anchor in {"arr", "pipeline", "status", "next", "step", "action", "lead", "owner", "负责人"}
    ]
    if field_anchors and not any(anchor in answer for anchor in field_anchors):
        return "agentic_answer_missed_query_fields"
    if anchors and not any(anchor in answer for anchor in anchors):
        return "agentic_answer_missed_query_anchors"
    return None


def _graph_seed_answer(
    query: str,
    supporting_passages: list[dict[str, Any]],
    top_facts: list[dict[str, Any]],
    graph_paths: list[dict[str, Any]],
    filtered_out_facts: list[dict[str, Any]],
) -> str:
    if not supporting_passages and not top_facts and not graph_paths:
        return ""
    table_answer = _graph_pipeline_table_answer(query, supporting_passages)
    title = table_answer["company"] if table_answer else _graph_answer_topic(query, supporting_passages)
    conclusions = table_answer["answer"] if table_answer else _graph_answer_conclusions(supporting_passages, top_facts)
    risks = _graph_answer_risks(supporting_passages, filtered_out_facts)
    actions = _graph_table_next_action(table_answer) if table_answer else _graph_answer_actions(supporting_passages)
    uncertainty = _graph_answer_uncertainty(top_facts, graph_paths, filtered_out_facts)
    citations = _graph_answer_citations(supporting_passages)
    return (
        f"关键结论：关于“{title}”，{conclusions}\n\n"
        f"风险与约束：{risks}\n\n"
        f"后续行动：{actions}\n\n"
        f"不确定性：{uncertainty}\n\n"
        f"引用线索：{citations}"
    )


def _graph_answer_topic(query: str, passages: list[dict[str, Any]]) -> str:
    for passage in passages:
        title = str(passage.get("title") or "").strip()
        if title:
            return title
    return query[:80]


def _graph_pipeline_table_answer(query: str, passages: list[dict[str, Any]]) -> dict[str, str] | None:
    if not _graph_pipeline_query_intent(query):
        return None
    query_phrases = _graph_capitalized_phrases(query)
    query_terms = {term.casefold() for term in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(term) > 1}
    for passage in passages:
        rows = _graph_markdown_table_rows(str(passage.get("snippet") or ""))
        for row in rows:
            company = str(row.get("Company") or "").strip()
            if not company:
                continue
            company_lower = company.casefold()
            phrase_match = any(phrase.casefold() in company_lower for phrase in query_phrases)
            term_match = {"acme", "widget", "fund"}.intersection(query_terms) and any(term in company_lower for term in query_terms)
            if not phrase_match and not term_match:
                continue
            lead = str(row.get("Lead") or "未知").strip()
            status = str(row.get("Status") or "未知").strip()
            arr = str(row.get("ARR") or "未知").strip()
            next_step = str(row.get("Next Step") or row.get("Action") or "未记录").strip()
            title = str(passage.get("title") or passage.get("source_item_id") or "supporting passage").strip()
            return {
                "company": company,
                "lead": lead,
                "status": status,
                "arr": arr,
                "next_step": next_step,
                "title": title,
                "answer": (
                    f"{company} 当前 pipeline 记录的负责人是 {lead}，状态是 {status}，ARR 是 {arr}，"
                    f"下一步行动是：{next_step}。证据来自 {title} 的表格行。"
                ),
            }
    return None


def _graph_pipeline_query_intent(query: str) -> bool:
    lower = query.casefold()
    return any(term in lower for term in ("pipeline", "next step", "下一步", "行动", "负责人", "状态", "arr", "lead"))


def _graph_capitalized_phrases(query: str) -> list[str]:
    return [match.strip() for match in re.findall(r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)+\b", query)]


def _graph_markdown_table_rows(text: str) -> list[dict[str, str]]:
    headers = ["Company", "Lead", "Status", "ARR", "Next Step"]
    cells = [cell.strip() for cell in text.replace("\n", " ").split("|")]
    rows: list[dict[str, str]] = []
    for index in range(0, max(0, len(cells) - len(headers) + 1)):
        if cells[index : index + len(headers)] != headers:
            continue
        row_cells: list[str] = []
        for cell in cells[index + len(headers) :]:
            compact = cell.replace(" ", "")
            if not cell or (compact and set(compact) == {"-"}):
                continue
            row_cells.append(cell)
            if len(row_cells) == len(headers):
                rows.append(dict(zip(headers, row_cells)))
                row_cells = []
        break
    return rows


def _graph_table_next_action(table_answer: dict[str, str]) -> str:
    return (
        f"直接推进表格中的 Next Step：{table_answer['next_step']}。"
        "完成后建议更新 pipeline 状态，并保留该表格行作为 citation。"
    )


def _graph_answer_conclusions(passages: list[dict[str, Any]], facts: list[dict[str, Any]]) -> str:
    fact_bits = [
        str(fact.get("statement") or fact.get("evidence_text") or "").strip()
        for fact in facts[:3]
        if str(fact.get("statement") or fact.get("evidence_text") or "").strip()
    ]
    passage_bits = [
        _clean_graph_snippet(passage.get("snippet"))
        for passage in passages[:3]
        if _clean_graph_snippet(passage.get("snippet"))
    ]
    bits = [*fact_bits, *passage_bits]
    if not bits:
        return "当前证据显示该问题已有相关资料命中，但可读事实不足，需要进一步 digest 或人工补充。"
    return "；".join(bits[:5]) + "。"


def _graph_answer_risks(passages: list[dict[str, Any]], filtered_facts: list[dict[str, Any]]) -> str:
    text = " ".join(_clean_graph_snippet(passage.get("snippet")) for passage in passages[:6])
    risk_terms = ["风险", "问题", "限制", "成本", "失败", "幻觉", "冲突", "不确定", "人工", "验证", "追溯"]
    matched = [term for term in risk_terms if term in text]
    if filtered_facts:
        return f"有 {len(filtered_facts)} 条 fact 被相关性过滤或降权，说明部分图谱关系可能只提供背景，不能直接支撑结论；同时需关注证据中的{ '、'.join(matched[:4]) if matched else '置信度、追溯和人工验证' }。"
    return f"主要风险在于证据需要持续校验，尤其是{ '、'.join(matched[:4]) if matched else '版本变化、引用完整性和人工复核' }。"


def _graph_answer_actions(passages: list[dict[str, Any]]) -> str:
    text = " ".join(_clean_graph_snippet(passage.get("snippet")) for passage in passages[:8])
    action_terms = ["建立", "实现", "设计", "准备", "评估", "记录", "追溯", "优化", "校验", "迭代", "测试"]
    matched = [term for term in action_terms if term in text]
    if matched:
        return f"建议把资料中出现的“{ '、'.join(matched[:5]) }”转成可跟踪任务：先确认目标和验收指标，再补齐证据引用，最后把需要人工判断的候选放入 review。"
    return "建议先把命中的 passages 和 facts 做人工 review，确认哪些值得进入长期 memory/profile/graph，再对缺证据的结论补充来源。"


def _graph_answer_uncertainty(facts: list[dict[str, Any]], graph_paths: list[dict[str, Any]], filtered_facts: list[dict[str, Any]]) -> str:
    review_facts = [fact for fact in facts if fact.get("relevance_status") == "review"]
    parts = []
    if review_facts:
        parts.append(f"{len(review_facts)} 条 fact 处于 review 相关性状态")
    if filtered_facts:
        parts.append(f"{len(filtered_facts)} 条 fact 被过滤")
    if not parts:
        if graph_paths:
            parts.append("证据来自当前命中的材料与关系线索，仍需复核原始来源是否为最新版本")
        else:
            parts.append("当前主要依赖命中的材料片段，仍需复核原始来源是否为最新版本")
    return "；".join(parts) + "。因此答案应被视为 grounded draft，而不是最终事实裁决。"


def _graph_answer_citations(passages: list[dict[str, Any]]) -> str:
    refs = []
    for passage in passages[:6]:
        refs.append(str(passage.get("result_id") or passage.get("source_item_id") or passage.get("title") or "").strip())
    refs = [ref for ref in refs if ref]
    return "、".join(refs) if refs else "暂无可显示 citation id。"


def _clean_graph_snippet(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:180]


def _graph_path_product_payload(query: str, deterministic: dict[str, Any]) -> dict[str, Any]:
    results = _list_of_dicts(deterministic.get("results"))
    citations = _list_of_dicts(deterministic.get("citations"))
    graph_paths = _list_of_dicts(deterministic.get("graph_paths"))
    score_debug = deterministic.get("score_debug") if isinstance(deterministic.get("score_debug"), dict) else {}
    diagnostics = score_debug.get("diagnostics") if isinstance(score_debug.get("diagnostics"), dict) else {}
    query_terms = _graph_query_terms(query)
    supporting_passages = _graph_supporting_passages(results, citations)
    ranked_facts = _graph_filter_facts(_graph_top_facts(graph_paths, score_debug), query_terms)
    top_facts = [fact for fact in ranked_facts if fact.get("relevance_status") != "dropped"]
    filtered_out_facts = [fact for fact in ranked_facts if fact.get("relevance_status") == "dropped"]
    return {
        "query_seeds": {
            "terms": query_terms,
            "passages": [
                {
                    "result_id": passage.get("result_id"),
                    "source_item_id": passage.get("source_item_id"),
                    "title": passage.get("title"),
                    "score": passage.get("score"),
                    "source": passage.get("source"),
                }
                for passage in supporting_passages[:5]
            ],
            "facts": [
                {
                    "fact_id": fact.get("fact_id") or fact.get("hyperedge_id") or fact.get("path_id"),
                    "statement": fact.get("statement") or fact.get("summary") or fact.get("explanation"),
                    "score": fact.get("score"),
                }
                for fact in top_facts[:5]
            ],
            "graph_path_count": len(graph_paths),
        },
        "top_facts": top_facts,
        "supporting_passages": supporting_passages,
        "filtered_out_facts": filtered_out_facts,
        "answer": _graph_seed_answer(query, supporting_passages, top_facts, graph_paths, filtered_out_facts),
        "answer_mode": "deterministic_synthesis",
        "path_summary": {
            "summary": _graph_path_summary(supporting_passages, top_facts, graph_paths),
            "result_count": len(results),
            "citation_count": len(citations),
            "graph_path_count": len(graph_paths),
            "kept_fact_count": len(top_facts),
            "filtered_fact_count": len(filtered_out_facts),
            "filter_mode": "deterministic_relevance",
            "has_graph_signal": bool(top_facts or graph_paths or score_debug.get("graph_context_used")),
            "fallback": "ordinary_rag" if not (top_facts or graph_paths or score_debug.get("graph_context_used")) else None,
            "diagnostics": diagnostics,
        },
    }


def _graph_query_terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for match in re.findall(r"[\w\u4e00-\u9fff]+", query.lower()):
        if len(match) < 2 or match in seen:
            continue
        seen.add(match)
        terms.append(match)
        if len(terms) >= 12:
            break
    return terms


def _graph_supporting_passages(results: list[dict[str, Any]], citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citation_by_source = {
        str(item.get("source_item_id")): item
        for item in citations
        if item.get("source_item_id")
    }
    passages: list[dict[str, Any]] = []
    for item in results[:8]:
        citation = citation_by_source.get(str(item.get("source_item_id"))) or {}
        passages.append(
            {
                "result_id": item.get("result_id"),
                "source_item_id": item.get("source_item_id"),
                "title": item.get("title") or citation.get("title"),
                "snippet": item.get("snippet") or citation.get("snippet"),
                "score": item.get("score"),
                "source": item.get("source"),
                "source_refs": [
                    {
                        "source_item_id": item.get("source_item_id") or citation.get("source_item_id"),
                        "chunk_id": item.get("result_id") or citation.get("chunk_id"),
                        "url": citation.get("url"),
                    }
                ],
                "score_debug": item.get("score_debug") if isinstance(item.get("score_debug"), dict) else {},
            }
        )
    return passages


def _graph_top_facts(graph_paths: list[dict[str, Any]], score_debug: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in graph_paths[:8]:
        path_id = str(path.get("path_id") or "")
        edges = _list_of_dicts(path.get("edges"))
        for edge in edges:
            fact_id = str(edge.get("hyperedge_id") or edge.get("fact_id") or edge.get("id") or path_id)
            if not fact_id or fact_id in seen:
                continue
            seen.add(fact_id)
            facts.append(
                {
                    "fact_id": fact_id,
                    "path_id": path_id,
                    "statement": edge.get("summary") or edge.get("label") or path.get("explanation"),
                    "relation_type": edge.get("relation_type"),
                    "score": path.get("score"),
                    "evidence_text": edge.get("evidence_text"),
                    "source_refs": edge.get("source_refs") or edge.get("evidence_citations") or [],
                    "why_it_matters": edge.get("why_it_matters") or path.get("explanation"),
                }
            )
    diagnostics = score_debug.get("diagnostics") if isinstance(score_debug.get("diagnostics"), dict) else {}
    offline_facts = diagnostics.get("top_facts") if isinstance(diagnostics.get("top_facts"), list) else []
    for index, item in enumerate(offline_facts):
        fact_id = f"offline_fact_seed:{index}"
        if fact_id in seen:
            continue
        facts.append(
            {
                "fact_id": fact_id,
                "statement": item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
                "score": None,
                "source_refs": [],
                "why_it_matters": "Matched by HippoRAG-style offline fact seeding.",
            }
        )
    return facts[:8]


def _graph_filter_facts(facts: list[dict[str, Any]], query_terms: list[str]) -> list[dict[str, Any]]:
    if not facts:
        return []
    if not query_terms:
        return [
            {
                **fact,
                "relevance_status": "review",
                "relevance_score": 0.0,
                "filter_reason": "no_query_terms",
            }
            for fact in facts
        ]
    ranked: list[dict[str, Any]] = []
    normalized_terms = [term.lower() for term in query_terms if term]
    for fact in facts:
        text = " ".join(
            str(fact.get(key) or "")
            for key in ("statement", "summary", "explanation", "evidence_text", "relation_type", "why_it_matters")
        ).lower()
        matches = sorted({term for term in normalized_terms if term in text})
        has_evidence = bool(fact.get("evidence_text") or fact.get("source_refs"))
        lexical_score = len(matches) / max(len(normalized_terms), 1)
        path_score = float(fact.get("score") or 0.0)
        relevance_score = min(1.0, lexical_score + min(path_score, 1.0) * 0.25 + (0.15 if has_evidence else 0.0))
        if matches or relevance_score >= 0.35:
            status = "kept"
            reason = "matched_query_terms" if matches else "high_graph_score"
        elif has_evidence and path_score > 0:
            status = "review"
            reason = "graph_supported_but_weak_lexical_match"
        else:
            status = "dropped"
            reason = "weak_query_match_and_no_evidence"
        ranked.append(
            {
                **fact,
                "relevance_status": status,
                "relevance_score": round(relevance_score, 4),
                "matched_terms": matches,
                "filter_reason": reason,
            }
        )
    return sorted(ranked, key=lambda item: (item.get("relevance_status") == "dropped", -float(item.get("relevance_score") or 0.0)))


def _graph_path_summary(
    supporting_passages: list[dict[str, Any]],
    top_facts: list[dict[str, Any]],
    graph_paths: list[dict[str, Any]],
) -> str:
    if graph_paths and top_facts:
        return f"GraphRAG found {len(supporting_passages)} supporting passages and {len(top_facts)} fact/path candidates."
    if supporting_passages:
        return f"No strong graph path was found; using {len(supporting_passages)} lexical/vector passage seeds as ordinary RAG evidence."
    return "No supporting passage or graph fact was found for this query."


def _source_refs_payload(source_refs: Any) -> list[dict[str, Any]]:
    return to_jsonable(source_refs if isinstance(source_refs, list) else list(source_refs or []))


def _source_item_knowledge_base_lineage(
    store: Any,
    source_item_id: str,
    *,
    tenant_id: str,
    owner_user_id: str,
    selected_knowledge_base_ids: list[str] | set[str] | None = None,
    active_only: bool = True,
) -> dict[str, Any]:
    source_item_id = str(source_item_id or "").strip()
    if not source_item_id or not hasattr(store, "list_knowledge_base_ids_for_source_item"):
        return {}
    selected_ids = set(selected_knowledge_base_ids or [])
    all_ids = sorted(
        store.list_knowledge_base_ids_for_source_item(
            source_item_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            active_only=active_only,
        )
    )
    knowledge_base_ids = [knowledge_base_id for knowledge_base_id in all_ids if not selected_ids or knowledge_base_id in selected_ids] or all_ids
    knowledge_base_names: list[str] = []
    for knowledge_base_id in knowledge_base_ids:
        try:
            knowledge_base = store.get_knowledge_base(knowledge_base_id, tenant_id=tenant_id, owner_user_id=owner_user_id)
        except KeyError:
            continue
        name = str(getattr(knowledge_base, "name", "") or getattr(knowledge_base, "slug", "") or knowledge_base_id).strip()
        if name:
            knowledge_base_names.append(name)
    knowledge_base_names = list(dict.fromkeys(knowledge_base_names))
    lineage: dict[str, Any] = {
        "knowledge_base_ids": knowledge_base_ids,
        "knowledge_base_names": knowledge_base_names,
    }
    if len(knowledge_base_ids) == 1:
        lineage["knowledge_base_id"] = knowledge_base_ids[0]
    if len(knowledge_base_names) == 1:
        lineage["knowledge_base_name"] = knowledge_base_names[0]
    return lineage


def _enrich_source_refs_knowledge_bases(
    store: Any,
    source_refs: list[dict[str, Any]],
    *,
    tenant_id: str,
    owner_user_id: str,
    selected_knowledge_base_ids: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    lineage_cache: dict[str, dict[str, Any]] = {}
    enriched_refs: list[dict[str, Any]] = []
    for ref in _list_of_dicts(source_refs):
        next_ref = dict(ref)
        source_item_id = str(next_ref.get("source_item_id") or "").strip()
        if source_item_id:
            if source_item_id not in lineage_cache:
                lineage_cache[source_item_id] = _source_item_knowledge_base_lineage(
                    store,
                    source_item_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    selected_knowledge_base_ids=selected_knowledge_base_ids,
                )
            lineage = lineage_cache[source_item_id]
            if lineage.get("knowledge_base_ids"):
                next_ref.update(lineage)
        enriched_refs.append(next_ref)
    return enriched_refs


def _knowledge_base_lineage_from_source_refs(source_refs: list[dict[str, Any]]) -> dict[str, Any]:
    knowledge_base_ids: list[str] = []
    knowledge_base_names: list[str] = []
    for ref in _list_of_dicts(source_refs):
        ref_ids = _string_list(ref.get("knowledge_base_ids")) or _string_list(ref.get("knowledge_base_id"))
        ref_names = _string_list(ref.get("knowledge_base_names")) or _string_list(ref.get("knowledge_base_name"))
        knowledge_base_ids.extend(ref_ids)
        knowledge_base_names.extend(ref_names)
    knowledge_base_ids = list(dict.fromkeys(knowledge_base_ids))
    knowledge_base_names = list(dict.fromkeys(knowledge_base_names))
    lineage: dict[str, Any] = {}
    if knowledge_base_ids:
        lineage["knowledge_base_ids"] = knowledge_base_ids
    if knowledge_base_names:
        lineage["knowledge_base_names"] = knowledge_base_names
    if len(knowledge_base_ids) == 1:
        lineage["knowledge_base_id"] = knowledge_base_ids[0]
    if len(knowledge_base_names) == 1:
        lineage["knowledge_base_name"] = knowledge_base_names[0]
    return lineage


def _enrich_understanding_artifacts_knowledge_bases(
    store: Any,
    items: list[dict[str, Any]],
    *,
    tenant_id: str,
    owner_user_id: str,
    selected_knowledge_base_ids: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    enriched_items: list[dict[str, Any]] = []
    nested_ref_fields = ("key_points", "actions", "open_questions", "risks", "memory_suggestions", "relationship_suggestions")
    for item in _list_of_dicts(items):
        enriched = dict(item)
        refs = _enrich_source_refs_knowledge_bases(
            store,
            _source_refs_payload(enriched.get("source_refs")),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=selected_knowledge_base_ids,
        )
        enriched["source_refs"] = refs
        lineage = _knowledge_base_lineage_from_source_refs(refs)
        if lineage:
            enriched.update(lineage)
            metadata = dict(enriched.get("metadata") or {}) if isinstance(enriched.get("metadata"), dict) else {}
            metadata.update(lineage)
            enriched["metadata"] = metadata
        for field in nested_ref_fields:
            nested_values: list[dict[str, Any]] = []
            changed = False
            for nested in _list_of_dicts(enriched.get(field)):
                nested_item = dict(nested)
                nested_refs = _source_refs_payload(nested_item.get("source_refs"))
                if nested_refs:
                    nested_item["source_refs"] = _enrich_source_refs_knowledge_bases(
                        store,
                        nested_refs,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        selected_knowledge_base_ids=selected_knowledge_base_ids,
                    )
                    changed = True
                nested_values.append(nested_item)
            if changed:
                enriched[field] = nested_values
        enriched_items.append(enriched)
    return enriched_items


def _enrich_review_candidate_payloads_knowledge_bases(
    store: Any,
    items: list[dict[str, Any]],
    *,
    tenant_id: str,
    owner_user_id: str,
    selected_knowledge_base_ids: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    enriched_items: list[dict[str, Any]] = []
    for item in _list_of_dicts(items):
        enriched = dict(item)
        proposal = dict(enriched.get("proposal") or {}) if isinstance(enriched.get("proposal"), dict) else {}
        refs = _source_refs_payload(proposal.get("source_refs") or proposal.get("sourceRefs") or enriched.get("source_refs"))
        refs = _enrich_source_refs_knowledge_bases(
            store,
            refs,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=selected_knowledge_base_ids,
        )
        if refs:
            proposal["source_refs"] = refs
            enriched["proposal"] = proposal
            enriched["source_refs"] = refs
            enriched.update(_knowledge_base_lineage_from_source_refs(refs))
        enriched_items.append(enriched)
    return enriched_items


def _enrich_search_retrieval_knowledge_bases(
    store: Any,
    retrieval: dict[str, Any],
    *,
    tenant_id: str,
    owner_user_id: str,
    selected_knowledge_base_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    enriched = dict(retrieval or {})
    selected_ids = list(dict.fromkeys(str(item) for item in (selected_knowledge_base_ids or []) if item))

    def enrich_refs(values: Any) -> list[dict[str, Any]]:
        return _enrich_source_refs_knowledge_bases(
            store,
            _list_of_dicts(values),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            selected_knowledge_base_ids=selected_ids,
        )

    results: list[dict[str, Any]] = []
    for result in _list_of_dicts(enriched.get("results")):
        result_payload = dict(result)
        source_item_id = str(result_payload.get("source_item_id") or "").strip()
        result_refs = enrich_refs([{"source_item_id": source_item_id}]) if source_item_id else []
        if result_refs:
            result_payload.update(_knowledge_base_lineage_from_source_refs(result_refs))
        citation = result_payload.get("citation") if isinstance(result_payload.get("citation"), dict) else {}
        if citation:
            enriched_citations = enrich_refs([citation])
            result_payload["citation"] = enriched_citations[0] if enriched_citations else citation
            result_payload.update(_knowledge_base_lineage_from_source_refs(enriched_citations))
        results.append(result_payload)
    citations = enrich_refs(enriched.get("citations"))
    enriched["results"] = results
    enriched["citations"] = citations
    enriched.update(_knowledge_base_lineage_from_source_refs([*citations, *[ref for result in results for ref in _list_of_dicts([result.get("citation")])]]))
    diagnostics = dict(enriched.get("diagnostics") or {}) if isinstance(enriched.get("diagnostics"), dict) else {}
    score_debug = dict(diagnostics.get("score_debug") or {}) if isinstance(diagnostics.get("score_debug"), dict) else {}
    score_debug["knowledge_base_scope"] = {
        "knowledge_base_ids": _string_list(enriched.get("knowledge_base_ids")) or selected_ids,
        "citation_count": len(citations),
        "result_count": len(results),
    }
    diagnostics["score_debug"] = score_debug
    enriched["diagnostics"] = diagnostics
    return enriched


def _add_source_ref_edges(add_edge, source_refs: list[dict[str, Any]], target: str, edge_type: str, passage_by_document: dict[str, PassageWindow], passage_by_source: dict[str, PassageWindow]) -> None:
    for ref in source_refs:
        passage_window_id = ref.get("passage_window_id")
        if passage_window_id:
            add_edge(f"passage:{passage_window_id}", target, edge_type)
            continue
        document_id = ref.get("document_id")
        if document_id and document_id in passage_by_document:
            add_edge(f"passage:{passage_by_document[document_id].passage_window_id}", target, edge_type)
            continue
        source_item_id = ref.get("source_item_id")
        if source_item_id and source_item_id in passage_by_source:
            add_edge(f"passage:{passage_by_source[source_item_id].passage_window_id}", target, edge_type)
        elif source_item_id:
            add_edge(f"source:{source_item_id}", target, edge_type)


def _source_refs_overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    left_keys = {_source_ref_key(ref) for ref in left}
    return any(_source_ref_key(ref) in left_keys for ref in right)


def _source_ref_key(ref: dict[str, Any]) -> tuple[str, str]:
    for key in ("passage_window_id", "chunk_id", "document_id", "source_item_id", "message_id"):
        if ref.get(key):
            return key, str(ref[key])
    return "", ""


def _workspace_entity(entity: Any) -> dict[str, Any]:
    return {
        "entity_id": getattr(entity, "entity_id", ""),
        "entity_type": getattr(entity, "entity_type", ""),
        "label": getattr(entity, "label", ""),
        "metadata": getattr(entity, "metadata", {}) or {},
    }


def _workspace_hyperedge(edge: Any, members: list[Any], entity_by_id: dict[str, Any]) -> dict[str, Any]:
    member_summaries = []
    for member in sorted(members, key=lambda item: getattr(item, "ordinal", 0)):
        entity = entity_by_id.get(getattr(member, "entity_id", ""))
        member_summaries.append(
            {
                "entity_id": getattr(member, "entity_id", ""),
                "label": getattr(entity, "label", getattr(member, "entity_id", "")),
                "entity_type": getattr(entity, "entity_type", ""),
                "role": getattr(member, "role", ""),
            }
        )
    return {
        "hyperedge_id": getattr(edge, "hyperedge_id", ""),
        "relation_type": getattr(edge, "relation_type", ""),
        "directionality": getattr(getattr(edge, "directionality", ""), "value", str(getattr(edge, "directionality", ""))),
        "confidence": getattr(edge, "confidence", 0.0),
        "evidence_text": getattr(edge, "evidence_text", ""),
        "source_refs": _console_source_ref_summaries(getattr(edge, "source_refs", [])),
        "members": member_summaries,
    }


def _today_continue_item(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or item.get("source_item_id") or "Untitled source")
    source_channel = str(item.get("source_channel") or "source")
    record_type = str(item.get("record_type") or "record")
    return {
        "id": item.get("source_item_id") or title,
        "type": "source",
        "title": title,
        "subtitle": " / ".join(part for part in [source_channel, record_type] if part),
        "summary": "最近进入 PSKA 的资料，可作为继续工作的入口。",
        "opened_surface": "document",
        "created_at": item.get("created_at"),
        "source_refs": [{"source_item_id": item.get("source_item_id")}]
        if item.get("source_item_id")
        else [],
    }


def _workspace_activity_item(event: WorkspaceActivityEvent) -> dict[str, Any]:
    return {
        "activity_id": event.workspace_activity_event_id,
        "activity_type": event.activity_type,
        "owner_user_id": event.owner_user_id,
        "actor_user_id": event.actor_user_id,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "surface": event.surface,
        "title": event.title or _workspace_activity_default_title(event.surface, event.target_id),
        "summary": event.summary,
        "metadata": to_jsonable(event.metadata),
        "created_at": event.created_at.isoformat(),
    }


def _workspace_continue_working(activity_items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    by_target: dict[tuple[str, str], dict[str, Any]] = {}
    pinned_keys: set[tuple[str, str]] = set()
    for item in activity_items:
        key = (str(item.get("target_type") or ""), str(item.get("target_id") or ""))
        if item.get("activity_type") == "pinned":
            pinned_keys.add(key)
        if key not in by_target:
            by_target[key] = dict(item)
            continue
        existing = by_target[key]
        if item.get("activity_type") == "pinned" and existing.get("activity_type") != "pinned":
            by_target[key] = dict(item)
    result = []
    for key, item in by_target.items():
        item = dict(item)
        item["pinned"] = key in pinned_keys
        result.append(item)
    result.sort(key=lambda item: (1 if item.get("pinned") else 0, str(item.get("created_at") or "")), reverse=True)
    return result[: max(0, limit)]


def _today_continue_item_from_activity(item: dict[str, Any]) -> dict[str, Any]:
    surface = str(item.get("surface") or "document")
    return {
        "id": item.get("target_id") or item.get("activity_id"),
        "type": item.get("target_type") or "workspace_activity",
        "title": item.get("title") or _workspace_activity_default_title(surface, str(item.get("target_id") or "")),
        "subtitle": " / ".join(
            part
            for part in [
                _workspace_activity_label(str(item.get("activity_type") or "")),
                surface,
                "pinned" if item.get("pinned") else "",
            ]
            if part
        ),
        "summary": item.get("summary") or "最近在工作区发生的真实活动。",
        "opened_surface": surface if surface in {"document", "canvas", "review"} else "document",
        "created_at": item.get("created_at"),
        "activity_type": item.get("activity_type"),
        "target_type": item.get("target_type"),
        "target_id": item.get("target_id"),
        "pinned": bool(item.get("pinned")),
    }


def _workspace_activity_default_title(surface: str, target_id: str) -> str:
    labels = {
        "today": "Today",
        "document": "文档工作区",
        "canvas": "画布工作区",
        "review": "Review Center",
    }
    return labels.get(surface, target_id or "Workspace")


def _workspace_activity_label(activity_type: str) -> str:
    labels = {
        "opened": "打开",
        "edited": "编辑",
        "viewed": "查看",
        "pinned": "置顶",
    }
    return labels.get(activity_type, activity_type)


def _discovery_item_payload(item) -> dict[str, Any]:
    evidence = to_jsonable(item.evidence)
    discovery_type = str(item.discovery_type)
    return {
        "id": item.discovery_id,
        "type": discovery_type,
        "title": item.title,
        "evidence": evidence,
        "confidence": item.confidence,
        "discovery_score": getattr(item, "discovery_score", 0.0),
        "quality_signals": to_jsonable(getattr(item, "quality_signals", {})),
        "fingerprint": getattr(item, "fingerprint", ""),
        "evidence_snapshot": to_jsonable(getattr(item, "evidence_snapshot", evidence)),
        "producer": item.producer,
        "created_at": item.created_at.isoformat(),
        "status": item.status,
        "label": _discovery_type_label(discovery_type),
        "summary": _discovery_summary(discovery_type, evidence),
        "evidence_count": len(evidence),
        "review_item_id": _discovery_review_item_id(evidence),
    }


def _linked_review_item(store, discovery_item):
    review_item_id = _discovery_review_item_id(to_jsonable(discovery_item.evidence))
    if not review_item_id:
        return None
    try:
        return store.get_review_item(review_item_id)
    except KeyError:
        return None


def _discovery_review_item_id(evidence: list[dict[str, Any]]) -> str | None:
    for item in evidence:
        if isinstance(item, dict) and item.get("review_item_id"):
            return str(item["review_item_id"])
    return None


def _discovery_type_label(discovery_type: str) -> str:
    labels = {
        "relationship": "关系发现",
        "conflict": "冲突发现",
        "memory": "记忆发现",
        "topic": "主题发现",
    }
    return labels.get(discovery_type, "发现")


def _discovery_summary(discovery_type: str, evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "Discovery producer 生成了一个待检查发现。"
    first = evidence[0]
    if discovery_type == "topic":
        return f"来自 {first.get('source_channel') or 'source'} 的新主题线索。"
    if first.get("review_item_id"):
        return f"由 review candidate {first.get('review_item_id')} 生成，等待检查。"
    return "Discovery producer 生成了一个待检查发现。"


def _today_discoveries(review_items: list[dict[str, Any]], hyperedges: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    discoveries: list[dict[str, Any]] = []
    discovery_review_types = {
        "relationship_candidate",
        "conflict",
        "memory_candidate",
        "profile_update",
        "action_candidate",
        "low_confidence",
    }
    for item in review_items:
        review_type = str(item.get("review_type") or "")
        if review_type not in discovery_review_types:
            continue
        discoveries.append(_today_discovery_from_review(item))
        if len(discoveries) >= limit:
            return discoveries
    for edge in hyperedges:
        discoveries.append(_today_discovery_from_hyperedge(edge))
        if len(discoveries) >= limit:
            break
    return discoveries


def _today_discovery_from_review(item: dict[str, Any]) -> dict[str, Any]:
    review_type = str(item.get("review_type") or "review_item")
    label_by_type = {
        "relationship_candidate": "发现关联",
        "conflict": "发现冲突",
        "memory_candidate": "记忆候选",
        "profile_update": "画像候选",
        "action_candidate": "行动候选",
        "low_confidence": "低置信候选",
    }
    source_refs = item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
    return {
        "id": item.get("review_item_id") or str(item.get("title") or review_type),
        "type": review_type,
        "label": label_by_type.get(review_type, "发现"),
        "title": item.get("title") or review_type,
        "summary": _today_review_summary(item),
        "confidence": item.get("confidence"),
        "evidence_count": len(source_refs),
        "review_item_id": item.get("review_item_id"),
        "source_ref_status": item.get("source_ref_status"),
        "recommended_action": item.get("recommended_action"),
    }


def _today_discovery_from_hyperedge(edge: dict[str, Any]) -> dict[str, Any]:
    members = edge.get("members") if isinstance(edge.get("members"), list) else []
    member_labels = [str(member.get("label") or "") for member in members if isinstance(member, dict) and member.get("label")]
    source_refs = edge.get("source_refs") if isinstance(edge.get("source_refs"), list) else []
    relation = str(edge.get("relation_type") or "知识关系")
    return {
        "id": edge.get("hyperedge_id") or relation,
        "type": "hyperedge",
        "label": "已有关系",
        "title": " ↔ ".join(member_labels[:3]) or relation,
        "summary": str(edge.get("evidence_text") or relation)[:240],
        "confidence": edge.get("confidence"),
        "evidence_count": len(source_refs),
        "source_ref_status": "present" if source_refs else "missing",
    }


def _today_review_item(item: dict[str, Any]) -> dict[str, Any]:
    source_refs = item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
    return {
        "review_item_id": item.get("review_item_id"),
        "review_type": item.get("review_type"),
        "title": item.get("title") or item.get("review_type") or "Review item",
        "summary": _today_review_summary(item),
        "confidence": item.get("confidence"),
        "recommended_action": item.get("recommended_action"),
        "recommended_actions": item.get("recommended_actions") or [],
        "source_ref_status": item.get("source_ref_status"),
        "evidence_count": len(source_refs),
        "source_refs": source_refs,
        "created_at": item.get("created_at"),
        "can_apply_now": bool(item.get("can_apply_now")),
        "apply_ready": bool(item.get("apply_ready")),
    }


def _today_review_summary(item: dict[str, Any]) -> str:
    review_type = str(item.get("review_type") or "candidate")
    source_status = str(item.get("source_ref_status") or "unknown")
    action = str(item.get("recommended_action") or "review")
    return f"{review_type} / {source_status} source refs / {action}"


def _workspace_writer_query(selected_text: str, draft_text: str, instruction: str) -> str:
    basis = selected_text or draft_text
    return " ".join(part for part in [basis[:260], instruction[:160]] if part).strip()


def _evidence_brief_artifacts(
    store: Any,
    *,
    tenant_id: str,
    owner_user_id: str,
    job_id: str | None,
    digest_note_ids: set[str],
    claim_ids: set[str],
    review_item_ids: set[str],
    limit: int,
) -> tuple[list[Any], list[Any], list[ReviewItem]]:
    notes = store.list_digest_notes(owner_user_id=owner_user_id, tenant_id=tenant_id, job_id=job_id, limit=limit)
    claims = store.list_knowledge_claims(owner_user_id=owner_user_id, tenant_id=tenant_id, job_id=job_id, limit=limit)
    review_items = [
        item
        for item in store.list_review_items(tenant_id=tenant_id)
        if item.owner_user_id == owner_user_id
        and (not job_id or str((item.proposal or {}).get("job_id") or "") == job_id)
    ][:limit]
    if digest_note_ids:
        notes = [note for note in notes if note.digest_note_id in digest_note_ids]
    if claim_ids:
        claims = [claim for claim in claims if claim.knowledge_claim_id in claim_ids]
    if review_item_ids:
        review_items = [item for item in review_items if item.review_item_id in review_item_ids]
    return notes, claims, review_items


def _evidence_brief_ask_runs(
    store: Any,
    *,
    tenant_id: str,
    owner_user_id: str,
    ask_run_ids: set[str],
    limit: int,
) -> list[Any]:
    if not ask_run_ids:
        return []
    if hasattr(store, "connect"):
        with store.connect() as conn:
            rows = conn.execute(
                """
                select *
                from ask_runs
                where tenant_id = %s and owner_user_id = %s and run_id = any(%s)
                order by started_at desc, run_id
                limit %s
                """,
                (tenant_id, owner_user_id, list(ask_run_ids), max(1, min(limit, 100))),
            ).fetchall()
        mapper = getattr(store, "_ask_run_from_row", None)
        return [mapper(row) for row in rows] if callable(mapper) else list(rows)
    runs = [
        run
        for run in getattr(store, "ask_runs", {}).values()
        if getattr(run, "tenant_id", DEFAULT_TENANT_ID) == tenant_id
        and getattr(run, "owner_user_id", "") == owner_user_id
        and getattr(run, "run_id", "") in ask_run_ids
    ]
    return sorted(runs, key=lambda run: getattr(run, "started_at", datetime.min.replace(tzinfo=UTC)), reverse=True)[:limit]


def _evidence_brief_refs(store: Any, *, tenant_id: str, owner_user_id: str, artifacts: list[Any]) -> list[dict[str, Any]]:
    raw_refs: list[dict[str, Any]] = []
    for artifact in artifacts:
        artifact_refs: list[dict[str, Any]] = []
        query_anchors: list[str] = []
        if isinstance(artifact, ReviewItem):
            proposal = artifact.proposal or {}
            artifact_refs.extend(_source_refs_payload(proposal.get("source_refs") or proposal.get("sourceRefs") or []))
        elif _is_ask_run_artifact(artifact):
            result = _ask_run_result_payload(artifact)
            evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
            artifact_refs.extend(_source_refs_payload(result.get("source_refs") or result.get("citations") or evidence.get("source_refs") or evidence.get("citations") or []))
            query_anchors = _ask_query_anchor_terms(_ask_run_query(artifact))
        else:
            artifact_refs.extend(_source_refs_payload(getattr(artifact, "source_refs", [])))
        if query_anchors:
            artifact_refs = [ref for ref in artifact_refs if not _source_ref_negated_for_terms(ref, query_anchors)]
        raw_refs.extend(artifact_refs)
    refs = _dedupe_writing_refs(raw_refs)
    source_by_id = {
        item.source_item_id: item
        for item in store.list_source_items(tenant_id=tenant_id)
        if getattr(item, "owner_user_id", "") == owner_user_id
    }
    enriched: list[dict[str, Any]] = []
    for ref in refs:
        source_item_id = str(ref.get("source_item_id") or "")
        source = source_by_id.get(source_item_id)
        next_ref = dict(ref)
        if source:
            next_ref.setdefault("title", source.title)
            next_ref.setdefault("url", source.url)
            next_ref.setdefault("source_channel", source.source_channel)
            next_ref.setdefault("lifecycle_status", _lifecycle_status(source))
        else:
            next_ref.setdefault("lifecycle_status", "missing")
        enriched.append(next_ref)
    return _enrich_source_refs_knowledge_bases(
        store,
        enriched,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )


def _source_ref_lifecycle_status(ref: dict[str, Any]) -> str:
    return str(ref.get("lifecycle_status") or "active")


def _source_ref_negated_for_terms(ref: dict[str, Any], terms: list[str]) -> bool:
    texts = [
        str(ref.get("mention_text") or ""),
        str(ref.get("snippet") or ""),
        str(ref.get("title") or ""),
    ]
    source_window = ref.get("source_window") if isinstance(ref.get("source_window"), dict) else {}
    texts.append(str(source_window.get("text") or ""))
    haystack = "\n".join(text for text in texts if text)
    if not haystack.strip():
        return False
    return any(_text_has_negated_label(haystack, term) for term in terms)


def _is_ask_run_artifact(artifact: Any) -> bool:
    if isinstance(artifact, dict):
        return bool(artifact.get("run_id") and artifact.get("query") is not None)
    return bool(getattr(artifact, "run_id", None) and getattr(artifact, "query", None) is not None)


def _ask_run_result_payload(run: Any) -> dict[str, Any]:
    if isinstance(run, dict):
        return dict(run.get("result") or {})
    return dict(getattr(run, "result", {}) or {})


def _ask_run_id(run: Any) -> str:
    return str(run.get("run_id") if isinstance(run, dict) else getattr(run, "run_id", "") or "")


def _ask_run_query(run: Any) -> str:
    return str(run.get("query") if isinstance(run, dict) else getattr(run, "query", "") or "")


def _evidence_brief_warnings(notes: list[Any], claims: list[Any], review_items: list[ReviewItem], ask_runs: list[Any] | None = None) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for note in notes:
        if not _source_refs_payload(getattr(note, "source_refs", [])):
            warnings.append({"artifact_type": "digest_note", "artifact_id": note.digest_note_id, "warning": "missing_source_refs"})
    for claim in claims:
        if not _source_refs_payload(getattr(claim, "source_refs", [])):
            warnings.append({"artifact_type": "knowledge_claim", "artifact_id": claim.knowledge_claim_id, "warning": "missing_source_refs"})
    for item in review_items:
        if not _source_refs_payload((item.proposal or {}).get("source_refs") or []):
            warnings.append({"artifact_type": "review_item", "artifact_id": item.review_item_id, "warning": "missing_source_refs"})
    for run in ask_runs or []:
        result = _ask_run_result_payload(run)
        if result.get("answer_type") == "no_answer" or (result.get("evidence_check") or {}).get("status") == "insufficient":
            warnings.append({"artifact_type": "ask_run", "artifact_id": _ask_run_id(run), "warning": "insufficient_evidence"})
    return warnings


def _evidence_brief_unavailable(
    *,
    reason: str,
    error: str,
    tenant_id: str,
    owner_user_id: str,
    warnings: list[dict[str, Any]] | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "error": error,
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
        "brief": None,
        "board": None,
        "nodes": [],
        "edges": [],
        "warnings": warnings or [],
        "source_refs": source_refs or [],
    }


def _evidence_brief_title(notes: list[Any], claims: list[Any], review_items: list[ReviewItem], ask_runs: list[Any] | None = None) -> str:
    if notes:
        return f"Brief: {notes[0].title}"
    if claims:
        return f"Brief: {_trim_words(claims[0].statement, 12)}"
    if review_items:
        return f"Brief: {review_items[0].title}"
    if ask_runs:
        return f"Brief: {_trim_words(_ask_run_query(ask_runs[0]), 12)}"
    return "Evidence Brief"


def _evidence_brief_review_status(review_items: list[ReviewItem]) -> str:
    if any(item.status == "pending" for item in review_items):
        return "needs_review"
    if review_items:
        return "review_linked"
    return "draft"


def _evidence_brief_lineage(
    *,
    job_id: str | None,
    notes: list[Any],
    claims: list[Any],
    review_items: list[ReviewItem],
    ask_runs: list[Any],
    source_refs: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    producers = sorted(
        {
            str(getattr(item, "producer", "") or (getattr(item, "proposal", {}) or {}).get("producer") or "pska")
            for item in [*notes, *claims, *review_items]
        }
    )
    return {
        "producer": "pska.evidence_brief",
        "job_id": job_id,
        "source_refs": source_refs,
        "warnings": warnings,
        "producers": producers,
        "digest_note_ids": [note.digest_note_id for note in notes],
        "knowledge_claim_ids": [claim.knowledge_claim_id for claim in claims],
        "review_item_ids": [item.review_item_id for item in review_items],
        "ask_run_ids": [_ask_run_id(run) for run in (ask_runs or [])],
        "review_status": _evidence_brief_review_status(review_items),
        **_knowledge_base_lineage_from_source_refs(source_refs),
    }


def _create_evidence_brief_writing_nodes(
    store: Any,
    *,
    board: WritingBoard,
    notes: list[Any],
    claims: list[Any],
    review_items: list[ReviewItem],
    ask_runs: list[Any],
    refs: list[dict[str, Any]],
    lineage: dict[str, Any],
) -> tuple[list[WritingNode], list[WritingEdge]]:
    nodes: list[WritingNode] = []
    edges: list[WritingEdge] = []
    enriched_ref_by_key = {
        _source_ref_key(ref): ref
        for ref in refs
        if _source_ref_key(ref) != ("", "")
    }

    def enriched_node_refs(raw_refs: Any) -> list[dict[str, Any]]:
        node_refs: list[dict[str, Any]] = []
        for ref in _source_refs_payload(raw_refs):
            key = _source_ref_key(ref)
            if key in enriched_ref_by_key:
                node_refs.append(enriched_ref_by_key[key])
        return _dedupe_writing_refs(node_refs)

    def add_node(node_type: str, title: str, body: str, *, x: int, y: int, source_refs: list[dict[str, Any]] | None = None, metadata: dict[str, Any] | None = None, status: str = "draft") -> WritingNode:
        node = WritingNode(
            node_id=f"wnode_{uuid4().hex}",
            board_id=board.board_id,
            tenant_id=board.tenant_id,
            owner_user_id=board.owner_user_id,
            node_type=node_type,
            title=title,
            body_markdown=body,
            position={"x": x, "y": y},
            status=status,
            source_refs=source_refs or [],
            citations=source_refs or [],
            metadata={"expanded": node_type in {"draft", "section"}, **dict(metadata or {})},
        )
        nodes.append(store.upsert_writing_node(node))
        return nodes[-1]

    def add_edge(source: WritingNode, target: WritingNode, edge_type: str, label: str) -> None:
        edge = WritingEdge(
            edge_id=f"wedge_{uuid4().hex}",
            board_id=board.board_id,
            tenant_id=board.tenant_id,
            owner_user_id=board.owner_user_id,
            source_node_id=source.node_id,
            target_node_id=target.node_id,
            edge_type=edge_type,
            label=label,
        )
        edges.append(store.upsert_writing_edge(edge))

    section = add_node(
        "section",
        "Evidence Brief",
        "This draft is grounded in Digest/Review artifacts and should be reviewed before long-term publication.",
        x=80,
        y=120,
        source_refs=refs,
        metadata={"lineage": lineage},
    )
    draft = add_node(
        "draft",
        board.title,
        _evidence_brief_markdown(board.title, notes=notes, claims=claims, review_items=review_items, ask_runs=ask_runs, refs=refs, lineage=lineage),
        x=420,
        y=120,
        source_refs=refs,
        metadata={"lineage": lineage, "wiki_status": "draft"},
    )
    add_edge(section, draft, "follows", "生成草稿")
    for index, note in enumerate(notes[:8]):
        note_refs = enriched_node_refs(getattr(note, "source_refs", []))
        node = add_node(
            "evidence",
            note.title or "Digest note",
            _digest_note_brief_body(note),
            x=80,
            y=360 + index * 130,
            source_refs=note_refs,
            metadata={"artifact_type": "digest_note", "artifact_id": note.digest_note_id, "lineage": lineage},
        )
        add_edge(node, draft, "supported_by", "Digest")
    for index, claim in enumerate(claims[:10]):
        claim_refs = enriched_node_refs(getattr(claim, "source_refs", []))
        node = add_node(
            "answer",
            _trim_words(claim.statement, 14) or "Knowledge claim",
            _claim_brief_body(claim),
            x=420,
            y=360 + index * 130,
            source_refs=claim_refs,
            metadata={"artifact_type": "knowledge_claim", "artifact_id": claim.knowledge_claim_id, "lineage": lineage},
            status="needs_review" if float(getattr(claim, "confidence", 0.0) or 0.0) < 0.7 else "draft",
        )
        add_edge(node, draft, "supported_by", "Claim")
    for index, item in enumerate(review_items[:10]):
        item_refs = enriched_node_refs((item.proposal or {}).get("source_refs") or [])
        node = add_node(
            "gap" if item.status == "pending" else "evidence",
            item.title or "Review item",
            _review_item_brief_body(item),
            x=760,
            y=360 + index * 130,
            source_refs=item_refs,
            metadata={"artifact_type": "review_item", "artifact_id": item.review_item_id, "review_status": item.status, "lineage": lineage},
            status=item.status,
        )
        add_edge(node, draft, "raises" if item.status == "pending" else "supported_by", "Review")
    ask_y_offset = 360 + max(len(notes), len(claims), len(review_items), 0) * 130
    for index, run in enumerate(ask_runs[:8]):
        result = _ask_run_result_payload(run)
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        run_refs = enriched_node_refs(result.get("source_refs") or result.get("citations") or evidence.get("source_refs") or evidence.get("citations") or [])
        node = add_node(
            "answer" if result.get("answer_type") != "no_answer" else "gap",
            _trim_words(_ask_run_query(run), 14) or "Ask answer",
            _ask_run_brief_body(run),
            x=1080,
            y=ask_y_offset + index * 130,
            source_refs=run_refs,
            metadata={"artifact_type": "ask_run", "artifact_id": _ask_run_id(run), "lineage": lineage, "answer_type": result.get("answer_type")},
            status="needs_review" if result.get("answer_type") == "no_answer" else "draft",
        )
        add_edge(node, draft, "raises" if result.get("answer_type") == "no_answer" else "supported_by", "Ask")
    return nodes, edges


def _evidence_brief_markdown(title: str, *, notes: list[Any], claims: list[Any], review_items: list[ReviewItem], ask_runs: list[Any], refs: list[dict[str, Any]], lineage: dict[str, Any]) -> str:
    lines = [f"# {title}", "", "> Draft Evidence Brief. Review before publishing to long-term knowledge.", ""]
    if notes:
        lines.extend(["## Digest Notes", ""])
        for note in notes:
            lines.append(f"### {note.title}")
            if note.synopsis:
                lines.extend([str(note.synopsis), ""])
            for point in _list_of_dicts(getattr(note, "key_points", []))[:5]:
                text = point.get("text") or point.get("summary") or point.get("point")
                if text:
                    lines.append(f"- {text}")
            if lines[-1] != "":
                lines.append("")
    if claims:
        lines.extend(["## Claims", ""])
        for claim in claims:
            lines.append(f"- {claim.statement}")
            if claim.evidence_text:
                lines.append(f"  Evidence: {claim.evidence_text}")
        lines.append("")
    if review_items:
        lines.extend(["## Review Queue", ""])
        for item in review_items:
            summary = (item.proposal or {}).get("plain_text_summary") or (item.proposal or {}).get("reason") or item.review_type.value
            lines.append(f"- [{item.status}] {item.title}: {summary}")
        lines.append("")
    if ask_runs:
        lines.extend(["## Ask Answers", ""])
        for run in ask_runs:
            result = _ask_run_result_payload(run)
            answer = _trim_words(str(result.get("answer") or ""), 80)
            lines.append(f"### {_ask_run_query(run)}")
            if answer:
                lines.extend([answer, ""])
            if result.get("answer_type"):
                lines.append(f"- Answer type: {result.get('answer_type')}")
            if (result.get("evidence_check") or {}).get("status"):
                lines.append(f"- Evidence check: {(result.get('evidence_check') or {}).get('status')}")
            lines.append("")
    lines.extend(["## Citations", ""])
    for index, ref in enumerate(refs[:20], start=1):
        title_ref = str(ref.get("title") or ref.get("source_item_id") or ref.get("chunk_id") or f"Source {index}")
        source_id = str(ref.get("source_item_id") or "")
        suffix = f" ({source_id})" if source_id and source_id != title_ref else ""
        knowledge_base_label = ", ".join(_string_list(ref.get("knowledge_base_names")) or _string_list(ref.get("knowledge_base_ids")))
        knowledge_base_suffix = f" [KB: {knowledge_base_label}]" if knowledge_base_label else ""
        lines.append(f"{index}. {title_ref}{suffix}{knowledge_base_suffix}")
    lines.extend(["", "## Lineage", "", f"- Producer: {lineage.get('producer')}", f"- Review status: {lineage.get('review_status')}", f"- Job: {lineage.get('job_id') or '-'}"])
    knowledge_base_names = _string_list(lineage.get("knowledge_base_names")) or _string_list(lineage.get("knowledge_base_ids"))
    if knowledge_base_names:
        lines.append(f"- Knowledge bases: {', '.join(knowledge_base_names)}")
    return "\n".join(lines).strip()


def _digest_note_brief_body(note: Any) -> str:
    lines = [note.synopsis or ""]
    for key, title in [("actions", "Actions"), ("open_questions", "Open Questions"), ("risks", "Risks")]:
        values = _list_of_dicts(getattr(note, key, []))
        if values:
            lines.extend(["", f"**{title}**"])
            for value in values[:5]:
                text = value.get("text") or value.get("summary") or value.get("title") or value.get("question") or value.get("risk")
                if text:
                    lines.append(f"- {text}")
    return "\n".join(line for line in lines if line is not None).strip()


def _claim_brief_body(claim: Any) -> str:
    lines = [claim.statement]
    if claim.evidence_text:
        lines.extend(["", f"Evidence: {claim.evidence_text}"])
    lines.extend(["", f"Confidence: {float(getattr(claim, 'confidence', 0.0) or 0.0):.2f}"])
    return "\n".join(lines).strip()


def _review_item_brief_body(item: ReviewItem) -> str:
    proposal = item.proposal or {}
    summary = proposal.get("plain_text_summary") or proposal.get("statement") or proposal.get("reason") or item.review_type.value
    return "\n".join(
        [
            str(summary),
            "",
            f"Review status: {item.status}",
            f"Review type: {item.review_type.value if hasattr(item.review_type, 'value') else item.review_type}",
        ]
    ).strip()


def _ask_run_brief_body(run: Any) -> str:
    result = _ask_run_result_payload(run)
    evidence_check = result.get("evidence_check") if isinstance(result.get("evidence_check"), dict) else {}
    reasons = result.get("no_answer_reasons") or evidence_check.get("no_answer_reasons") or []
    lines = [
        f"Question: {_ask_run_query(run)}",
        "",
        str(result.get("answer") or "").strip(),
        "",
        f"Answer type: {result.get('answer_type') or 'ask_answer'}",
        f"Evidence check: {evidence_check.get('status') or '-'}",
    ]
    if reasons:
        lines.extend(["", "No-answer reasons:"])
        lines.extend(f"- {reason}" for reason in reasons[:6])
    return "\n".join(line for line in lines if line is not None).strip()


def _writing_board_payload(board: WritingBoard) -> dict[str, Any]:
    return {
        "board_id": board.board_id,
        "tenant_id": board.tenant_id,
        "owner_user_id": board.owner_user_id,
        "title": board.title,
        "goal": board.goal,
        "metadata": board.metadata,
        "created_at": board.created_at.isoformat(),
        "updated_at": board.updated_at.isoformat(),
    }


def _writing_node_payload(node: WritingNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "board_id": node.board_id,
        "tenant_id": node.tenant_id,
        "owner_user_id": node.owner_user_id,
        "node_type": node.node_type,
        "title": node.title,
        "body_markdown": node.body_markdown,
        "position": node.position,
        "size": node.size,
        "status": node.status,
        "source_refs": node.source_refs,
        "citations": node.citations,
        "quality_signals": node.quality_signals,
        "metadata": node.metadata,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }


def _writing_edge_payload(edge: WritingEdge) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "board_id": edge.board_id,
        "tenant_id": edge.tenant_id,
        "owner_user_id": edge.owner_user_id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "edge_type": edge.edge_type,
        "label": edge.label,
        "metadata": edge.metadata,
        "created_at": edge.created_at.isoformat(),
        "updated_at": edge.updated_at.isoformat(),
    }


def _writing_node_type(value: str) -> str:
    allowed = {"goal", "question", "answer", "evidence", "gap", "section", "draft"}
    return value if value in allowed else "question"


def _writing_edge_type(value: str) -> str:
    allowed = {"decomposes_to", "answered_by", "supported_by", "raises", "conflicts_with", "included_in", "follows"}
    return value if value in allowed else "raises"


def _writing_default_node_title(node_type: str) -> str:
    labels = {
        "goal": "写作目标",
        "question": "待回答问题",
        "answer": "证据回答",
        "evidence": "证据",
        "gap": "缺口",
        "section": "章节",
        "draft": "草稿",
    }
    return labels.get(node_type, "写作节点")


def _writing_question_suggestions(
    board: WritingBoard,
    nodes: list[WritingNode],
    focus: WritingNode | None,
    *,
    direction: str,
) -> list[dict[str, Any]]:
    basis = " ".join(part for part in [focus.title if focus else "", focus.body_markdown if focus else "", board.goal, board.title] if part).strip()
    topic = _trim_words(basis, 28) or "当前写作目标"
    existing_questions = {
        _normalize_question_text(node.title or node.body_markdown)
        for node in nodes
        if node.node_type == "question"
    }
    templates = {
        "decompose": [
            ("结构拆解", "要把“{topic}”写清楚，至少需要回答哪几个部分？"),
            ("核心判断", "这篇内容最终需要证明或判断的中心命题是什么？"),
            ("读者路径", "读者理解“{topic}”之前，必须先知道哪些背景？"),
        ],
        "evidence_gap": [
            ("证据缺口", "围绕“{topic}”，目前最需要补证据的是哪一处？"),
            ("可引用来源", "哪些来源可以直接支撑“{topic}”，哪些只能作为背景？"),
            ("反证检查", "有哪些证据可能削弱或反驳当前判断？"),
        ],
        "counterpoint": [
            ("相反观点", "如果反对“{topic}”的结论，最强理由会是什么？"),
            ("边界条件", "在什么条件下，当前结论需要降级或改写？"),
            ("冲突梳理", "现有资料之间是否存在冲突、时间差或口径差？"),
        ],
        "followup": [
            ("继续追问", "这个回答还引出了哪一个最值得继续查的问题？"),
            ("章节定位", "这个答案适合放进文章的哪一部分，为什么？"),
            ("行动结论", "基于“{topic}”，可以形成什么谨慎、可引用的下一步结论？"),
        ],
    }
    selected = templates.get(direction, templates["followup"])
    suggestions: list[dict[str, Any]] = []
    for index, (title, question_template) in enumerate(selected):
        question = question_template.format(topic=topic)
        if _normalize_question_text(question) in existing_questions:
            continue
        suggestions.append(
            {
                "suggestion_id": f"suggest_{index + 1}",
                "title": title,
                "question": question,
                "direction": direction,
                "rationale": "这是通用写作追问，用于扩展论点、证据、反证或章节结构。",
            }
        )
    return suggestions


def _writing_compose_markdown(
    *,
    board: WritingBoard,
    section: WritingNode | None,
    answer_nodes: list[WritingNode],
) -> str:
    title = section.title if section else board.title
    lines = [f"## {title or '写作草稿'}", ""]
    if section and section.body_markdown.strip():
        lines.extend([section.body_markdown.strip(), ""])
    if not answer_nodes:
        lines.extend(["当前章节还没有纳入答案节点。请先把有证据的 answer 节点加入章节。"])
        return "\n".join(lines).strip()
    for index, node in enumerate(answer_nodes, start=1):
        if node.title:
            lines.append(f"### {index}. {node.title}")
        body = node.body_markdown.strip() or "该答案节点还没有正文。"
        lines.extend([body, ""])
        refs = _dedupe_writing_refs([*node.citations, *node.source_refs])
        if refs:
            lines.append("引用：")
            for ref_index, ref in enumerate(refs[:6], start=1):
                title_ref = str(ref.get("title") or ref.get("source_item_id") or ref.get("chunk_id") or f"来源 {ref_index}")
                source_id = str(ref.get("source_item_id") or "")
                suffix = f" ({source_id})" if source_id and source_id != title_ref else ""
                lines.append(f"- {title_ref}{suffix}")
            lines.append("")
    return "\n".join(lines).strip()


def _dedupe_writing_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        key = "|".join(str(ref.get(part) or "") for part in ["source_item_id", "chunk_id", "title", "url"])
        if not key.strip("|") or key in seen:
            continue
        seen.add(key)
        result.append(dict(ref))
    return result


def _normalize_question_text(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _trim_words(value: str, max_words: int) -> str:
    words = re.split(r"\s+", value.strip())
    if len(words) <= max_words:
        return value.strip()
    return " ".join(words[:max_words])


def _workspace_writer_suggestion(
    *,
    selected_text: str,
    instruction: str,
    citations: list[dict[str, Any]],
    graph_paths: list[dict[str, Any]],
    memory_context: list[dict[str, Any]],
    profile_context: list[dict[str, Any]],
    gaps: list[Any],
    conflicts: list[Any],
) -> dict[str, Any]:
    citation_titles = [str(item.get("title") or item.get("source_item_id") or "") for item in citations[:3] if item]
    memory_count = len(memory_context)
    profile_count = len(profile_context)
    graph_count = len(graph_paths)
    selected_preview = selected_text[:160] if selected_text else "当前草稿"
    bullets = [
        f"围绕选中文本“{selected_preview}”补一层明确论点，先说明结论，再接证据。",
        f"引用 {len(citations)} 条 citation；优先使用：{', '.join(citation_titles) if citation_titles else '暂无可用 citation'}。",
        f"结合 {memory_count} 条 memory、{profile_count} 条 profile 和 {graph_count} 条 graph path，但只保留和段落意图直接相关的内容。",
    ]
    if gaps:
        bullets.append("存在检索 gap，建议把不确定判断改成待确认表述。")
    if conflicts:
        bullets.append("存在 conflict，建议显式标注不同证据之间的分歧。")
    proposed_text = (
        "建议改写方向：先把这段收束为一个可验证的中文判断，随后用 PSKA citation 支撑，"
        "最后补一句仍需确认的缺口或冲突。"
    )
    return {
        "language": "zh",
        "instruction": instruction,
        "summary": "已基于选中文本构造检索上下文，并生成一版不自动写回的中文写作建议。",
        "bullets": bullets,
        "proposed_text": proposed_text,
        "used_context": {
            "citation_count": len(citations),
            "graph_path_count": graph_count,
            "memory_count": memory_count,
            "profile_count": profile_count,
            "gap_count": len(gaps),
            "conflict_count": len(conflicts),
        },
    }


def _console_agent_memory(memory: Any) -> dict[str, Any]:
    source_refs = _console_source_ref_summaries(getattr(memory, "source_refs", []))
    confidence = float(getattr(memory, "confidence", 0.0) or 0.0)
    decay_policy = str(getattr(memory, "decay_policy", "") or "manual")
    status = "forgotten" if decay_policy == "forgotten" or confidence <= 0 else "active"
    return {
        "agent_memory_id": getattr(memory, "agent_memory_id", ""),
        "owner_user_id": getattr(memory, "owner_user_id", ""),
        "layer": getattr(getattr(memory, "layer", ""), "value", str(getattr(memory, "layer", ""))),
        "text": getattr(memory, "text", ""),
        "confidence": confidence,
        "source_refs": source_refs,
        "source_ref_status": "present" if source_refs else "missing",
        "last_verified_at": getattr(memory, "last_verified_at", None),
        "status": status,
        "promotion_status": "forgotten" if status == "forgotten" else ("updated" if getattr(memory, "last_verified_at", None) else "promoted"),
        "decay_policy": decay_policy,
        "created_by_user_id": getattr(memory, "created_by_user_id", None),
        "needs_attention": (not source_refs) or confidence < 0.5 or status == "forgotten",
    }


def _console_profile_card(card: Any) -> dict[str, Any]:
    source_refs = _console_source_ref_summaries(getattr(card, "source_refs", []))
    confidence = float(getattr(card, "confidence", 0.0) or 0.0)
    return {
        "profile_card_id": getattr(card, "profile_card_id", ""),
        "owner_user_id": getattr(card, "owner_user_id", ""),
        "profile": getattr(card, "profile", {}) or {},
        "confidence": confidence,
        "source_refs": source_refs,
        "source_ref_status": "present" if source_refs else "missing",
        "last_verified_at": getattr(card, "last_verified_at", None),
        "status": "active",
        "promotion_status": "updated" if getattr(card, "last_verified_at", None) else "promoted",
        "needs_attention": (not source_refs) or confidence < 0.5,
    }


def _console_source_ref_summaries(source_refs: Any) -> list[dict[str, Any]]:
    allowed = set(SourceRef.__dataclass_fields__)
    summaries: list[dict[str, Any]] = []
    for ref in source_refs if isinstance(source_refs, list) else []:
        if isinstance(ref, dict):
            summary = {key: value for key, value in ref.items() if key in allowed and value}
        else:
            summary = {key: getattr(ref, key) for key in allowed if getattr(ref, key, None)}
        if summary:
            summaries.append(summary)
    return summaries


def _console_service_readiness(ready: dict[str, Any]) -> dict[str, Any]:
    checks = ready.get("checks") if isinstance(ready.get("checks"), dict) else {}
    agentic_service = checks.get("agentic_service") or {}
    return {
        "ok": bool(ready.get("ok")),
        "database_ok": bool((checks.get("database") or {}).get("ok")),
        "schema_ok": bool((checks.get("schema") or {}).get("ok")),
        "mcp_ok": bool((checks.get("mcp") or {}).get("ok")),
        "jobs_ok": bool((checks.get("jobs") or {}).get("ok")),
        "metrics_ok": bool((checks.get("metrics") or {}).get("ok")),
        "agentic_service_ok": bool(agentic_service.get("ok")),
        "agentic_service_provider": agentic_service.get("provider"),
        "agentic_service_adapter": agentic_service.get("adapter"),
        "agentic_service_error": agentic_service.get("error"),
        "error": ready.get("error"),
    }


def _console_ops_issues(
    ready: dict[str, Any],
    stats: dict[str, Any],
    failed_jobs: list[dict[str, Any]],
    stale_running: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
    checks = ready.get("checks") if isinstance(ready.get("checks"), dict) else {}
    agentic_service = checks.get("agentic_service") or {}
    digest_backlog = stats.get("digest_backlog") or {}
    failed_digest = [job for job in failed_jobs if job.get("job_type") == DIGEST_VIA_FASTREACT]
    issues = [
        _console_issue(
            "service_readiness",
            "ok" if ready.get("ok") else "service_check_failed",
            "info" if ready.get("ok") else "critical",
            "PSKA service checks are healthy." if ready.get("ok") else "PSKA service readiness is failing.",
            [] if ready.get("ok") else ["./scripts/pska db-init", "./scripts/pska service-check"],
            {"error": ready.get("error")},
        ),
        _console_issue(
            "agentic_service",
            "ok" if agentic_service.get("ok") else "agentic_service_down",
            "info" if agentic_service.get("ok") else "warning",
            "Agentic service is reachable." if agentic_service.get("ok") else "Agentic service is offline, unauthorized, or missing PSKA tools.",
            [] if agentic_service.get("ok") else ["./scripts/pska fastreact-digest-worker-command", "./scripts/pska service-check"],
            {
                "provider": agentic_service.get("provider"),
                "adapter": agentic_service.get("adapter"),
                "error": agentic_service.get("error"),
                "pska_tools_loaded": agentic_service.get("pska_tools_loaded"),
            },
        ),
        _console_issue(
            "stale_jobs",
            "stale_job" if stale_running else "ok",
            "warning" if stale_running else "info",
            f"{len(stale_running)} stale running job(s) need recovery." if stale_running else "No stale running jobs detected.",
            ["./scripts/pska job-recover --max-age-seconds 900", "./scripts/pska jobs list --status running"] if stale_running else [],
            {"stale_running": stale_running},
        ),
        _console_issue(
            "failed_digest",
            "failed_digest" if failed_digest else "ok",
            "warning" if failed_digest else "info",
            f"{len(failed_digest)} failed digest job(s) need inspection." if failed_digest else "No failed digest jobs in the recent sample.",
            ["./scripts/pska jobs list --status failed --job-type digest_via_fastreact", "./scripts/pska fastreact-digest-worker-command"] if failed_digest else [],
            {"failed_digest_jobs": failed_digest},
        ),
        _console_issue(
            "digest_backlog",
            "empty_backlog" if int(digest_backlog.get("jobs") or 0) == 0 else "backlog_present",
            "info",
            "Digest backlog is empty." if int(digest_backlog.get("jobs") or 0) == 0 else f"Digest backlog has {int(digest_backlog.get('jobs') or 0)} job(s).",
            ["./scripts/pska digest-schedule --owner-user-id user_primary"] if int(digest_backlog.get("jobs") or 0) == 0 else ["./scripts/pska fastreact-digest-worker-command"],
            {"digest_backlog": digest_backlog},
        ),
        _console_issue(
            "port_8765",
            "check_manually",
            "info",
            "If the console shows old routes or 401 JSON for pages, port 8765 may be occupied by an older daemon.",
            ["lsof -nP -iTCP:8765 -sTCP:LISTEN", "./scripts/pska local-daemon"],
            {},
        ),
        _console_issue(
            "worker_boundary",
            "check_worker_filter",
            "info",
            "The local PSKA worker should not consume digest_via_fastreact jobs; use the configured agentic service adapter for that backlog.",
            ["./scripts/pska job-worker --exclude-job-type digest_via_fastreact", "./scripts/pska fastreact-digest-worker-command"],
            {},
        ),
    ]
    return issues


def _console_issue(
    issue_id: str,
    status: str,
    severity: str,
    summary: str,
    recovery_commands: list[str],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "status": status,
        "severity": severity,
        "summary": summary,
        "recovery_commands": recovery_commands,
        "diagnostics": diagnostics,
    }


def _console_job_is_stale(job: Any) -> bool:
    leased_until = getattr(job, "leased_until", None)
    return bool(getattr(job, "status", None) == "running" and leased_until and _as_aware(leased_until) < datetime.now(UTC))


def _console_job_summary(job: Any) -> dict[str, Any]:
    return {
        "job_id": getattr(job, "job_id", None),
        "job_type": getattr(job, "job_type", None),
        "status": getattr(job, "status", None),
        "worker_id": getattr(job, "worker_id", None),
        "leased_until": getattr(job, "leased_until", None),
        "error": getattr(job, "error", None),
    }


def _console_connector_state(state: Any) -> dict[str, Any]:
    permission_scope = getattr(state, "permission_scope", {}) or {}
    config = getattr(state, "config", {}) or {}
    return {
        "connector_state_id": getattr(state, "connector_state_id", None),
        "connector_id": getattr(state, "connector_id", None),
        "owner_user_id": getattr(state, "owner_user_id", None),
        "enabled": bool(getattr(state, "enabled", False)),
        "scan_cursor": getattr(state, "scan_cursor", None),
        "sync_status": getattr(state, "sync_status", None),
        "last_success_at": getattr(state, "last_success_at", None),
        "last_error_at": getattr(state, "last_error_at", None),
        "last_error": getattr(state, "last_error", None),
        "permission_scope": permission_scope,
        "config": config,
        "roots": _string_list(permission_scope.get("roots")) or _string_list(config.get("roots")),
    }


def _console_knowledge_source(source: Any, sync_runs: list[Any], processing_spans: list[Any] | None = None) -> dict[str, Any]:
    latest = sync_runs[0] if sync_runs else None
    config = getattr(source, "config", {}) or {}
    permission_scope = getattr(source, "permission_scope", {}) or {}
    spans = [_console_processing_span(span) for span in (processing_spans or [])]
    return {
        "knowledge_source_id": getattr(source, "knowledge_source_id", None),
        "name": getattr(source, "name", None),
        "source_type": getattr(source, "source_type", None),
        "uri": getattr(source, "uri", None),
        "mode": getattr(source, "mode", None),
        "status": getattr(source, "status", None),
        "connector_id": getattr(source, "connector_id", None),
        "path": config.get("path") or permission_scope.get("path"),
        "last_sync_at": getattr(source, "last_sync_at", None),
        "last_error": getattr(source, "last_error", None),
        "processing_config": (config.get("processing") if isinstance(config.get("processing"), dict) else None),
        "last_sync_run": to_jsonable(latest) if latest else None,
        "sync_runs": to_jsonable(sync_runs),
        "latest_processing_spans": spans,
        "processing_status": _processing_status_summary(spans),
    }


def _console_processing_span(span: Any) -> dict[str, Any]:
    return {
        "processing_span_id": getattr(span, "processing_span_id", None),
        "knowledge_source_id": getattr(span, "knowledge_source_id", None),
        "sync_run_id": getattr(span, "sync_run_id", None),
        "source_item_id": getattr(span, "source_item_id", None),
        "stage": getattr(span, "stage", None),
        "status": getattr(span, "status", None),
        "started_at": getattr(span, "started_at", None),
        "finished_at": getattr(span, "finished_at", None),
        "duration_ms": getattr(span, "duration_ms", None),
        "input": getattr(span, "input", {}) or {},
        "output": getattr(span, "output", {}) or {},
        "metadata": getattr(span, "metadata", {}) or {},
        "error": getattr(span, "error", None),
    }


def _processing_status_summary(spans: list[dict[str, Any]]) -> dict[str, Any]:
    if not spans:
        return {"status": "unknown", "failed": 0, "pending": 0, "succeeded": 0, "skipped": 0}
    counts: dict[str, int] = {}
    for span in spans:
        status = str(span.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    status = "failed" if counts.get("failed") else ("pending" if counts.get("pending") else "succeeded")
    return {"status": status, **counts}


def _console_knowledge_source_roots(sources: list[dict[str, Any]]) -> list[str]:
    roots: list[str] = []
    for source in sources:
        if source.get("source_type") != "folder" or not source.get("path"):
            continue
        if source.get("status") == "paused" or source.get("mode") == "paused":
            continue
        roots.append(str(source.get("path")))
    return list(dict.fromkeys(roots))


def _source_item_ids_for_knowledge_sources(store: Any, *, tenant_id: str, knowledge_source_ids: list[str]) -> list[str]:
    source_item_ids: list[str] = []
    for knowledge_source_id in knowledge_source_ids:
        for run in store.list_sync_runs(tenant_id=tenant_id, knowledge_source_id=knowledge_source_id, limit=20):
            report = dict(getattr(run, "report", {}) or {})
            source_item_ids.extend(_string_list(report.get("source_item_ids")))
    return list(dict.fromkeys(source_item_ids))


def _draft_knowledge_source_from_payload(payload: dict[str, Any], *, context: RequestContext | None = None) -> KnowledgeSource:
    owner_user_id = _owner_user_id_for_write(payload, context)
    tenant_id = str(payload.get("tenant_id") or (context.tenant_id if context else DEFAULT_TENANT_ID))
    source_type = _normal_source_type(payload.get("source_type") or payload.get("kind") or payload.get("type"))
    value = _source_value_from_payload(payload, source_type)
    visibility = Visibility(str(payload.get("visibility") or Visibility.PRIVATE))
    processing_config = payload.get("processing_config") if isinstance(payload.get("processing_config"), dict) else None
    if source_type == "folder":
        root = Path(value).expanduser().resolve(strict=False)
        uri = root.as_uri()
        connector_id = "files"
        config: dict[str, Any] = {
            "path": str(root),
            "ignore": _string_list(payload.get("ignore")),
            "max_bytes": _optional_positive_int(payload.get("max_bytes")) or DEFAULT_FILES_MAX_BYTES,
            "spreadsheet_max_rows_per_sheet": _optional_positive_int(
                payload.get("spreadsheet_max_rows_per_sheet") or payload.get("spreadsheet_row_limit_per_sheet")
            )
            or DEFAULT_SPREADSHEET_MAX_ROWS_PER_SHEET,
            "spreadsheet_max_columns": _optional_positive_int(
                payload.get("spreadsheet_max_columns") or payload.get("spreadsheet_column_limit")
            )
            or DEFAULT_SPREADSHEET_MAX_COLUMNS,
        }
        permission_scope = {"path": str(root), "read_scope": "explicit_directory"}
        name = str(payload.get("name") or root.name or str(root))
    else:
        uri = value
        connector_id = "rss" if source_type == "rss" else "url"
        config = {"url": uri}
        permission_scope = {"url": uri, "read_scope": "explicit_url"}
        name = str(payload.get("name") or _url_display_name(uri))
    if processing_config:
        config["processing"] = processing_config
    return KnowledgeSource(
        knowledge_source_id=knowledge_source_id(owner_user_id, uri, tenant_id=tenant_id),
        owner_user_id=owner_user_id,
        name=name,
        source_type=source_type,
        uri=uri,
        mode="preview",
        status="authorized",
        connector_id=connector_id,
        space_id=str(payload.get("space_id") or "private_primary"),
        visibility=visibility,
        visible_team_ids=_string_list(payload.get("visible_team_ids")),
        permission_scope=permission_scope,
        config=config,
        tenant_id=tenant_id,
    )


def _inline_knowledge_source_from_payload(
    payload: dict[str, Any],
    *,
    context: RequestContext | None,
    source_type: str,
    title: str,
    text: str,
) -> KnowledgeSource:
    owner_user_id = _owner_user_id_for_write(payload, context)
    tenant_id = str(payload.get("tenant_id") or (context.tenant_id if context else DEFAULT_TENANT_ID))
    source_id = str(payload.get("source_id") or payload.get("upload_id") or f"{source_type}_{uuid4().hex}").strip()
    safe_source_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", source_id).strip("_") or f"{source_type}_{uuid4().hex}"
    uri = str(payload.get("uri") or f"pska-{source_type}://{tenant_id}/{owner_user_id}/{safe_source_id}")
    processing_config = payload.get("processing_config") if isinstance(payload.get("processing_config"), dict) else None
    safe_title = postgres_safe_text(title)
    safe_text = postgres_safe_text(text)
    config: dict[str, Any] = {
        "source_id": safe_source_id,
        "title": safe_title,
        "content": {"text": safe_text},
        "record_type": "uploaded_document" if source_type == "upload" else "pasted_text",
        "metadata": {
            **dict(postgres_safe_json(payload.get("metadata") or {})),
            "product_input": True,
            "origin": payload.get("origin") or ("upload" if source_type == "upload" else "paste"),
        },
    }
    for key in ["filename", "content_type", "size_bytes"]:
        if payload.get(key) is not None:
            config[key] = payload[key]
    if processing_config:
        config["processing"] = processing_config
    config = postgres_safe_json(config)
    return KnowledgeSource(
        knowledge_source_id=knowledge_source_id(owner_user_id, uri, tenant_id=tenant_id),
        owner_user_id=owner_user_id,
        name=safe_title,
        source_type=source_type,
        uri=uri,
        mode="manual",
        status="authorized",
        connector_id=source_type,
        space_id=str(payload.get("space_id") or "private_primary"),
        visibility=Visibility(str(payload.get("visibility") or Visibility.PRIVATE)),
        visible_team_ids=_string_list(payload.get("visible_team_ids")),
        permission_scope={"read_scope": "user_submitted", "source_type": source_type},
        config=config,
        tenant_id=tenant_id,
    )


def _upload_text_from_payload(
    payload: dict[str, Any],
    *,
    max_bytes: int,
    spreadsheet_max_rows_per_sheet: int,
    spreadsheet_max_columns: int,
    document_parser: DocumentParserConfig | None = None,
) -> tuple[str, str, int, dict[str, Any]]:
    content_type = str(payload.get("content_type") or payload.get("mime_type") or "text/plain").strip() or "text/plain"
    if payload.get("text") is not None or payload.get("content") is not None:
        text = postgres_safe_text(str(payload.get("text") if payload.get("text") is not None else payload.get("content")))
        size_bytes = len(text.encode("utf-8"))
        if size_bytes > max_bytes:
            raise ValueError(f"uploaded file exceeds max_bytes ({size_bytes} > {max_bytes})")
        return text, content_type, size_bytes, {"ok": True, "extractor": "direct_text"}
    raw = b""
    filename = str(payload.get("filename") or payload.get("name") or "upload.txt").strip() or "upload.txt"
    if payload.get("bytes_base64"):
        raw = base64.b64decode(str(payload["bytes_base64"]))
    elif payload.get("file") and isinstance(payload.get("file"), dict):
        file_payload = payload["file"]
        filename = str(file_payload.get("filename") or filename).strip() or filename
        content_type = str(file_payload.get("content_type") or content_type)
        if file_payload.get("bytes_base64"):
            raw = base64.b64decode(str(file_payload["bytes_base64"]))
    if len(raw) > max_bytes:
        raise ValueError(f"uploaded file exceeds max_bytes ({len(raw)} > {max_bytes})")
    extraction = (
        extract_text_from_bytes(
            filename,
            raw,
            spreadsheet_max_rows_per_sheet=spreadsheet_max_rows_per_sheet,
            spreadsheet_max_columns=spreadsheet_max_columns,
            document_parser=document_parser,
        )
        if raw
        else {"ok": False, "reason": "empty_upload"}
    )
    if extraction.get("ok"):
        extraction_metadata = {
            key: value
            for key, value in extraction.items()
            if key not in {"text", "metadata", "extractor"}
        }
        extraction_metadata["extractor"] = extraction.get("extractor")
        extraction_metadata.update(dict(extraction.get("metadata") or {}))
        return postgres_safe_text(str(extraction.get("text") or "")), content_type, len(raw), postgres_safe_json(extraction_metadata)
    encoding = str(payload.get("encoding") or "utf-8").strip() or "utf-8"
    text = postgres_safe_text(raw.decode(encoding, errors="replace"))
    return text, content_type, len(raw), postgres_safe_json({**extraction, "fallback_extractor": "decode_replace", "encoding": encoding})


def _default_inline_title(text: str, *, fallback: str) -> str:
    first = re.sub(r"\s+", " ", str(text or "")).strip()[:80]
    return first or fallback


def _chunk_stats(chunks: list[Any]) -> dict[str, Any]:
    lengths = [len(str(getattr(chunk, "text", "") or "")) for chunk in chunks]
    return {
        "count": len(chunks),
        "min_chars": min(lengths) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
        "total_chars": sum(lengths),
    }


def _source_value_from_payload(payload: dict[str, Any], source_type: str) -> str:
    if source_type == "folder":
        value = payload.get("path") or payload.get("root") or payload.get("uri") or payload.get("url")
    else:
        value = payload.get("url") or payload.get("uri") or payload.get("path")
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{source_type} source requires a value")
    return value


def _normal_source_type(value: Any) -> str:
    source_type = str(value or "url").strip().lower()
    aliases = {
        "files": "folder",
        "file": "folder",
        "local": "folder",
        "directory": "folder",
        "feed": "rss",
        "atom": "rss",
        "rss_atom": "rss",
        "page": "url",
        "web": "url",
        "sitemap": "url",
    }
    return aliases.get(source_type, source_type)


def _url_display_name(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return name or parsed.netloc or url


def _console_files_roots(states: list[dict[str, Any]]) -> list[str]:
    roots: list[str] = []
    for state in states:
        connector_id = str(state.get("connector_id") or "")
        if connector_id and connector_id != "files":
            continue
        roots.extend(_string_list(state.get("roots")))
    return list(dict.fromkeys(roots))


def _console_files_commands(roots: list[str]) -> list[str]:
    if not roots:
        return ["./scripts/pska files-sync --root <authorized-root>"]
    root_args = " ".join(f"--root {root}" for root in roots)
    return [
        f"./scripts/pska files-sync {root_args}",
        f"./scripts/pska files-watch {root_args} --initial-sync",
    ]


def _normalized_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _console_input_sources(config: PSKAConfig, source_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    configured_roots = {_normalized_path(root) for root in config.files.roots}
    for source in source_cards:
        if source.get("source_type") != "folder":
            continue
        path = str(source.get("path") or "")
        if not path:
            continue
        configured = _normalized_path(Path(path)) in configured_roots
        if not configured and (source.get("status") == "paused" or source.get("mode") == "paused"):
            continue
        seen_paths.add(path)
        inputs.append(
            {
                "kind": "files_root",
                "name": source.get("name") or Path(path).name or "files",
                "path": path,
                "status": source.get("status"),
                "mode": source.get("mode"),
                "configured": configured,
                "knowledge_source_id": source.get("knowledge_source_id"),
            }
        )
    for root in config.files.roots:
        path = str(root.expanduser())
        if path in seen_paths:
            continue
        inputs.append(
            {
                "kind": "files_root",
                "name": root.expanduser().name or "files",
                "path": path,
                "status": "configured",
                "mode": "manual",
                "configured": True,
            }
        )
    twitter_dir = (config.workspace.user_sources_dir(config.files.tenant_id, config.files.owner_user_id) / "archives" / "twitter").expanduser()
    inputs.append(
        {
            "kind": "twitter_archive",
            "name": "Twitter/X zip inbox",
            "path": str(twitter_dir),
            "status": "available" if twitter_dir.exists() else "missing",
            "mode": "import",
            "configured": True,
            "zip_count": len(list(twitter_dir.glob("*.zip"))) if twitter_dir.exists() else 0,
        }
    )
    return inputs


def _workspace_excluded_paths(config: PSKAConfig) -> list[Path]:
    return [
        config.workspace.run_dir.expanduser(),
        config.workspace.log_dir.expanduser(),
    ]


def _lifecycle_status(item: Any) -> str:
    return str(getattr(item, "lifecycle_status", None) or "active")


def _is_active_lifecycle(item: Any) -> bool:
    return _lifecycle_status(item) == "active"


def _document_membership_delete_plan(
    store: Any,
    *,
    source_item_ids: list[str],
    knowledge_base_ids: list[str],
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, list[str]]:
    scoped_source_ids = store.list_knowledge_base_source_item_ids(
        set(knowledge_base_ids),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        active_only=True,
    )
    requested_source_ids = set(source_item_ids)
    membership_source_ids = sorted(requested_source_ids & scoped_source_ids)
    removed_knowledge_base_ids = set(knowledge_base_ids)
    orphan_source_ids = [
        source_item_id
        for source_item_id in membership_source_ids
        if not (
            store.list_knowledge_base_ids_for_source_item(
                source_item_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                active_only=True,
            )
            - removed_knowledge_base_ids
        )
    ]
    return {
        "membership_source_item_ids": membership_source_ids,
        "orphan_source_item_ids": sorted(orphan_source_ids),
    }


def _document_delete_impact(store: Any, *, tenant_id: str, source_item_ids: list[str]) -> dict[str, int]:
    source_ids = set(source_item_ids)
    documents = store.list_documents_for_sources(source_ids)
    chunks = store.list_chunks_for_sources(source_ids)
    counts = {
        "source_items": len(source_ids),
        "documents": len(documents),
        "chunks": len(chunks),
        "knowledge_claims": 0,
        "digest_notes": 0,
        "hyperedges": 0,
        "review_items": 0,
        "discovery_items": 0,
        "memories": 0,
        "agent_memories": 0,
        "user_profile_cards": 0,
        "jobs": 0,
        "offline_index_states": 0,
        "topic_mentions": 0,
        "artifact_supports": 0,
    }
    if hasattr(store, "connect"):
        with store.connect() as conn:
            params = (list(source_ids),)
            counts.update(
                {
                    "offline_index_states": _count_sql(
                        conn,
                        """
                        select count(*) from offline_index_states
                        where tenant_id = %s
                          and (source_item_id = any(%s) or object_id = any(%s))
                        """,
                        (tenant_id, list(source_ids), list(source_ids)),
                    ),
                    "knowledge_claims": _count_source_refs(conn, "knowledge_claims", list(source_ids)),
                    "digest_notes": _count_source_refs(conn, "digest_notes", list(source_ids)),
                    "hyperedges": _count_source_refs(conn, "hyperedges", list(source_ids)),
                    "review_items": _count_text_refs(conn, "review_items", "proposal", list(source_ids)),
                    "discovery_items": _count_text_refs(conn, "discovery_items", "evidence", list(source_ids)),
                    "memories": _count_source_refs(conn, "memories", list(source_ids)),
                    "agent_memories": _count_source_refs(conn, "agent_memories", list(source_ids)),
                    "user_profile_cards": _count_source_refs(conn, "user_profile_cards", list(source_ids), source_refs_column="source_refs"),
                    "jobs": _count_text_refs(conn, "jobs", "payload", list(source_ids)),
                    "topic_mentions": _count_sql(
                        conn,
                        "select count(*) from topic_mentions where tenant_id = %s and source_item_id = any(%s)",
                        (tenant_id, list(source_ids)),
                    ),
                    "artifact_supports": _count_sql(
                        conn,
                        "select count(*) from artifact_supports where tenant_id = %s and source_item_id = any(%s)",
                        (tenant_id, list(source_ids)),
                    ),
                }
            )
            _ = params
    else:
        claims = getattr(store, "knowledge_claims", {})
        notes = getattr(store, "digest_notes", {})
        reviews = getattr(store, "review_items", {})
        topic_mentions = getattr(store, "topic_mentions", {})
        artifact_supports = getattr(store, "artifact_supports", {})
        counts["knowledge_claims"] = sum(1 for claim in claims.values() if _object_has_source_ref(claim, source_ids))
        counts["digest_notes"] = sum(1 for note in notes.values() if _object_has_source_ref(note, source_ids))
        counts["review_items"] = sum(1 for item in reviews.values() if any(source_id in json.dumps(to_jsonable(getattr(item, "proposal", {}))) for source_id in source_ids))
        counts["topic_mentions"] = sum(1 for item in topic_mentions.values() if getattr(item, "source_item_id", "") in source_ids)
        counts["artifact_supports"] = sum(1 for item in artifact_supports.values() if getattr(item, "source_item_id", "") in source_ids)
    return counts


def _object_has_source_ref(value: Any, source_ids: set[str]) -> bool:
    refs = getattr(value, "source_refs", []) or []
    for ref in refs:
        if isinstance(ref, SourceRef) and ref.source_item_id in source_ids:
            return True
        if isinstance(ref, dict) and ref.get("source_item_id") in source_ids:
            return True
    return False


def _document_delete_notes(*, restore: bool, hard_delete: bool, membership_delete: bool = False) -> list[str]:
    if restore:
        return ["已恢复软删资料，资料、原文和检索片段会重新参与 Ask。"]
    if hard_delete:
        return ["彻底清除会删除资料条目、原文、检索片段、索引状态，以及未审阅的派生产物；已审阅知识不会静默删除。"]
    if membership_delete:
        return ["会先从当前知识库移除资料 membership；如果资料不再属于任何 active 知识库，再进入软删流程。"]
    return ["软删会让资料从检索中隐藏；已审阅的派生知识会标记为 stale/evidence_removed 并进入 Review。"]


def _hard_delete_source_derivatives(store: Any, source_item_ids: list[str]) -> dict[str, int]:
    if not hasattr(store, "connect"):
        source_ids = set(source_item_ids)
        before = {
            "knowledge_claims": len(getattr(store, "knowledge_claims", {})),
            "digest_notes": len(getattr(store, "digest_notes", {})),
            "review_items": len(getattr(store, "review_items", {})),
            "discovery_items": len(getattr(store, "discovery_items", {})),
            "agent_memories": len(getattr(store, "agent_memories", {})),
            "user_profile_cards": len(getattr(store, "profile_cards", {})),
            "hyperedges": len(getattr(store, "hyperedges", {})),
            "jobs": len(getattr(store, "jobs", {})),
            "topic_mentions": len(getattr(store, "topic_mentions", {})),
            "artifact_supports": len(getattr(store, "artifact_supports", {})),
        }
        store.knowledge_claims = {key: value for key, value in getattr(store, "knowledge_claims", {}).items() if not _object_has_source_ref(value, source_ids)}
        store.digest_notes = {key: value for key, value in getattr(store, "digest_notes", {}).items() if not _object_has_source_ref(value, source_ids)}
        deleted_review_ids = {
            key
            for key, value in getattr(store, "review_items", {}).items()
            if any(source_id in json.dumps(to_jsonable(getattr(value, "proposal", {}))) for source_id in source_ids)
            and getattr(value, "status", "pending") in {"pending", "new"}
        }
        store.review_items = {key: value for key, value in getattr(store, "review_items", {}).items() if key not in deleted_review_ids}
        for value in getattr(store, "review_items", {}).values():
            if any(source_id in json.dumps(to_jsonable(getattr(value, "proposal", {}))) for source_id in source_ids):
                proposal = dict(getattr(value, "proposal", {}) or {})
                proposal["lifecycle"] = {"status": "stale", "reason": "evidence_removed", "source_item_ids": sorted(source_ids)}
                value.proposal = proposal
        store.discovery_items = {
            key: value
            for key, value in getattr(store, "discovery_items", {}).items()
            if not any(source_id in json.dumps(to_jsonable(getattr(value, "evidence", []))) for source_id in source_ids)
        }
        store.agent_memories = {key: value for key, value in getattr(store, "agent_memories", {}).items() if not _object_has_source_ref(value, source_ids)}
        store.profile_cards = {key: value for key, value in getattr(store, "profile_cards", {}).items() if not _object_has_source_ref(value, source_ids)}
        store.hyperedges = {key: value for key, value in getattr(store, "hyperedges", {}).items() if not _object_has_source_ref(value, source_ids)}
        store.jobs = {
            key: value
            for key, value in getattr(store, "jobs", {}).items()
            if not any(source_id in json.dumps(to_jsonable(getattr(value, "payload", {}))) for source_id in source_ids)
        }
        store.topic_mentions = {
            key: value for key, value in getattr(store, "topic_mentions", {}).items() if getattr(value, "source_item_id", "") not in source_ids
        }
        store.artifact_supports = {
            key: value
            for key, value in getattr(store, "artifact_supports", {}).items()
            if getattr(value, "source_item_id", "") not in source_ids
            and not (getattr(value, "artifact_type", "") == "review_item" and getattr(value, "artifact_id", "") in deleted_review_ids)
        }
        return {
            "knowledge_claims": before["knowledge_claims"] - len(getattr(store, "knowledge_claims", {})),
            "digest_notes": before["digest_notes"] - len(getattr(store, "digest_notes", {})),
            "review_items": before["review_items"] - len(getattr(store, "review_items", {})),
            "discovery_items": before["discovery_items"] - len(getattr(store, "discovery_items", {})),
            "agent_memories": before["agent_memories"] - len(getattr(store, "agent_memories", {})),
            "user_profile_cards": before["user_profile_cards"] - len(getattr(store, "profile_cards", {})),
            "hyperedges": before["hyperedges"] - len(getattr(store, "hyperedges", {})),
            "jobs": before["jobs"] - len(getattr(store, "jobs", {})),
            "topic_mentions": before["topic_mentions"] - len(getattr(store, "topic_mentions", {})),
            "artifact_supports": before["artifact_supports"] - len(getattr(store, "artifact_supports", {})),
        }
    with store.connect() as conn:
        deleted: dict[str, int] = {}
        pending_review_ids = _pending_review_item_ids_for_purge(conn, source_item_ids)
        deleted["review_items"] = _delete_by_ids(conn, "review_items", "review_item_id", pending_review_ids)
        deleted["stale_review_items"] = _mark_review_items_stale_for_purge(conn, source_item_ids)
        deleted["discovery_items"] = _delete_text_refs(conn, "discovery_items", "evidence", source_item_ids)
        deleted["knowledge_claims"] = _delete_source_refs(conn, "knowledge_claims", source_item_ids)
        deleted["digest_notes"] = _delete_source_refs(conn, "digest_notes", source_item_ids)
        deleted["memories"] = _delete_source_refs(conn, "memories", source_item_ids)
        deleted["agent_memories"] = _delete_source_refs(conn, "agent_memories", source_item_ids)
        deleted["user_profile_cards"] = _delete_source_refs(conn, "user_profile_cards", source_item_ids)
        deleted["hyperedges"] = _delete_source_refs(conn, "hyperedges", source_item_ids)
        deleted["topic_mentions"] = _delete_where(conn, "delete from topic_mentions where source_item_id = any(%s)", (source_item_ids,))
        deleted["artifact_supports"] = _delete_artifact_supports_for_purge(conn, source_item_ids, pending_review_ids)
        deleted["jobs"] = _delete_text_refs(conn, "jobs", "payload", source_item_ids)
        return deleted


def _mark_source_derivatives_stale(store: Any, source_item_ids: list[str], *, tenant_id: str, actor_user_id: str, reason: str) -> dict[str, int]:
    if not hasattr(store, "connect"):
        source_ids = set(source_item_ids)
        counts = {"knowledge_claims": 0, "digest_notes": 0}
        for collection_name in ["knowledge_claims", "digest_notes"]:
            for value in getattr(store, collection_name, {}).values():
                if _object_has_source_ref(value, source_ids):
                    metadata = dict(getattr(value, "metadata", {}) or {})
                    metadata["lifecycle"] = {"status": "stale", "reason": reason, "actor_user_id": actor_user_id}
                    value.metadata = metadata
                    counts[collection_name] += 1
        updater = getattr(store, "update_artifact_support_status_for_sources", None)
        if callable(updater):
            counts["artifact_supports"] = updater(source_ids, tenant_id=tenant_id, status="evidence_removed")
        return counts
    lifecycle = Jsonb({"status": "stale", "reason": reason, "actor_user_id": actor_user_id, "source_item_ids": source_item_ids})
    with store.connect() as conn:
        counts = {}
        for table in ["knowledge_claims", "digest_notes", "hyperedges", "memories", "agent_memories", "user_profile_cards"]:
            if table in {"memories", "agent_memories", "user_profile_cards"}:
                metadata_column = "source_refs"
                _ = metadata_column
            count = _count_source_refs(conn, table, source_item_ids)
            if table in {"knowledge_claims", "digest_notes"}:
                conn.execute(
                    f"""
                    update {table}
                    set metadata = jsonb_set(coalesce(metadata, '{{}}'::jsonb), '{{lifecycle}}', %s, true)
                    where {_source_ref_exists_sql()}
                    """,  # noqa: S608 - fixed table allowlist.
                    (lifecycle, source_item_ids),
                )
            counts[table] = count
        updater = getattr(store, "update_artifact_support_status_for_sources", None)
        if callable(updater):
            counts["artifact_supports"] = updater(set(source_item_ids), tenant_id=tenant_id, status="evidence_removed")
        return counts


def _cleanup_knowledge_source_payload(
    store: PostgresKnowledgeStore,
    source: Any,
    *,
    execute: bool,
    delete_knowledge_source: bool,
    pause_knowledge_source: bool,
) -> dict[str, Any]:
    root = _knowledge_source_root(source)
    if not root:
        raise ValueError("knowledge source has no resolvable root")
    root_uri = Path(root).as_uri()
    with store.connect() as conn:
        source_rows = conn.execute(
            """
            select source_item_id, title, url, created_at
            from source_items
            where owner_user_id = %s
              and (
                url = %s
                or url like %s
                or metadata #>> '{extra,permission_metadata,root}' = %s
                or metadata #>> '{extra,connector,external_id}' like %s
              )
            order by created_at, source_item_id
            """,
            (
                source.owner_user_id,
                root_uri,
                f"{root_uri}/%",
                root,
                f"{root}/%",
            ),
        ).fetchall()
        source_item_ids = [str(row["source_item_id"]) for row in source_rows]
        counts = _cleanup_counts(conn, source_item_ids, source.knowledge_source_id)
        preview = {
            "knowledge_source": _console_knowledge_source(source, store.list_sync_runs(knowledge_source_id=source.knowledge_source_id, limit=1)),
            "root": root,
            "source_item_ids": source_item_ids,
            "source_items": [dict(row) for row in source_rows[:20]],
            "counts": counts,
            "execute": execute,
            "delete_knowledge_source": delete_knowledge_source,
            "pause_knowledge_source": pause_knowledge_source,
        }
        if not execute:
            return {"ok": True, "dry_run": True, **preview}
        deleted = _execute_knowledge_source_cleanup(
            conn,
            source_item_ids,
            source.knowledge_source_id,
            root=root,
            delete_knowledge_source=delete_knowledge_source,
            pause_knowledge_source=pause_knowledge_source,
        )
        return {"ok": True, "dry_run": False, **preview, "deleted": deleted}


def _knowledge_source_root(source: Any) -> str:
    config = getattr(source, "config", {}) or {}
    permission_scope = getattr(source, "permission_scope", {}) or {}
    path = config.get("path") or permission_scope.get("path")
    if path:
        return str(Path(str(path)).expanduser().resolve(strict=False))
    uri = str(getattr(source, "uri", "") or "")
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        return str(Path(unquote(parsed.path)).expanduser().resolve(strict=False))
    return uri


def _cleanup_counts(conn: Any, source_item_ids: list[str], knowledge_source_id: str) -> dict[str, int]:
    counts = {
        "knowledge_sources": 1,
        "sync_runs": _count_sql(conn, "select count(*) from sync_runs where knowledge_source_id = %s", (knowledge_source_id,)),
    }
    if not source_item_ids:
        counts.update(
            {
                "source_items": 0,
                "documents": 0,
                "chunks": 0,
                "processing_spans": _count_processing_spans_for_source(conn, knowledge_source_id, source_item_ids),
                "offline_index_states": 0,
                "knowledge_claims": 0,
                "digest_notes": 0,
                "hyperedges": 0,
                "review_items": 0,
                "discovery_items": 0,
                "memories": 0,
                "agent_memories": 0,
                "user_profile_cards": 0,
                "jobs": 0,
                "orphan_entities": 0,
            }
        )
        return counts
    params = (source_item_ids,)
    counts.update(
        {
            "source_items": len(source_item_ids),
            "documents": _count_sql(conn, "select count(*) from documents where source_item_id = any(%s)", params),
            "chunks": _count_sql(conn, "select count(*) from chunks where source_item_id = any(%s)", params),
            "processing_spans": _count_processing_spans_for_source(conn, knowledge_source_id, source_item_ids),
            "offline_index_states": _count_sql(
                conn,
                """
                select count(*) from offline_index_states
                where source_item_id = any(%s)
                   or object_id = any(%s)
                   or object_id in (select document_id from documents where source_item_id = any(%s))
                   or object_id in (select chunk_id from chunks where source_item_id = any(%s))
                """,
                (source_item_ids, source_item_ids, source_item_ids, source_item_ids),
            ),
            "knowledge_claims": _count_source_refs(conn, "knowledge_claims", source_item_ids),
            "digest_notes": _count_source_refs(conn, "digest_notes", source_item_ids),
            "hyperedges": _count_source_refs(conn, "hyperedges", source_item_ids),
            "review_items": _count_text_refs(conn, "review_items", "proposal", source_item_ids),
            "discovery_items": _count_text_refs(conn, "discovery_items", "evidence", source_item_ids),
            "memories": _count_source_refs(conn, "memories", source_item_ids),
            "agent_memories": _count_source_refs(conn, "agent_memories", source_item_ids),
            "user_profile_cards": _count_source_refs(conn, "user_profile_cards", source_item_ids, source_refs_column="source_refs"),
            "jobs": _count_text_refs(conn, "jobs", "payload", source_item_ids),
        }
    )
    orphan_entity_ids = _orphan_entity_ids_after_source_cleanup(conn, source_item_ids)
    counts["orphan_entities"] = len(orphan_entity_ids)
    return counts


def _execute_knowledge_source_cleanup(
    conn: Any,
    source_item_ids: list[str],
    knowledge_source_id: str,
    *,
    root: str,
    delete_knowledge_source: bool,
    pause_knowledge_source: bool,
) -> dict[str, int]:
    deleted: dict[str, int] = {}
    if source_item_ids:
        deleted["review_items"] = _delete_text_refs(conn, "review_items", "proposal", source_item_ids)
        deleted["discovery_items"] = _delete_text_refs(conn, "discovery_items", "evidence", source_item_ids)
        deleted["knowledge_claims"] = _delete_source_refs(conn, "knowledge_claims", source_item_ids)
        deleted["digest_notes"] = _delete_source_refs(conn, "digest_notes", source_item_ids)
        deleted["memories"] = _delete_source_refs(conn, "memories", source_item_ids)
        deleted["agent_memories"] = _delete_source_refs(conn, "agent_memories", source_item_ids)
        deleted["user_profile_cards"] = _delete_source_refs(conn, "user_profile_cards", source_item_ids)
        orphan_entity_ids = _orphan_entity_ids_after_source_cleanup(conn, source_item_ids)
        deleted["hyperedges"] = _delete_source_refs(conn, "hyperedges", source_item_ids)
        deleted["orphan_entities"] = _delete_by_ids(conn, "entities", "entity_id", orphan_entity_ids)
        deleted["jobs"] = _delete_text_refs(conn, "jobs", "payload", source_item_ids)
        deleted["offline_index_states"] = _delete_offline_index_states(conn, source_item_ids)
        deleted["processing_spans"] = _delete_processing_spans_for_source(conn, knowledge_source_id, source_item_ids)
        deleted["source_items"] = _delete_by_ids(conn, "source_items", "source_item_id", source_item_ids)
    else:
        deleted["processing_spans"] = _delete_processing_spans_for_source(conn, knowledge_source_id, source_item_ids)
    deleted["sync_runs"] = _delete_where(conn, "delete from sync_runs where knowledge_source_id = %s", (knowledge_source_id,))
    if delete_knowledge_source:
        deleted["knowledge_sources"] = _delete_where(conn, "delete from knowledge_sources where knowledge_source_id = %s", (knowledge_source_id,))
    elif pause_knowledge_source:
        row = conn.execute(
            """
            update knowledge_sources
            set mode = 'paused', status = 'paused', updated_at = now()
            where knowledge_source_id = %s
            returning knowledge_source_id
            """,
            (knowledge_source_id,),
        ).fetchone()
        deleted["knowledge_sources_paused"] = 1 if row else 0
    deleted["connector_state_roots_updated"] = _remove_root_from_connector_state(conn, root)
    return deleted


def _source_ref_exists_sql(column: str = "source_refs") -> str:
    return f"""
    exists (
      select 1
      from jsonb_array_elements(coalesce({column}, '[]'::jsonb)) ref
      where ref->>'source_item_id' = any(%s)
    )
    """


def _processing_spans_for_source_where(source_item_ids: list[str]) -> str:
    source_item_clause = "or source_item_id = any(%s)" if source_item_ids else ""
    return f"""
    where knowledge_source_id = %s
       {source_item_clause}
       or sync_run_id in (
            select sync_run_id from sync_runs where knowledge_source_id = %s
       )
    """


def _processing_spans_for_source_params(knowledge_source_id: str, source_item_ids: list[str]) -> tuple[Any, ...]:
    if source_item_ids:
        return (knowledge_source_id, source_item_ids, knowledge_source_id)
    return (knowledge_source_id, knowledge_source_id)


def _count_processing_spans_for_source(conn: Any, knowledge_source_id: str, source_item_ids: list[str]) -> int:
    return _count_sql(
        conn,
        f"select count(*) from processing_spans {_processing_spans_for_source_where(source_item_ids)}",
        _processing_spans_for_source_params(knowledge_source_id, source_item_ids),
    )


def _delete_processing_spans_for_source(conn: Any, knowledge_source_id: str, source_item_ids: list[str]) -> int:
    return _delete_where(
        conn,
        f"delete from processing_spans {_processing_spans_for_source_where(source_item_ids)}",
        _processing_spans_for_source_params(knowledge_source_id, source_item_ids),
    )


def _count_source_refs(conn: Any, table: str, source_item_ids: list[str], *, source_refs_column: str = "source_refs") -> int:
    return _count_sql(conn, f"select count(*) from {table} where {_source_ref_exists_sql(source_refs_column)}", (source_item_ids,))


def _delete_source_refs(conn: Any, table: str, source_item_ids: list[str]) -> int:
    return _delete_where(conn, f"delete from {table} where {_source_ref_exists_sql()}", (source_item_ids,))


def _count_text_refs(conn: Any, table: str, column: str, source_item_ids: list[str]) -> int:
    return _count_sql(
        conn,
        f"""
        select count(*) from {table}
        where exists (
          select 1 from unnest(%s::text[]) sid
          where position(sid in coalesce({column}::text, '')) > 0
        )
        """,
        (source_item_ids,),
    )


def _delete_text_refs(conn: Any, table: str, column: str, source_item_ids: list[str]) -> int:
    return _delete_where(
        conn,
        f"""
        delete from {table}
        where exists (
          select 1 from unnest(%s::text[]) sid
          where position(sid in coalesce({column}::text, '')) > 0
        )
        """,
        (source_item_ids,),
    )


def _pending_review_item_ids_for_purge(conn: Any, source_item_ids: list[str]) -> list[str]:
    rows = conn.execute(
        """
        select review_item_id
        from review_items
        where status in ('pending', 'new')
          and exists (
            select 1 from jsonb_array_elements(coalesce(proposal->'source_refs', '[]'::jsonb)) ref
            where ref->>'source_item_id' = any(%s)
          )
        """,
        (source_item_ids,),
    ).fetchall()
    return [str(row["review_item_id"]) for row in rows]


def _delete_artifact_supports_for_purge(conn: Any, source_item_ids: list[str], review_item_ids: list[str]) -> int:
    return _delete_where(
        conn,
        """
        delete from artifact_supports
        where source_item_id = any(%s)
           or (artifact_type = 'review_item' and artifact_id = any(%s))
        """,
        (source_item_ids, review_item_ids),
    )


def _mark_review_items_stale_for_purge(conn: Any, source_item_ids: list[str]) -> int:
    rows = conn.execute(
        """
        update review_items
        set proposal = jsonb_set(
              coalesce(proposal, '{}'::jsonb),
              '{lifecycle}',
              %s,
              true
            )
        where status not in ('pending', 'new')
          and exists (
            select 1 from jsonb_array_elements(coalesce(proposal->'source_refs', '[]'::jsonb)) ref
            where ref->>'source_item_id' = any(%s)
          )
        returning review_item_id
        """,
        (Jsonb({"status": "stale", "reason": "evidence_removed", "source_item_ids": source_item_ids}), source_item_ids),
    ).fetchall()
    return len(rows)


def _orphan_entity_ids_after_source_cleanup(conn: Any, source_item_ids: list[str]) -> list[str]:
    rows = conn.execute(
        """
        with affected_hyperedges as (
          select hyperedge_id
          from hyperedges
          where exists (
            select 1
            from jsonb_array_elements(coalesce(source_refs, '[]'::jsonb)) ref
            where ref->>'source_item_id' = any(%s)
          )
        ),
        affected_entities as (
          select distinct entity_id
          from hyperedge_members
          where hyperedge_id in (select hyperedge_id from affected_hyperedges)
        )
        select entity_id
        from affected_entities ae
        where not exists (
          select 1
          from hyperedge_members hm
          join hyperedges h on h.hyperedge_id = hm.hyperedge_id
          where hm.entity_id = ae.entity_id
            and hm.hyperedge_id not in (select hyperedge_id from affected_hyperedges)
        )
        """,
        (source_item_ids,),
    ).fetchall()
    return [str(row["entity_id"]) for row in rows]


def _delete_offline_index_states(conn: Any, source_item_ids: list[str]) -> int:
    return _delete_where(
        conn,
        """
        delete from offline_index_states
        where source_item_id = any(%s)
           or object_id = any(%s)
           or object_id in (select document_id from documents where source_item_id = any(%s))
           or object_id in (select chunk_id from chunks where source_item_id = any(%s))
        """,
        (source_item_ids, source_item_ids, source_item_ids, source_item_ids),
    )


def _delete_by_ids(conn: Any, table: str, id_column: str, ids: list[str]) -> int:
    if not ids:
        return 0
    return _delete_where(conn, f"delete from {table} where {id_column} = any(%s)", (ids,))


def _delete_where(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    cursor = conn.execute(sql, params)
    return int(cursor.rowcount or 0)


def _count_sql(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int((row or {}).get("count") or 0)


def _remove_root_from_connector_state(conn: Any, root: str) -> int:
    rows = conn.execute("select connector_state_id, permission_scope, config from connector_states where connector_id = 'files'").fetchall()
    updated = 0
    for row in rows:
        permission_scope = dict(row.get("permission_scope") or {})
        roots = [item for item in permission_scope.get("roots", []) if item != root]
        config = dict(row.get("config") or {})
        manifests = dict(config.get("files_manifests_by_root") or {})
        missing = dict(config.get("files_missing_by_root") or {})
        if root not in permission_scope.get("roots", []) and root not in manifests and root not in missing:
            continue
        permission_scope["roots"] = roots
        manifests.pop(root, None)
        missing.pop(root, None)
        config["files_manifests_by_root"] = manifests
        config["files_missing_by_root"] = missing
        conn.execute(
            """
            update connector_states
            set permission_scope = %s, config = %s, updated_at = now()
            where connector_state_id = %s
            """,
            (Jsonb(permission_scope), Jsonb(config), row["connector_state_id"]),
        )
        updated += 1
    return updated


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _console_recommended_commands(*, pending_review_count: int, failed_job_count: int, digest_backlog_count: int) -> list[str]:
    commands = ["./scripts/pska daily-status", "./scripts/pska daily-briefing"]
    if pending_review_count:
        commands.append("./scripts/pska review-list --status pending --summary")
    if failed_job_count:
        commands.append("./scripts/pska jobs list --status failed")
    if digest_backlog_count == 0:
        commands.append("./scripts/pska digest-schedule --owner-user-id user_primary --limit 5")
    return commands


def _console_next_actions(*, pending_review_count: int, failed_job_count: int, digest_backlog_count: int) -> list[str]:
    actions: list[str] = []
    if pending_review_count:
        actions.append(f"Review {pending_review_count} pending candidate(s).")
    if failed_job_count:
        actions.append(f"Inspect {failed_job_count} failed job(s).")
    if digest_backlog_count:
        actions.append(f"Let the digest worker process {digest_backlog_count} queued digest job(s).")
    if not actions:
        actions.append("PSKA is ready; run a search or schedule a small digest batch when new sources arrive.")
    return actions


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _owner_user_id_for_write(payload: dict[str, Any], context: RequestContext | None) -> str:
    if context and context.caller == "agent_service":
        return context.represented_user_id or context.effective_user_id
    if payload.get("owner_user_id"):
        return str(payload["owner_user_id"])
    if context and context.represented_user_id:
        return context.represented_user_id
    if context:
        return context.effective_user_id
    return "user_primary"


def _actor_user_id(context: RequestContext | None, payload: dict[str, Any]) -> str:
    return str(payload.get("actor_user_id") or (context.user_id if context else None) or payload.get("user_id") or payload.get("owner_user_id") or "user_primary")


def _allowed_tools_for_job(job) -> list[str]:
    if job.job_type in {"digest_via_fastreact", "extract_via_fastreact"}:
        return ["pska_job_context", "pska_search", "pska_write_candidates", "pska_review_items"]
    return ["pska_job_context", "pska_search", "pska_write_candidates"]


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def _query_string_list(query: dict[str, list[str]], key: str) -> list[str]:
    values: list[str] = []
    for raw in query.get(key) or []:
        values.extend(part.strip() for part in str(raw).split(",") if part.strip())
    return list(dict.fromkeys(values))


def _writing_path_parts(path: str) -> list[str]:
    prefix = "/workspace/writing/boards/"
    if not path.startswith(prefix):
        return []
    return [part for part in path.removeprefix(prefix).split("/") if part]


def _ask_conversation_path_parts(path: str) -> list[str]:
    prefix = "/workspace/ask/conversations/"
    if not path.startswith(prefix):
        return []
    return [part for part in path.removeprefix(prefix).split("/") if part]


def _knowledge_base_path_parts(path: str) -> list[str]:
    prefix = "/workspace/knowledge-bases/"
    if not path.startswith(prefix):
        return []
    return [part for part in path.removeprefix(prefix).split("/") if part]


def _int_first(values: list[str] | None) -> int | None:
    value = _first(values)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _node_types_param(value: str | None) -> set[str] | None:
    if not value:
        return None
    node_types = {item.strip() for item in value.split(",") if item.strip()}
    return node_types or None


def _parse_multipart_payload(content_type: str, raw: bytes) -> dict[str, Any]:
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
    )
    payload: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    for part in message.iter_parts():
        disposition = part.get("content-disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename:
            file_payload = {
                "field": name,
                "filename": filename,
                "content_type": part.get_content_type() or "application/octet-stream",
                "size_bytes": len(data),
                "bytes_base64": base64.b64encode(data).decode("ascii"),
            }
            files.append(file_payload)
            payload.setdefault("file", file_payload)
            payload.setdefault("filename", filename)
            payload.setdefault("content_type", file_payload["content_type"])
            payload.setdefault("size_bytes", len(data))
        else:
            charset = part.get_content_charset() or "utf-8"
            payload[str(name)] = data.decode(charset, errors="replace")
    if files:
        payload["files"] = files
    return payload


def _float_first(values: list[str] | None, default: float) -> float:
    value = _first(values)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _cursor_offset(value: str | int | None) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _batch_limit(value: Any) -> int:
    try:
        limit = int(value) if value is not None else 20
    except (TypeError, ValueError):
        limit = 20
    return min(max(limit, 1), 100)


def _workspace_digest_worker_runs(
    api: PSKAApi,
    payload: dict[str, Any],
    *,
    scheduled: dict[str, Any],
    tenant_id: str,
    owner_user_id: str,
) -> list[dict[str, Any]]:
    scheduled_job = scheduled.get("job") if isinstance(scheduled.get("job"), dict) else None
    job_id = str(scheduled_job.get("job_id") or "").strip() if scheduled_job else ""
    if not job_id:
        return [{"ok": True, "processed": 0, "stage": "fastreact_worker", "reason": "no_digest_job_scheduled"}]
    max_worker_runs = max(0, min(int(payload.get("max_worker_runs") if payload.get("max_worker_runs") is not None else 1), 10))
    if max_worker_runs <= 0:
        return [{"ok": True, "processed": 0, "stage": "fastreact_worker", "reason": "worker_disabled"}]
    try:
        from pska_core.cli import _default_fastreact_root, _run_fastreact_digest_worker

        fastreact_root_value = payload.get("fastreact_root")
        args = argparse.Namespace(
            database_url=getattr(api.store, "database_url", api.config.database.url),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            job_id=job_id,
            pska_url=payload.get("pska_url"),
            fastreact_url=payload.get("fastreact_url"),
            fastreact_root=Path(str(fastreact_root_value)).expanduser() if fastreact_root_value else _default_fastreact_root(),
            python=str(payload.get("python") or "python3"),
            batch_size=_batch_limit(payload.get("batch_size") or len(scheduled.get("scheduled_source_item_ids") or []) or 1),
            max_worker_runs=max_worker_runs,
            worker_timeout_seconds=float(payload.get("worker_timeout_seconds") or 300.0),
        )
        return _run_fastreact_digest_worker(args, api.config)
    except Exception as exc:  # noqa: BLE001 - product API should expose worker diagnostics.
        return [
            {
                "ok": False,
                "processed": 0,
                "stage": "fastreact_worker",
                "error": f"{type(exc).__name__}: {exc}",
            }
        ]


def _workspace_digest_worker_status(worker_runs: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics: list[str] = []
    processed = 0
    failed = 0
    for run in worker_runs:
        processed += int(run.get("processed") or 0)
        if run.get("ok") is False:
            failed += 1
            diagnostics.append(str(run.get("error") or run.get("reason") or "worker_failed"))
        elif run.get("reason"):
            diagnostics.append(str(run.get("reason")))
    return {
        "requested": bool(worker_runs),
        "ok": failed == 0,
        "processed": processed,
        "failed_runs": failed,
        "diagnostics": list(dict.fromkeys(diagnostics)),
    }


_WORKSPACE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Workspace</title>
  <link rel="stylesheet" href="/workspace/app.css">
</head>
<body>
  <header class="workspace-topbar">
    <div class="brand">
      <p class="eyebrow">User Workspace</p>
      <h1>PSKA</h1>
    </div>
    <nav class="mode-tabs" aria-label="Workspace modes">
      <a href="#chat" aria-current="page">Chat</a>
      <a href="#corpus">Corpus</a>
      <a href="#writer">Writer</a>
      <a href="#evidence">Evidence</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit">Apply</button>
    </form>
  </header>
  <main class="workspace-shell">
    <aside id="corpus" class="rail corpus-rail" aria-label="Corpus">
      <div class="panel-head">
        <p class="eyebrow">Corpus</p>
        <strong>Sources</strong>
      </div>
      <div class="corpus-actions">
        <a href="/console/sources">Sources</a>
        <a href="/console/memory">Memory</a>
        <a href="/console/search">Advanced Search</a>
      </div>
      <form id="corpus-form" class="corpus-form">
        <label for="corpus-query">Find</label>
        <input id="corpus-query" type="search" autocomplete="off" placeholder="Filter text">
        <label for="corpus-channel">Channel</label>
        <select id="corpus-channel">
          <option value="">All channels</option>
        </select>
        <label for="corpus-limit">Limit</label>
        <input id="corpus-limit" type="number" min="1" max="100" value="20">
        <button type="submit">Load</button>
      </form>
      <section>
        <h2>Retrieved Sources</h2>
        <div id="corpus-list" class="stack"></div>
      </section>
      <section>
        <h2>Explorer Chunks</h2>
        <div id="corpus-chunks" class="stack"></div>
      </section>
      <section>
        <h2>Graph Evidence</h2>
        <div id="corpus-graph" class="stack"></div>
      </section>
      <section>
        <h2>Memory / Profile</h2>
        <div id="corpus-memory" class="stack"></div>
      </section>
    </aside>

    <section id="chat" class="chat-column" aria-label="Chat">
      <div id="status" class="status-line">Ready for direct retrieval.</div>
      <div id="messages" class="messages" aria-live="polite">
        <article class="message assistant">
          <strong>PSKA</strong>
          <p>Ask in Chinese or English. Direct retrieval works locally; agentic mode will report clearly if it cannot run.</p>
        </article>
      </div>
      <form id="chat-form" class="composer">
        <label for="query">Query</label>
        <textarea id="query" rows="3" required placeholder="例如：最近和 PSKA workspace 相关的资料有哪些？"></textarea>
        <div class="composer-row">
          <label class="toggle"><input id="agentic" type="checkbox"> Agentic</label>
          <label class="toggle"><input id="capture" type="checkbox"> Capture</label>
          <button type="submit">Send</button>
        </div>
      </form>
    </section>

    <aside id="evidence" class="rail evidence-rail" aria-label="Evidence inspector">
      <div class="panel-head">
        <p class="eyebrow">Evidence</p>
        <strong>Context Inspector</strong>
      </div>
      <section>
        <h2>Citations</h2>
        <div id="citations" class="stack"></div>
      </section>
      <section>
        <h2>Graph</h2>
        <div id="graph-paths" class="stack"></div>
      </section>
      <section>
        <h2>Memory / Profile</h2>
        <div id="memory-profile" class="stack"></div>
      </section>
      <section>
        <h2>Gaps / Conflicts</h2>
        <div id="gaps-conflicts" class="stack"></div>
      </section>
    </aside>
  </main>

  <section id="writer" class="writer-band" aria-label="Writer">
    <div>
      <p class="eyebrow">Writer</p>
      <h2>Draft Space</h2>
      <div class="writer-actions">
        <button id="capture-selection" type="button">Use selection</button>
        <button id="writer-suggest" type="button">Suggest</button>
      </div>
    </div>
    <div class="writer-main">
      <div id="draft" class="draft-editor" contenteditable="true" role="textbox" aria-multiline="true" data-placeholder="Write here. Select text, capture it, then ask PSKA for Chinese writing suggestions."></div>
      <form id="writer-form" class="writer-form">
        <label for="selected-text">Selected text</label>
        <textarea id="selected-text" rows="3" placeholder="Select text in the draft, then use selection."></textarea>
        <label for="writer-instruction">Request</label>
        <input id="writer-instruction" type="text" value="请基于 PSKA 证据给出中文写作建议，并指出需要引用的资料。">
      </form>
      <div class="writer-output">
        <section>
          <h2>Suggestions</h2>
          <div id="writer-suggestions" class="stack"></div>
        </section>
        <section>
          <h2>Writer Evidence</h2>
          <div id="writer-evidence" class="stack"></div>
        </section>
      </div>
    </div>
  </section>
  <p id="error" role="alert"></p>
  <script src="/workspace/app.js"></script>
</body>
</html>
"""


_WORKSPACE_CSS = """
:root {
  color-scheme: light;
  --bg: #f5f6f0;
  --panel: #ffffff;
  --ink: #202522;
  --muted: #657168;
  --line: #d9ded6;
  --accent: #236c61;
  --accent-weak: #e3f0ec;
  --warn: #a44224;
  --shadow: 0 8px 22px rgba(33, 39, 35, 0.08);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 34px;
  line-height: 1;
  letter-spacing: 0;
}

h2 {
  font-size: 15px;
  line-height: 1.25;
}

button,
a {
  border-radius: 6px;
}

button {
  min-width: 44px;
  min-height: 40px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: white;
  font: inherit;
  font-weight: 800;
  cursor: pointer;
}

.workspace-topbar {
  display: grid;
  grid-template-columns: auto minmax(280px, 1fr) auto;
  align-items: center;
  gap: 18px;
  padding: 16px clamp(16px, 3vw, 36px);
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

.eyebrow {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

.mode-tabs,
.corpus-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.mode-tabs a,
.corpus-actions a {
  min-height: 38px;
  padding: 9px 11px;
  border: 1px solid var(--line);
  color: var(--ink);
  font-weight: 800;
  text-decoration: none;
}

.mode-tabs a[aria-current="page"] {
  border-color: var(--accent);
  background: var(--accent-weak);
  color: var(--accent);
}

.token-form {
  display: flex;
  align-items: end;
  gap: 8px;
}

.token-form label,
.composer label:first-child {
  display: grid;
  gap: 5px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

input,
textarea,
select {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: white;
  color: var(--ink);
  font: inherit;
}

.token-form input {
  width: min(230px, 24vw);
  min-height: 40px;
  padding: 8px 10px;
}

.corpus-form {
  display: grid;
  gap: 8px;
}

.corpus-form label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.corpus-form input,
.corpus-form select {
  width: 100%;
  min-height: 38px;
  padding: 8px 10px;
}

.workspace-shell {
  display: grid;
  grid-template-columns: minmax(210px, 280px) minmax(360px, 1fr) minmax(250px, 340px);
  align-items: start;
  gap: 16px;
  width: min(1480px, calc(100% - 32px));
  margin: 16px auto;
}

.rail,
.chat-column,
.writer-band {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
}

.rail {
  display: grid;
  align-content: start;
  gap: 18px;
  min-height: 620px;
  max-height: calc(100vh - 112px);
  overflow: auto;
  padding: 16px;
}

.panel-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line);
}

.stack {
  display: grid;
  gap: 10px;
  margin-top: 10px;
}

.mini-card,
.message {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: white;
}

.mini-card p,
.message p {
  margin-top: 8px;
  color: var(--muted);
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.message details {
  margin-top: 10px;
  border-top: 1px solid var(--line);
  padding-top: 8px;
}

.message summary {
  cursor: pointer;
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
}

.message pre {
  max-height: 360px;
  overflow: auto;
  margin-top: 8px;
  padding: 10px;
  border-radius: 6px;
  background: #101820;
  color: #ecf4f1;
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
}

.mini-card .meta {
  display: block;
  margin-top: 7px;
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.chat-column {
  display: grid;
  grid-template-rows: auto minmax(360px, 1fr) auto;
  min-height: 620px;
  max-height: calc(100vh - 112px);
  overflow: hidden;
}

.status-line {
  min-height: 44px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-weight: 800;
}

.status-line.warn {
  color: var(--warn);
}

.messages {
  display: grid;
  align-content: start;
  gap: 12px;
  overflow: auto;
  padding: 16px;
}

.message.user {
  margin-left: min(96px, 16%);
  background: var(--accent-weak);
}

.message.assistant {
  margin-right: min(96px, 16%);
}

.composer {
  display: grid;
  gap: 10px;
  padding: 14px;
  border-top: 1px solid var(--line);
  background: #fbfcf8;
}

.composer textarea,
.writer-band textarea {
  width: 100%;
  resize: vertical;
  padding: 10px 12px;
  line-height: 1.45;
}

.composer-row {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 40px;
  font-weight: 800;
}

.writer-band {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 18px;
  width: min(1480px, calc(100% - 32px));
  margin: 0 auto 32px;
  padding: 16px;
}

.writer-actions {
  display: grid;
  gap: 8px;
  margin-top: 16px;
}

.writer-main {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.draft-editor {
  min-height: 170px;
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: white;
  line-height: 1.55;
  outline: none;
  white-space: pre-wrap;
}

.draft-editor:empty::before {
  color: var(--muted);
  content: attr(data-placeholder);
}

.writer-form {
  display: grid;
  gap: 8px;
}

.writer-form label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.writer-form input {
  width: 100%;
  min-height: 40px;
  padding: 8px 10px;
}

.writer-output {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

#error {
  width: min(1480px, calc(100% - 32px));
  min-height: 24px;
  margin: 0 auto 24px;
  color: var(--warn);
  font-weight: 800;
}

@media (max-width: 1100px) {
  .workspace-topbar,
  .workspace-shell,
  .writer-band,
  .writer-output {
    grid-template-columns: 1fr;
  }

  .rail,
  .chat-column {
    max-height: none;
    min-height: auto;
  }

  .token-form {
    align-items: stretch;
    flex-direction: column;
  }

  .token-form input {
    width: 100%;
  }
}
"""


_WORKSPACE_JS = """
const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const setError = (message) => {
  document.getElementById("error").textContent = message || "";
};

const headers = () => {
  const result = {"accept": "application/json", "content-type": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    result["X-PSKA-Service-Token"] = token;
  }
  return result;
};

const text = (value) => value === null || value === undefined || value === "" ? "-" : String(value);

const miniCard = (title, body, meta = "") => {
  const card = document.createElement("article");
  card.className = "mini-card";
  const strong = document.createElement("strong");
  strong.textContent = text(title);
  const paragraph = document.createElement("p");
  paragraph.textContent = text(body);
  card.append(strong, paragraph);
  if (meta) {
    const small = document.createElement("span");
    small.className = "meta";
    small.textContent = meta;
    card.appendChild(small);
  }
  return card;
};

const renderStack = (id, values, render, emptyText) => {
  const element = document.getElementById(id);
  element.replaceChildren();
  if (!values || !values.length) {
    element.appendChild(miniCard("None", emptyText || "No evidence returned."));
    return;
  }
  for (const value of values) {
    element.appendChild(render(value));
  }
};

const addMessage = (role, body, title = role === "user" ? "You" : "PSKA", detailsPayload = null) => {
  const messages = document.getElementById("messages");
  const message = document.createElement("article");
  message.className = `message ${role}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  const paragraph = document.createElement("p");
  paragraph.textContent = body;
  message.append(strong, paragraph);
  if (detailsPayload) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = detailsPayload.title || "Details";
    const pre = document.createElement("pre");
    pre.textContent = typeof detailsPayload.body === "string" ? detailsPayload.body : JSON.stringify(detailsPayload.body, null, 2);
    details.append(summary, pre);
    message.appendChild(details);
  }
  messages.appendChild(message);
  messages.scrollTop = messages.scrollHeight;
};

const activeRetrieval = (payload) => {
  if (payload.retrieval) {
    return payload.retrieval;
  }
  return payload.fallback?.retrieval || {};
};

const traceEvents = (payload) => {
  const events = payload.trace?.events;
  return Array.isArray(events) ? events : [];
};

const finalAnswerFromEvents = (payload) => {
  const events = traceEvents(payload);
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.type === "session_end") {
      return event.content || event.final_content || event.answer || event.metadata?.final_content || event.metadata?.final || event.metadata?.answer || "";
    }
  }
  return "";
};

const renderToolTrace = (payload) => {
  const events = traceEvents(payload);
  const calls = events.filter((event) => event?.type === "tool_call");
  if (!calls.length) {
    return;
  }
  const lines = calls.slice(0, 8).map((call) => {
    const result = [...events].reverse().find((event) =>
      event?.type === "tool_result" &&
      ((call.tool_call_id && event.tool_call_id === call.tool_call_id) || (!call.tool_call_id && event.tool_name === call.tool_name))
    );
    const status = result?.metadata?.error || result?.metadata?.is_error ? "failed" : (result ? "complete" : "running");
    return `${call.tool_name || "tool"}: ${status}`;
  });
  addMessage("assistant", lines.join("\\n"), "FastReAct tool trace", {title: "Raw FastReAct events", body: events});
};

const renderEvidence = (payload) => {
  const retrieval = activeRetrieval(payload);
  const evidence = payload.workspace?.evidence || {};
  const citations = evidence.citations || retrieval.citations || [];
  const graphPaths = evidence.graph_paths || retrieval.graph_paths || [];
  const memory = [...(evidence.memory_context || []), ...(evidence.profile_context || [])];
  const gapsConflicts = [...(evidence.gaps || []), ...(evidence.conflicts || [])];

  renderStack("corpus-list", citations, (item) => miniCard(item.title || item.source_item_id, item.snippet, [item.source_item_id, item.chunk_id].filter(Boolean).join(" / ")), "Run a chat query to populate source refs.");
  renderStack("citations", citations, (item) => miniCard(item.title || item.source_item_id, item.snippet, [item.source_item_id, item.chunk_id].filter(Boolean).join(" / ")), "No citations.");
  renderStack("graph-paths", graphPaths, (path) => miniCard(path.explanation || (path.entities || []).join(" -> "), `${path.grounded_edges || 0}/${path.edge_count || 0} grounded edges`, `depth ${text(path.depth)}`), "No graph paths.");
  renderStack("memory-profile", memory, (item) => miniCard("Context", item.text, `confidence ${text(item.confidence)} / citations ${text(item.citation_count)}`), "No memory or profile context.");
  renderStack("gaps-conflicts", gapsConflicts, (value) => miniCard("Attention", value), "No gaps or conflicts reported.");
};

const renderResponse = (payload) => {
  const status = payload.workspace?.chat_status || {};
  const statusEl = document.getElementById("status");
  statusEl.textContent = status.message || "Search complete.";
  statusEl.className = `status-line ${payload.ok ? "" : "warn"}`;
  const displayMode = payload.display_mode || status.display_mode || "";
  const retrieval = activeRetrieval(payload);
  const results = retrieval.results || [];
  const fallbackSummary = results.length
    ? "Agentic service did not return a usable grounded answer. Direct retrieval found source refs below."
    : "Agentic service did not return a usable grounded answer, and direct retrieval found no matching evidence.";
  const eventAnswer = finalAnswerFromEvents(payload);
  const summary = displayMode === "direct_fallback"
    ? fallbackSummary
    : eventAnswer || payload.answer || (results.length ? results.map((item) => item.snippet || item.title).filter(Boolean).slice(0, 3).join("\\n\\n") : "No matching evidence was found.");
  const title = displayMode === "direct_fallback" ? "PSKA Direct fallback" : (payload.mode === "agentic" ? "PSKA Agentic" : "PSKA Direct");
  renderToolTrace(payload);
  addMessage("assistant", summary, title, {title: "Raw PSKA response", body: payload});
  renderEvidence(payload);
};

const updateChannelOptions = (channels, selected) => {
  const select = document.getElementById("corpus-channel");
  const existing = new Set([...select.options].map((option) => option.value));
  for (const channel of channels || []) {
    if (existing.has(channel)) {
      continue;
    }
    const option = document.createElement("option");
    option.value = channel;
    option.textContent = channel;
    select.appendChild(option);
  }
  select.value = selected || "";
};

const renderCorpus = (payload) => {
  updateChannelOptions(payload.filters?.available_source_channels || [], payload.filters?.source_channel);
  renderStack("corpus-list", payload.sources || [], (item) => miniCard(item.title || item.source_item_id, item.snippet, [item.source_channel, item.record_type, item.created_at, `${item.chunk_count || 0} chunk(s)`].filter(Boolean).join(" / ")), "No source items match the current filters.");
  renderStack("corpus-chunks", payload.chunks || [], (item) => miniCard(item.chunk_id, item.snippet, [item.source_item_id, item.document_id, `ordinal ${text(item.ordinal)}`].filter(Boolean).join(" / ")), "No chunks match the current filters.");
  renderStack("corpus-graph", payload.hyperedges || [], (edge) => {
    const members = (edge.members || []).map((member) => `${member.role}:${member.label}`).join(" -> ");
    return miniCard(edge.relation_type || edge.hyperedge_id, edge.evidence_text || members, [`confidence ${text(edge.confidence)}`, `${(edge.source_refs || []).length} source ref(s)`].join(" / "));
  }, "No graph evidence yet.");
  const memoryCards = [
    ...(payload.memories || []).map((item) => ({title: item.layer || item.agent_memory_id, body: item.text, meta: `confidence ${text(item.confidence)} / ${item.source_ref_status}`})),
    ...(payload.profiles || []).map((item) => ({title: item.profile_card_id, body: JSON.stringify(item.profile), meta: `confidence ${text(item.confidence)} / ${item.source_ref_status}`})),
  ];
  renderStack("corpus-memory", memoryCards, (item) => miniCard(item.title, item.body, item.meta), "No memory or profile records yet.");
};

const renderWriterSuggestion = (payload) => {
  const suggestion = payload.suggestion || {};
  const bullets = suggestion.bullets || [];
  const cards = [
    {title: "中文建议", body: suggestion.summary || "No suggestion.", meta: `query ${payload.query_context?.query || "-"}`},
    {title: "建议改写", body: suggestion.proposed_text || "-", meta: `language ${suggestion.language || payload.default_language || "zh"}`},
    ...bullets.map((bullet, index) => ({title: `要点 ${index + 1}`, body: bullet, meta: ""})),
  ];
  renderStack("writer-suggestions", cards, (item) => miniCard(item.title, item.body, item.meta), "Select draft text and request suggestions.");
  const evidence = payload.evidence || {};
  const evidenceCards = [
    ...(evidence.citations || []).map((item) => ({title: item.title || item.source_item_id, body: item.snippet, meta: [item.source_item_id, item.chunk_id].filter(Boolean).join(" / ")})),
    ...(evidence.graph_paths || []).map((item) => ({title: "Graph path", body: item.explanation || (item.entities || []).join(" -> "), meta: `${item.grounded_edges || 0}/${item.edge_count || 0} grounded`})),
    ...(evidence.memory_context || []).map((item) => ({title: "Memory", body: item.text, meta: `confidence ${text(item.confidence)}`})),
    ...(evidence.profile_context || []).map((item) => ({title: "Profile", body: item.text, meta: `confidence ${text(item.confidence)}`})),
    ...(evidence.gaps || []).map((item) => ({title: "Gap", body: item, meta: ""})),
    ...(evidence.conflicts || []).map((item) => ({title: "Conflict", body: item, meta: ""})),
  ];
  renderStack("writer-evidence", evidenceCards, (item) => miniCard(item.title, item.body, item.meta), "No writer evidence returned.");
};

const loadCorpus = async () => {
  const params = new URLSearchParams({
    owner_user_id: "user_primary",
    limit: document.getElementById("corpus-limit").value || "20",
  });
  const query = document.getElementById("corpus-query").value.trim();
  const channel = document.getElementById("corpus-channel").value;
  if (query) {
    params.set("query", query);
  }
  if (channel) {
    params.set("source_channel", channel);
  }
  const response = await fetch(`/workspace/corpus/data?${params.toString()}`, {headers: headers()});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  renderCorpus(payload);
};

const draft = document.getElementById("draft");
draft.innerHTML = sessionStorage.getItem("pska_workspace_draft") || "";
draft.addEventListener("input", () => {
  sessionStorage.setItem("pska_workspace_draft", draft.innerHTML);
});

const selectedTextInput = document.getElementById("selected-text");

const captureSelection = () => {
  const selection = window.getSelection();
  const selected = selection ? selection.toString().trim() : "";
  if (selected) {
    selectedTextInput.value = selected;
    sessionStorage.setItem("pska_workspace_selected_text", selected);
  }
  return selectedTextInput.value.trim();
};

selectedTextInput.value = sessionStorage.getItem("pska_workspace_selected_text") || "";
selectedTextInput.addEventListener("input", () => {
  sessionStorage.setItem("pska_workspace_selected_text", selectedTextInput.value);
});

const writerSuggest = async () => {
  const selectedText = selectedTextInput.value.trim() || captureSelection();
  const payload = {
    selected_text: selectedText,
    draft_text: draft.textContent || "",
    instruction: document.getElementById("writer-instruction").value,
    user_id: "user_primary",
    represented_user_id: "user_primary",
    top_k: 5,
  };
  const response = await fetch("/workspace/writer/suggest", {method: "POST", headers: headers(), body: JSON.stringify(payload)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  renderWriterSuggestion(data);
};

const search = async (query) => {
  const payload = {
    query,
    mode: document.getElementById("agentic").checked ? "agentic" : "direct",
    capture: document.getElementById("capture").checked,
    user_id: "user_primary",
    represented_user_id: "user_primary",
    top_k: 5,
  };
  const response = await fetch("/workspace/search/query", {method: "POST", headers: headers(), body: JSON.stringify(payload)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  setError("");
  loadCorpus().catch((error) => setError(error.message));
});

document.getElementById("corpus-form").addEventListener("submit", (event) => {
  event.preventDefault();
  setError("");
  loadCorpus().catch((error) => setError(error.message));
});

document.getElementById("capture-selection").addEventListener("click", () => {
  captureSelection();
});

document.getElementById("writer-suggest").addEventListener("click", () => {
  setError("");
  writerSuggest().catch((error) => setError(error.message));
});

document.getElementById("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  const queryInput = document.getElementById("query");
  const query = queryInput.value.trim();
  if (!query) {
    return;
  }
  addMessage("user", query);
  queryInput.value = "";
  document.getElementById("status").textContent = "Searching PSKA...";
  try {
    renderResponse(await search(query));
  } catch (error) {
    document.getElementById("status").textContent = "Search needs attention.";
    document.getElementById("status").className = "status-line warn";
    setError(error.message);
  }
});

loadCorpus().catch((error) => setError(error.message));
"""


_CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Console</title>
  <link rel="stylesheet" href="/console/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Local Web Console</p>
      <h1>PSKA</h1>
    </div>
    <nav class="nav-links" aria-label="Console pages">
      <a href="/console">Home</a>
      <a href="/console/reviews">Reviews</a>
      <a href="/console/search">Search</a>
      <a href="/console/memory">Memory</a>
      <a href="/console/jobs">Jobs</a>
      <a href="/console/sources">Sources</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit" aria-label="Apply service token">Apply</button>
    </form>
    <button id="refresh" type="button" aria-label="Refresh dashboard">Refresh</button>
  </header>
  <main>
    <section class="summary">
      <div>
        <p class="label">Service</p>
        <strong id="status">Loading</strong>
      </div>
      <div>
        <p class="label">Sources</p>
        <strong id="sources">-</strong>
      </div>
      <div>
        <p class="label">Chunks</p>
        <strong id="chunks">-</strong>
      </div>
      <div>
        <p class="label">Digest Jobs</p>
        <strong id="digest">-</strong>
      </div>
      <div>
        <p class="label">Pending Reviews</p>
        <strong id="reviews">-</strong>
      </div>
      <div>
        <p class="label">Failed Jobs</p>
        <strong id="failed">-</strong>
      </div>
    </section>

    <section class="grid">
      <article>
        <h2>Readiness</h2>
        <div id="readiness" class="checks"></div>
      </article>
      <article>
        <h2>Next Actions</h2>
        <ul id="actions" class="list"></ul>
      </article>
      <article>
        <h2>Recent Sources</h2>
        <ul id="recent-sources" class="list"></ul>
      </article>
      <article>
        <h2>Recommended Commands</h2>
        <ul id="commands" class="commands"></ul>
      </article>
    </section>
  </main>
  <p id="error" role="alert"></p>
  <script src="/console/app.js"></script>
</body>
</html>
"""


_CONSOLE_REVIEWS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Reviews</title>
  <link rel="stylesheet" href="/console/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Local Web Console</p>
      <h1>Reviews</h1>
    </div>
    <nav class="nav-links" aria-label="Console pages">
      <a href="/console">Home</a>
      <a href="/console/reviews">Reviews</a>
      <a href="/console/search">Search</a>
      <a href="/console/memory">Memory</a>
      <a href="/console/jobs">Jobs</a>
      <a href="/console/sources">Sources</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit" aria-label="Apply service token">Apply</button>
    </form>
    <button id="refresh" type="button" aria-label="Refresh reviews">Refresh</button>
  </header>
  <main>
    <section class="review-head">
      <div>
        <p class="label">Pending Reviews</p>
        <strong id="review-count">-</strong>
      </div>
      <div>
        <p class="label">Owner</p>
        <strong id="review-owner">user_primary</strong>
      </div>
    </section>
    <section id="reviews" class="review-list" aria-live="polite"></section>
  </main>
  <p id="error" role="alert"></p>
  <script src="/console/reviews.js"></script>
</body>
</html>
"""


_CONSOLE_SEARCH_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Search</title>
  <link rel="stylesheet" href="/console/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Local Web Console</p>
      <h1>Search</h1>
    </div>
    <nav class="nav-links" aria-label="Console pages">
      <a href="/console">Home</a>
      <a href="/console/reviews">Reviews</a>
      <a href="/console/search">Search</a>
      <a href="/console/memory">Memory</a>
      <a href="/console/jobs">Jobs</a>
      <a href="/console/sources">Sources</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit" aria-label="Apply service token">Apply</button>
    </form>
  </header>
  <main>
    <form id="search-form" class="search-form">
      <label class="query-label" for="query">Query</label>
      <input id="query" name="query" type="search" autocomplete="off" required>
      <label class="option"><input id="agentic" type="checkbox"> Agentic</label>
      <label class="option"><input id="capture" type="checkbox"> Capture</label>
      <button type="submit" class="primary">Search</button>
    </form>
    <section id="search-status" class="search-status"></section>
    <section id="search-results" class="search-results"></section>
  </main>
  <p id="error" role="alert"></p>
  <script src="/console/search.js"></script>
</body>
</html>
"""


_CONSOLE_MEMORY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Memory</title>
  <link rel="stylesheet" href="/console/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Local Web Console</p>
      <h1>Memory</h1>
    </div>
    <nav class="nav-links" aria-label="Console pages">
      <a href="/console">Home</a>
      <a href="/console/reviews">Reviews</a>
      <a href="/console/search">Search</a>
      <a href="/console/memory">Memory</a>
      <a href="/console/jobs">Jobs</a>
      <a href="/console/sources">Sources</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit" aria-label="Apply service token">Apply</button>
    </form>
    <button id="refresh" type="button" aria-label="Refresh memory">Refresh</button>
  </header>
  <main>
    <section class="review-head">
      <div>
        <p class="label">Agent Memories</p>
        <strong id="memory-count">-</strong>
      </div>
      <div>
        <p class="label">Profile Cards</p>
        <strong id="profile-count">-</strong>
      </div>
    </section>
    <section class="memory-grid">
      <div>
        <h2>Agent Memories</h2>
        <div id="agent-memories" class="memory-list"></div>
      </div>
      <div>
        <h2>Profile Cards</h2>
        <div id="profile-cards" class="memory-list"></div>
      </div>
    </section>
  </main>
  <p id="error" role="alert"></p>
  <script src="/console/memory.js"></script>
</body>
</html>
"""


_CONSOLE_JOBS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Jobs</title>
  <link rel="stylesheet" href="/console/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Local Web Console</p>
      <h1>Jobs</h1>
    </div>
    <nav class="nav-links" aria-label="Console pages">
      <a href="/console">Home</a>
      <a href="/console/reviews">Reviews</a>
      <a href="/console/search">Search</a>
      <a href="/console/memory">Memory</a>
      <a href="/console/jobs">Jobs</a>
      <a href="/console/sources">Sources</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit" aria-label="Apply service token">Apply</button>
    </form>
    <button id="refresh" type="button" aria-label="Refresh jobs">Refresh</button>
  </header>
  <main>
    <section class="summary">
      <div><p class="label">Service</p><strong id="ops-service">-</strong></div>
      <div><p class="label">Agentic Service</p><strong id="ops-agentic-service">-</strong></div>
      <div><p class="label">Queued</p><strong id="ops-queued">-</strong></div>
      <div><p class="label">Running</p><strong id="ops-running">-</strong></div>
      <div><p class="label">Failed</p><strong id="ops-failed">-</strong></div>
      <div><p class="label">Digest Jobs</p><strong id="ops-digest">-</strong></div>
    </section>
    <section class="ops-grid">
      <article>
        <h2>Issues</h2>
        <div id="ops-issues" class="memory-list"></div>
      </article>
      <article>
        <h2>Recovery Commands</h2>
        <ul id="ops-commands" class="commands"></ul>
      </article>
      <article>
        <h2>Recent Failures</h2>
        <div id="ops-failures" class="memory-list"></div>
      </article>
      <article>
        <h2>Running / Stale</h2>
        <div id="ops-running-list" class="memory-list"></div>
      </article>
    </section>
  </main>
  <p id="error" role="alert"></p>
  <script src="/console/jobs.js"></script>
</body>
</html>
"""


_CONSOLE_SOURCES_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PSKA Sources</title>
  <link rel="stylesheet" href="/console/app.css">
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Local Web Console</p>
      <h1>Sources</h1>
    </div>
    <nav class="nav-links" aria-label="Console pages">
      <a href="/console">Home</a>
      <a href="/console/reviews">Reviews</a>
      <a href="/console/search">Search</a>
      <a href="/console/memory">Memory</a>
      <a href="/console/jobs">Jobs</a>
      <a href="/console/sources">Sources</a>
    </nav>
    <form id="token-form" class="token-form">
      <label for="service-token">Service token</label>
      <input id="service-token" type="password" autocomplete="off" placeholder="Optional">
      <button type="submit" aria-label="Apply service token">Apply</button>
    </form>
    <button id="refresh" type="button" aria-label="Refresh sources">Refresh</button>
  </header>
  <main>
    <section class="summary">
      <div><p class="label">Sources</p><strong id="src-count">-</strong></div>
      <div><p class="label">Documents</p><strong id="doc-count">-</strong></div>
      <div><p class="label">Chunks</p><strong id="chunk-count">-</strong></div>
      <div><p class="label">Channels</p><strong id="channel-count">-</strong></div>
      <div><p class="label">Connectors</p><strong id="connector-count">-</strong></div>
      <div><p class="label">Files Roots</p><strong id="root-count">-</strong></div>
    </section>
    <section class="ops-grid">
      <article>
        <h2>Source Channels</h2>
        <div id="source-channels" class="memory-list"></div>
      </article>
      <article>
        <h2>Recent Sources</h2>
        <div id="recent-source-list" class="memory-list"></div>
      </article>
      <article>
        <h2>Connector States</h2>
        <div id="connector-states" class="memory-list"></div>
      </article>
      <article>
        <h2>Files Commands</h2>
        <ul id="source-commands" class="commands"></ul>
      </article>
    </section>
  </main>
  <p id="error" role="alert"></p>
  <script src="/console/sources.js"></script>
</body>
</html>
"""


_CONSOLE_CSS = """
:root {
  color-scheme: light;
  --bg: #f6f7f3;
  --ink: #1f2522;
  --muted: #667069;
  --line: #d9ddd5;
  --panel: #ffffff;
  --accent: #2d6a5f;
  --warn: #a8452a;
  --ok: #2f7d52;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 24px clamp(16px, 4vw, 48px);
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: clamp(32px, 5vw, 56px);
  line-height: 1;
  letter-spacing: 0;
}

h2 {
  font-size: 18px;
  line-height: 1.25;
}

.eyebrow,
.label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

button {
  min-width: 44px;
  min-height: 40px;
  border: 1px solid var(--accent);
  border-radius: 6px;
  background: var(--accent);
  color: white;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}

.nav-links {
  display: flex;
  align-items: center;
  gap: 8px;
}

.nav-links a {
  min-height: 40px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--ink);
  font-weight: 700;
  text-decoration: none;
}

.token-form {
  display: flex;
  align-items: end;
  gap: 8px;
  margin-left: auto;
}

.token-form label {
  display: grid;
  gap: 4px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}

.token-form input {
  width: min(240px, 34vw);
  min-height: 40px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  background: white;
  color: var(--ink);
  font: inherit;
}

main {
  width: min(1180px, calc(100% - 32px));
  margin: 24px auto 40px;
}

.summary {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--line);
}

.review-head {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--line);
}

.review-head > div {
  min-height: 96px;
  padding: 16px;
  background: var(--panel);
}

.review-head strong {
  display: block;
  margin-top: 10px;
  font-size: 26px;
  line-height: 1.1;
}

.review-list {
  display: grid;
  gap: 14px;
  margin-top: 16px;
}

.review-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.review-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.review-title strong {
  font-size: 17px;
}

.badge {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
}

.badge.warn {
  border-color: #d8aa9c;
  color: var(--warn);
}

.review-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 10px;
  color: var(--muted);
  font-size: 13px;
}

.review-actions {
  display: flex;
  flex-wrap: wrap;
  align-content: start;
  justify-content: end;
  gap: 8px;
}

.review-actions button {
  background: white;
  color: var(--accent);
}

.review-actions button.primary {
  background: var(--accent);
  color: white;
}

.review-actions button.danger {
  border-color: var(--warn);
  color: var(--warn);
}

.empty-state {
  padding: 24px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--muted);
}

.search-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto auto;
  gap: 10px;
  align-items: end;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.query-label {
  display: grid;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}

.search-form input[type="search"] {
  width: 100%;
  min-height: 42px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  color: var(--ink);
  font: inherit;
}

.option {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 42px;
  color: var(--ink);
  font-weight: 700;
}

.search-status {
  min-height: 22px;
  margin-top: 14px;
  color: var(--muted);
  font-weight: 700;
}

.search-results {
  display: grid;
  gap: 14px;
  margin-top: 14px;
}

.result-item,
.answer-panel,
details {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.result-item h2,
.answer-panel h2 {
  margin-bottom: 8px;
}

.snippet {
  color: var(--ink);
  line-height: 1.5;
}

.citation-line {
  margin-top: 10px;
  color: var(--muted);
  font-size: 13px;
}

details summary {
  cursor: pointer;
  font-weight: 800;
}

.compact-list {
  display: grid;
  gap: 8px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.compact-list li {
  padding: 8px 0;
  border-bottom: 1px solid var(--line);
}

.memory-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}

.ops-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}

.memory-grid > div {
  min-width: 0;
}

.memory-list {
  display: grid;
  gap: 12px;
  margin-top: 12px;
}

.memory-card {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.memory-card.attention {
  border-color: #d8aa9c;
}

.memory-card p {
  margin-top: 10px;
  line-height: 1.5;
}

.profile-json {
  margin-top: 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 13px;
}

.issue-card {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.issue-card.warning {
  border-color: #d8aa9c;
}

.issue-card.critical {
  border-color: var(--warn);
}

.issue-card p {
  margin-top: 8px;
  line-height: 1.45;
}

.summary > div,
article {
  background: var(--panel);
}

.summary > div {
  min-height: 96px;
  padding: 16px;
}

.summary strong {
  display: block;
  margin-top: 10px;
  font-size: 26px;
  line-height: 1.1;
}

.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}

article {
  min-height: 220px;
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 8px;
}

.checks {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 16px;
}

.check {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 42px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
}

.check span:last-child {
  color: var(--warn);
  font-weight: 800;
}

.check.ok span:last-child {
  color: var(--ok);
}

.list,
.commands {
  display: grid;
  gap: 10px;
  padding: 0;
  margin: 16px 0 0;
  list-style: none;
}

.list li,
.commands li {
  min-height: 38px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
}

.meta {
  display: block;
  margin-top: 4px;
  color: var(--muted);
  font-size: 13px;
}

code {
  display: block;
  overflow-wrap: anywhere;
  padding: 10px 12px;
  border-radius: 6px;
  background: #202622;
  color: #f4f7f2;
  font-size: 13px;
}

#error {
  width: min(1180px, calc(100% - 32px));
  min-height: 24px;
  margin: 0 auto 24px;
  color: var(--warn);
  font-weight: 700;
}

@media (max-width: 900px) {
  .summary,
  .grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 620px) {
  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .nav-links,
  .token-form {
    align-items: stretch;
    flex-direction: column;
    width: 100%;
    margin-left: 0;
  }

  .nav-links a {
    width: 100%;
  }

  .token-form input {
    width: 100%;
  }

  .summary,
  .review-head,
  .review-item,
  .search-form,
  .memory-grid,
  .ops-grid,
  .grid,
  .checks {
    grid-template-columns: 1fr;
  }

  .review-actions {
    justify-content: stretch;
  }
}
"""


_CONSOLE_JS = """
const text = (id, value) => {
  document.getElementById(id).textContent = value;
};

const list = (id, values, render) => {
  const element = document.getElementById(id);
  element.replaceChildren();
  const rows = values && values.length ? values : ["None"];
  for (const value of rows) {
    const item = document.createElement("li");
    render(item, value);
    element.appendChild(item);
  }
};

const renderDashboard = (data) => {
  const readiness = data.service_readiness || {};
  const sourceCounts = data.source_counts || {};
  const digest = data.digest_backlog || {};
  const pending = data.pending_reviews || {};
  const failed = data.failed_jobs || {};
  text("status", data.ok ? "Ready" : "Needs attention");
  text("sources", sourceCounts.source_items ?? 0);
  text("chunks", sourceCounts.chunks ?? 0);
  text("digest", digest.jobs ?? 0);
  text("reviews", pending.total_matching ?? 0);
  text("failed", failed.count ?? 0);

  const checks = document.getElementById("readiness");
  checks.replaceChildren();
  for (const [key, value] of Object.entries(readiness)) {
    const row = document.createElement("div");
    row.className = `check ${value ? "ok" : ""}`;
    const name = document.createElement("span");
    name.textContent = key.replaceAll("_", " ");
    const state = document.createElement("span");
    state.textContent = value ? "OK" : "Check";
    row.append(name, state);
    checks.appendChild(row);
  }

  list("actions", data.deterministic_next_actions, (item, value) => {
    item.textContent = value;
  });
  list("recent-sources", data.source_summary?.recent_sources, (item, value) => {
    if (typeof value === "string") {
      item.textContent = value;
      return;
    }
    item.textContent = value.title || value.source_item_id;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = [value.source_channel, value.record_type, value.created_at].filter(Boolean).join(" / ");
    item.appendChild(meta);
  });
  list("commands", data.recommended_commands, (item, value) => {
    const code = document.createElement("code");
    code.textContent = value;
    item.appendChild(code);
  });
};

const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const load = async () => {
  document.getElementById("error").textContent = "";
  const headers = {"accept": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    headers["X-PSKA-Service-Token"] = token;
  }
  const response = await fetch("/console/data?limit=5", {headers});
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(`Console data failed with HTTP ${response.status}`);
  }
  renderDashboard(await response.json());
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  load().catch((error) => {
    document.getElementById("error").textContent = error.message;
  });
});

document.getElementById("refresh").addEventListener("click", () => {
  load().catch((error) => {
    document.getElementById("error").textContent = error.message;
  });
});

load().catch((error) => {
  document.getElementById("error").textContent = error.message;
});
"""


_CONSOLE_REVIEWS_JS = """
const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const headers = () => {
  const result = {"accept": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    result["X-PSKA-Service-Token"] = token;
  }
  return result;
};

const jsonHeaders = () => ({...headers(), "content-type": "application/json"});

const setError = (message) => {
  document.getElementById("error").textContent = message || "";
};

const label = (text, warn = false) => {
  const element = document.createElement("span");
  element.className = `badge ${warn ? "warn" : ""}`;
  element.textContent = text;
  return element;
};

const value = (input) => input === null || input === undefined || input === "" ? "-" : input;

const actionButton = (text, className, onClick) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
};

const requestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  if (payload.error) {
    throw new Error(payload.error);
  }
  return payload;
};

const runAction = async (reviewId, action) => {
  const body = JSON.stringify({actor_user_id: "user_primary", reason: `console ${action}`, apply: action === "approve_apply"});
  let url = `/review-items/${encodeURIComponent(reviewId)}/approve`;
  if (action === "reject") {
    url = `/review-items/${encodeURIComponent(reviewId)}/reject`;
  }
  if (action === "apply") {
    url = `/review-items/${encodeURIComponent(reviewId)}/apply`;
  }
  await requestJson(url, {method: "POST", headers: jsonHeaders(), body});
  await loadReviews();
};

const renderReview = (item) => {
  const row = document.createElement("article");
  row.className = "review-item";

  const content = document.createElement("div");
  const title = document.createElement("div");
  title.className = "review-title";
  const strong = document.createElement("strong");
  strong.textContent = item.title || item.review_item_id;
  title.appendChild(strong);
  title.appendChild(label(item.review_type));
  title.appendChild(label(item.source_ref_status === "present" ? "source refs present" : "missing source refs", item.source_ref_status !== "present"));
  if (!item.apply_supported) {
    title.appendChild(label("apply unsupported", true));
  } else if (!item.apply_ready) {
    title.appendChild(label("inspect before apply", true));
  }

  const meta = document.createElement("div");
  meta.className = "review-meta";
  meta.append(
    `confidence ${value(item.confidence)}`,
    `created ${value(item.created_at)}`,
    `recommended ${value(item.recommended_action)}`,
    `id ${item.review_item_id}`,
  );

  content.append(title, meta);

  const actions = document.createElement("div");
  actions.className = "review-actions";
  if (item.status === "pending") {
    actions.appendChild(actionButton("Approve", "primary", () => runAction(item.review_item_id, "approve").catch((error) => setError(error.message))));
    if (item.apply_ready) {
      actions.appendChild(actionButton("Approve + apply", "primary", () => runAction(item.review_item_id, "approve_apply").catch((error) => setError(error.message))));
    }
    actions.appendChild(actionButton("Reject", "danger", () => runAction(item.review_item_id, "reject").catch((error) => setError(error.message))));
  } else if (item.can_apply_now) {
    actions.appendChild(actionButton("Apply", "primary", () => runAction(item.review_item_id, "apply").catch((error) => setError(error.message))));
  }

  row.append(content, actions);
  return row;
};

const renderReviews = (payload) => {
  document.getElementById("review-count").textContent = payload.total_matching ?? payload.count ?? 0;
  document.getElementById("review-owner").textContent = payload.owner_user_id || "user_primary";
  const container = document.getElementById("reviews");
  container.replaceChildren();
  const items = payload.review_items || [];
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No pending reviews.";
    container.appendChild(empty);
    return;
  }
  for (const item of items) {
    container.appendChild(renderReview(item));
  }
};

const loadReviews = async () => {
  setError("");
  const payload = await requestJson("/console/reviews/data?status=pending&owner_user_id=user_primary&limit=50", {headers: headers()});
  renderReviews(payload);
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  loadReviews().catch((error) => setError(error.message));
});

document.getElementById("refresh").addEventListener("click", () => {
  loadReviews().catch((error) => setError(error.message));
});

loadReviews().catch((error) => setError(error.message));
"""


_CONSOLE_SEARCH_JS = """
const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const setError = (message) => {
  document.getElementById("error").textContent = message || "";
};

const headers = () => {
  const result = {"accept": "application/json", "content-type": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    result["X-PSKA-Service-Token"] = token;
  }
  return result;
};

const text = (value) => value === null || value === undefined || value === "" ? "-" : String(value);

const requestSearch = async (payload) => {
  const response = await fetch("/console/search/query", {method: "POST", headers: headers(), body: JSON.stringify(payload)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  if (data.error) {
    throw new Error(data.error);
  }
  return data;
};

const line = (content) => {
  const item = document.createElement("li");
  item.textContent = content;
  return item;
};

const details = (title, rows) => {
  const element = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = title;
  element.appendChild(summary);
  const list = document.createElement("ul");
  list.className = "compact-list";
  const values = rows && rows.length ? rows : ["None"];
  for (const value of values) {
    list.appendChild(line(value));
  }
  element.appendChild(list);
  return element;
};

const renderResults = (payload) => {
  const container = document.getElementById("search-results");
  container.replaceChildren();
  const retrieval = (payload.fallback && payload.fallback.retrieval) || payload.retrieval || {};
  document.getElementById("search-status").textContent = `${payload.mode} search / ${retrieval.results?.length || 0} result(s)`;

  if (payload.error) {
    const warning = document.createElement("section");
    warning.className = "answer-panel";
    const heading = document.createElement("h2");
    heading.textContent = "Agentic mode needs a knowledge question";
    const body = document.createElement("p");
    body.className = "snippet";
    body.textContent = payload.error.message || "Agentic mode failed.";
    const detail = document.createElement("p");
    detail.className = "citation-line";
    detail.textContent = payload.error.detail || "";
    warning.append(heading, body, detail);
    container.appendChild(warning);
  }

  if (payload.answer) {
    const answer = document.createElement("section");
    answer.className = "answer-panel";
    const heading = document.createElement("h2");
    heading.textContent = "Answer";
    const body = document.createElement("p");
    body.className = "snippet";
    body.textContent = payload.answer;
    answer.append(heading, body);
    if (payload.capture) {
      const capture = document.createElement("p");
      capture.className = "citation-line";
      capture.textContent = `capture ${payload.capture.action}: ${payload.capture.explanation}`;
      answer.appendChild(capture);
    }
    container.appendChild(answer);
  }

  for (const result of retrieval.results || []) {
    const row = document.createElement("article");
    row.className = "result-item";
    const heading = document.createElement("h2");
    heading.textContent = result.title || "Untitled source";
    const snippet = document.createElement("p");
    snippet.className = "snippet";
    snippet.textContent = result.snippet || "";
    const citation = document.createElement("p");
    citation.className = "citation-line";
    citation.textContent = [result.citation?.source_item_id, result.citation?.chunk_id, `score ${text(result.score)}`].filter(Boolean).join(" / ");
    row.append(heading, snippet, citation);
    container.appendChild(row);
  }

  const citations = (retrieval.citations || []).map((citation) => [citation.title, citation.source_item_id, citation.chunk_id].filter(Boolean).join(" / "));
  const graphPaths = (retrieval.graph_paths || []).map((path) => `${path.explanation || path.entities?.join(" -> ") || "graph path"} / grounded ${path.grounded_edges}/${path.edge_count}`);
  const memory = (retrieval.memory_context || []).map((item) => `${item.text} / citations ${item.citation_count}`);
  const profile = (retrieval.profile_context || []).map((item) => `${item.text} / citations ${item.citation_count}`);
  const diagnostics = retrieval.diagnostics || {};
  const diagnosticRows = [
    ...(diagnostics.gaps || []).map((item) => `gap: ${item}`),
    ...(diagnostics.conflicts || []).map((item) => `conflict: ${item}`),
    ...(diagnostics.sensitivity || []).map((item) => `sensitivity: ${item}`),
  ];
  container.append(
    details("Citations", citations),
    details("Graph Evidence", graphPaths),
    details("Memory Context", memory),
    details("Profile Context", profile),
    details("Diagnostics", diagnosticRows),
  );
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  setError("");
});

document.getElementById("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  document.getElementById("search-status").textContent = "Searching...";
  const mode = document.getElementById("agentic").checked ? "agentic" : "direct";
  const payload = {
    query: document.getElementById("query").value,
    mode,
    capture: document.getElementById("capture").checked,
    user_id: "user_primary",
    represented_user_id: "user_primary",
    top_k: 5,
  };
  try {
    renderResults(await requestSearch(payload));
  } catch (error) {
    document.getElementById("search-status").textContent = "";
    setError(error.message);
  }
});
"""


_CONSOLE_MEMORY_JS = """
const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const setError = (message) => {
  document.getElementById("error").textContent = message || "";
};

const headers = () => {
  const result = {"accept": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    result["X-PSKA-Service-Token"] = token;
  }
  return result;
};

const text = (value) => value === null || value === undefined || value === "" ? "-" : String(value);

const badge = (value, warn = false) => {
  const element = document.createElement("span");
  element.className = `badge ${warn ? "warn" : ""}`;
  element.textContent = value;
  return element;
};

const meta = (values) => {
  const element = document.createElement("div");
  element.className = "review-meta";
  for (const value of values) {
    const span = document.createElement("span");
    span.textContent = value;
    element.appendChild(span);
  }
  return element;
};

const memoryCard = (item) => {
  const card = document.createElement("article");
  card.className = `memory-card ${item.needs_attention ? "attention" : ""}`;
  const title = document.createElement("div");
  title.className = "review-title";
  const strong = document.createElement("strong");
  strong.textContent = item.layer || item.agent_memory_id;
  title.append(
    strong,
    badge(item.status || "active", item.status !== "active"),
    badge(item.source_ref_status === "present" ? "source refs present" : "missing source refs", item.source_ref_status !== "present"),
  );
  const body = document.createElement("p");
  body.textContent = item.text || "";
  card.append(
    title,
    body,
    meta([
      `confidence ${text(item.confidence)}`,
      `last verified ${text(item.last_verified_at)}`,
      `decay ${text(item.decay_policy)}`,
      `created by ${text(item.created_by_user_id)}`,
      `id ${text(item.agent_memory_id)}`,
    ]),
  );
  return card;
};

const profileCard = (item) => {
  const card = document.createElement("article");
  card.className = `memory-card ${item.needs_attention ? "attention" : ""}`;
  const title = document.createElement("div");
  title.className = "review-title";
  const strong = document.createElement("strong");
  strong.textContent = item.profile_card_id || "profile";
  title.append(
    strong,
    badge(item.status || "active"),
    badge(item.source_ref_status === "present" ? "source refs present" : "missing source refs", item.source_ref_status !== "present"),
  );
  const pre = document.createElement("pre");
  pre.className = "profile-json";
  pre.textContent = JSON.stringify(item.profile || {}, null, 2);
  card.append(
    title,
    pre,
    meta([
      `confidence ${text(item.confidence)}`,
      `last verified ${text(item.last_verified_at)}`,
      `id ${text(item.profile_card_id)}`,
    ]),
  );
  return card;
};

const renderList = (id, items, render) => {
  const container = document.getElementById(id);
  container.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No records.";
    container.appendChild(empty);
    return;
  }
  for (const item of items) {
    container.appendChild(render(item));
  }
};

const loadMemory = async () => {
  setError("");
  const response = await fetch("/console/memory/data?owner_user_id=user_primary&limit=50", {headers: headers()});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  document.getElementById("memory-count").textContent = payload.memory_count ?? 0;
  document.getElementById("profile-count").textContent = payload.profile_count ?? 0;
  renderList("agent-memories", payload.agent_memories || [], memoryCard);
  renderList("profile-cards", payload.profile_cards || [], profileCard);
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  loadMemory().catch((error) => setError(error.message));
});

document.getElementById("refresh").addEventListener("click", () => {
  loadMemory().catch((error) => setError(error.message));
});

loadMemory().catch((error) => setError(error.message));
"""


_CONSOLE_JOBS_JS = """
const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const setError = (message) => {
  document.getElementById("error").textContent = message || "";
};

const headers = () => {
  const result = {"accept": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    result["X-PSKA-Service-Token"] = token;
  }
  return result;
};

const text = (value) => value === null || value === undefined || value === "" ? "-" : String(value);

const setText = (id, value) => {
  document.getElementById(id).textContent = text(value);
};

const card = (className, children) => {
  const element = document.createElement("article");
  element.className = className;
  element.append(...children);
  return element;
};

const title = (main, badges = []) => {
  const row = document.createElement("div");
  row.className = "review-title";
  const strong = document.createElement("strong");
  strong.textContent = main;
  row.appendChild(strong);
  for (const badgeText of badges) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = badgeText;
    row.appendChild(badge);
  }
  return row;
};

const paragraph = (value) => {
  const element = document.createElement("p");
  element.textContent = value;
  return element;
};

const renderList = (id, values, render, emptyText = "None") => {
  const container = document.getElementById(id);
  container.replaceChildren();
  if (!values.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  for (const value of values) {
    container.appendChild(render(value));
  }
};

const renderCommands = (commands) => {
  const container = document.getElementById("ops-commands");
  container.replaceChildren();
  for (const command of commands) {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = command;
    item.appendChild(code);
    container.appendChild(item);
  }
};

const jobCard = (job) => card("memory-card", [
  title(job.job_id || "job", [job.job_type || "unknown", job.status || "unknown"]),
  paragraph(job.error || `worker ${text(job.worker_id)} / leased ${text(job.leased_until)}`),
]);

const issueCard = (issue) => card(`issue-card ${issue.severity || ""}`, [
  title(issue.id || "issue", [issue.status || "unknown", issue.severity || "info"]),
  paragraph(issue.summary || ""),
]);

const renderOps = (payload) => {
  const readiness = payload.service_readiness || {};
  const health = payload.worker_health || {};
  const byStatus = health.by_status || {};
  const backlog = payload.digest_backlog || {};
  setText("ops-service", readiness.ok ? "OK" : "Check");
  setText("ops-agentic-service", readiness.agentic_service_ok ? "OK" : "Check");
  setText("ops-queued", byStatus.queued || 0);
  setText("ops-running", byStatus.running || 0);
  setText("ops-failed", byStatus.failed || 0);
  setText("ops-digest", backlog.jobs || 0);
  renderList("ops-issues", payload.issues || [], issueCard);
  renderCommands(payload.recommended_recovery_commands || []);
  renderList("ops-failures", payload.recent_failed || [], jobCard, "No failed jobs.");
  renderList("ops-running-list", [...(payload.worker_health?.stale_running || []), ...(payload.running_jobs || [])], jobCard, "No running jobs.");
};

const loadOps = async () => {
  setError("");
  const response = await fetch("/console/jobs/data?limit=20", {headers: headers()});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  renderOps(payload);
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  loadOps().catch((error) => setError(error.message));
});

document.getElementById("refresh").addEventListener("click", () => {
  loadOps().catch((error) => setError(error.message));
});

loadOps().catch((error) => setError(error.message));
"""


_CONSOLE_SOURCES_JS = """
const tokenInput = document.getElementById("service-token");
tokenInput.value = sessionStorage.getItem("pska_service_token") || "";

const setError = (message) => {
  document.getElementById("error").textContent = message || "";
};

const headers = () => {
  const result = {"accept": "application/json"};
  const token = tokenInput.value.trim();
  if (token) {
    result["X-PSKA-Service-Token"] = token;
  }
  return result;
};

const text = (value) => value === null || value === undefined || value === "" ? "-" : String(value);

const setText = (id, value) => {
  document.getElementById(id).textContent = text(value);
};

const title = (main, badges = []) => {
  const row = document.createElement("div");
  row.className = "review-title";
  const strong = document.createElement("strong");
  strong.textContent = main;
  row.appendChild(strong);
  for (const value of badges) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = value;
    row.appendChild(badge);
  }
  return row;
};

const paragraph = (value) => {
  const element = document.createElement("p");
  element.textContent = value;
  return element;
};

const renderList = (id, values, render, emptyText = "None") => {
  const container = document.getElementById(id);
  container.replaceChildren();
  if (!values.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  for (const value of values) {
    container.appendChild(render(value));
  }
};

const renderCommands = (commands) => {
  const container = document.getElementById("source-commands");
  container.replaceChildren();
  for (const command of commands) {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = command;
    item.appendChild(code);
    container.appendChild(item);
  }
};

const card = (children) => {
  const element = document.createElement("article");
  element.className = "memory-card";
  element.append(...children);
  return element;
};

const channelCard = ([name, value]) => card([
  title(name, [`${value.source_items || 0} source(s)`]),
  paragraph(`latest ${text(value.latest_source_item_id)} / ${text(value.latest_source_item_at)}`),
]);

const sourceCard = (source) => card([
  title(source.title || source.source_item_id, [source.source_channel || "unknown", source.record_type || "record"]),
  paragraph(`${source.source_item_id} / ${text(source.created_at)}`),
]);

const connectorCard = (state) => card([
  title(state.connector_state_id || state.connector_id, [state.connector_id || "connector", state.enabled ? "enabled" : "disabled", state.sync_status || "unknown"]),
  paragraph(`roots ${state.roots && state.roots.length ? state.roots.join(", ") : "-"} / last success ${text(state.last_success_at)} / cursor ${text(state.scan_cursor)}`),
]);

const renderSources = (payload) => {
  const counts = payload.source_counts || {};
  const states = payload.connector_state?.states || [];
  const roots = payload.files?.roots || [];
  setText("src-count", counts.source_items || 0);
  setText("doc-count", counts.documents || 0);
  setText("chunk-count", counts.chunks || 0);
  setText("channel-count", Object.keys(payload.source_channels || {}).length);
  setText("connector-count", payload.connector_state?.state_count || 0);
  setText("root-count", roots.length);
  renderList("source-channels", Object.entries(payload.source_channels || {}), channelCard);
  renderList("recent-source-list", payload.recent_sources || [], sourceCard);
  renderList("connector-states", states, connectorCard);
  renderCommands(payload.recommended_commands || []);
};

const loadSources = async () => {
  setError("");
  const response = await fetch("/console/sources/data?owner_user_id=user_primary&limit=20", {headers: headers()});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Service token required. Paste the local PSKA service token and apply.");
    }
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  renderSources(payload);
};

document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem("pska_service_token", tokenInput.value.trim());
  loadSources().catch((error) => setError(error.message));
});

document.getElementById("refresh").addEventListener("click", () => {
  loadSources().catch((error) => setError(error.message));
});

loadSources().catch((error) => setError(error.message));
"""
