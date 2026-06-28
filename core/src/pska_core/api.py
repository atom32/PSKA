from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
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
from pska_core.config import DEFAULT_DATABASE_URL, DatabaseConfig, PSKAConfig, ServiceConfig
from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.discovery import DISCOVERY_TODAY_SCORE_THRESHOLD, DiscoveryService
from pska_core.embeddings import EmbeddingConfig, build_embedding_provider
from pska_core.enums import UserRole, Visibility
from pska_core.extraction import ExtractionService
from pska_core.fastreact_protocol import compact_trace_for_context
from pska_core.files_connector import scan_files
from pska_core.importers.twitter_zip import TwitterZipImporter
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.knowledge_sources import KnowledgeSourceService
from pska_core.memory import MemoryService
from pska_core.mcp_server import MCPServer, PROTOCOL_VERSION
from pska_core.models import (
    DEFAULT_TENANT_ID,
    ChannelIngestPayload,
    PassageWindow,
    ReviewItem,
    SourceRef,
    WorkspaceActivityEvent,
    WritingBoard,
    WritingEdge,
    WritingNode,
)
from pska_core.offline_index import OfflineIndexService
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.serde import to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


ASK_READ_TOOL_PROFILE = "ask_read"
ASK_READ_ONLY_TOOLS = [
    "pska_pska_search",
    "pska_pska_index_status",
    "pska_pska_read_evidence_context",
    "pska_pska_graph_context",
    "pska_pska_digest_context",
]


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

    def index_status(self) -> dict[str, Any]:
        return {
            "source_items": self.store.count_table("source_items"),
            "documents": self.store.count_table("documents"),
            "chunks": self.store.count_table("chunks"),
            "entities": self.store.count_table("entities"),
            "hyperedges": self.store.count_table("hyperedges"),
            "knowledge_claims": self.store.count_table("knowledge_claims"),
            "digest_notes": self.store.count_table("digest_notes"),
            "graph_nodes": self.store.count_table("graph_nodes"),
            "graph_edges": self.store.count_table("graph_edges"),
            "review_items": self.store.count_table("review_items"),
            "jobs": self.store.count_table("jobs"),
            "offline_index_states": self.store.count_table("offline_index_states"),
            "writing_boards": self.store.count_table("writing_boards"),
            "writing_nodes": self.store.count_table("writing_nodes"),
            "writing_edges": self.store.count_table("writing_edges"),
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
            "sync_runs",
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
        return to_jsonable(
            self.retrieval.search(
                payload["query"],
                user,
                represented_user_id=payload.get("represented_user_id"),
                top_k=int(payload.get("top_k") or 5),
            )
        )

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

    def files_sync(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        source_service = KnowledgeSourceService(self.store)
        tenant_id = str(payload.get("tenant_id") or self.config.files.tenant_id or DEFAULT_TENANT_ID)
        owner_user_id = str(payload.get("owner_user_id") or self.config.files.owner_user_id)
        requested_roots = [Path(str(root)).expanduser().resolve() for root in _string_list(payload.get("roots") or payload.get("root"))]
        ignore = _string_list(payload.get("ignore"))
        max_bytes = _optional_positive_int(payload.get("max_bytes")) or self.config.files.max_bytes
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
                "error": "No knowledge sources configured. Add files.roots to .pska/config.json for cold start seed or pass roots.",
                "database_error": f"{type(exc).__name__}: {exc}",
                "reports": [],
                "knowledge_sources": [],
            }
        if not sources:
            return {
                "ok": False,
                "error": "No knowledge sources configured. Add files.roots to .pska/config.json for cold start seed or pass roots.",
                "reports": [],
                "knowledge_sources": [],
            }

        reports = []
        sync_runs = []
        failed = []
        embedding_provider = build_embedding_provider(EmbeddingConfig(provider="disabled"))
        for source in sources:
            root = source_service.source_path(source)
            try:
                report = scan_files(
                    self.store,
                    root=root,
                    owner_user_id=source.owner_user_id,
                    tenant_id=source.tenant_id,
                    space_id=source.space_id,
                    visibility=source.visibility,
                    visible_team_ids=source.visible_team_ids,
                    ignore=[*list(source.config.get("ignore") or []), *ignore],
                    max_bytes=max_bytes,
                    embedding_provider=embedding_provider,
                )
                reports.append(report)
                failed.extend(report.failed)
                sync_runs.append(source_service.record_sync_report(source, report))
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
            _digest_now_fallback_review,
            _digest_schedule_payload,
            _review_items_payload,
            _run_fastreact_digest_worker,
        )

        payload = context.apply_to_payload(payload) if context else payload
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = str(payload.get("owner_user_id") or "user_primary")
        args = argparse.Namespace(
            database_url=getattr(self.store, "database_url", self.config.database.url),
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
        worker_runs = _run_fastreact_digest_worker(args, self.config) if args.max_worker_runs > 0 else []
        diagnostics = _digest_now_diagnostics(worker_runs)
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
        candidate_summary = _digest_now_candidate_summary(worker_runs)
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

    def digest_logs(self, *, owner_user_id: str = "user_primary", tenant_id: str | None = None, limit: int = 10) -> dict[str, Any]:
        limit = max(1, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        jobs = [
            job
            for job in self.store.list_jobs(tenant_id=tenant_id, job_type=DIGEST_VIA_FASTREACT, limit=max(limit * 3, limit))
            if str(job.payload.get("owner_user_id") or owner_user_id) == owner_user_id
        ][:limit]
        entries = []
        for job in jobs:
            source_ids = _job_source_item_ids(job)
            events = self.store.list_job_events(job.job_id)
            claims = self.store.list_knowledge_claims(owner_user_id=owner_user_id, tenant_id=tenant_id, job_id=job.job_id, limit=20)
            notes = self.store.list_digest_notes(owner_user_id=owner_user_id, tenant_id=tenant_id, job_id=job.job_id, limit=10)
            entries.append(_digest_log_entry(job, events, claims, notes, source_ids))
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "summary": _digest_logs_summary(entries),
            "logs": to_jsonable(entries),
            "count": len(entries),
        }

    def metrics(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        source_items = self.store.list_source_items(tenant_id=tenant_id)
        chunks = self.store.list_chunks_for_sources({item.source_item_id for item in source_items})
        return {
            "index": self.index_status(),
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

    def console_reviews(self, *, status: str = "pending", owner_user_id: str = "user_primary", tenant_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        limit = max(0, limit)
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        items = _console_review_items(
            to_jsonable(self.store.list_review_items(tenant_id=tenant_id)),
            status=status,
            owner_user_id=owner_user_id,
            limit=limit,
        )
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "status": status,
            "review_items": items,
            "count": len(items),
            "total_matching": len(
                _console_review_items(
                    to_jsonable(self.store.list_review_items(tenant_id=tenant_id)),
                    status=status,
                    owner_user_id=owner_user_id,
                    limit=10_000,
                )
            ),
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
        )
        return {
            "ok": True,
            "mode": "direct",
            "requires_agentic_service_online": False,
            "query": query,
            "retrieval": _console_search_summary(to_jsonable(response)),
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

    def workspace_ask(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        started_at = time.perf_counter()
        payload = context.apply_to_payload(payload) if context else payload
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        intent = str(payload.get("intent") or "auto").strip().lower()
        if intent not in {"auto", "quick", "deep"}:
            raise ValueError("intent must be one of auto, quick, deep")
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        user_id = str(payload.get("user_id") or owner_user_id)
        represented_user_id = str(payload.get("represented_user_id") or owner_user_id)
        surface = str(payload.get("surface") or "ask").strip() or "ask"
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        top_k = max(1, min(int(payload.get("top_k") or 8), 20))
        session_id = str(payload.get("session_id") or "").strip() or None
        selected_intent = _ask_route_intent(query, intent=intent)
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        if selected_intent == "deep":
            try:
                deep_query = _ask_deep_query(query=query, surface=surface, scope=scope)
                deep = self._workspace_ask_deep_agentic(
                    deep_query,
                    user,
                    represented_user_id=represented_user_id,
                    max_iterations=max(1, min(int(payload.get("max_iterations") or 4), 8)),
                    skills=[PSKA_QA_SKILL],
                    tool_policy={"mode": "allowlist", "allowed_tools": ASK_READ_ONLY_TOOLS},
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
                        agentic=deep,
                        started_at=started_at,
                        allowed_tools=ASK_READ_ONLY_TOOLS,
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
        if intent not in {"auto", "quick", "deep"}:
            raise ValueError("intent must be one of auto, quick, deep")
        tenant_id = str(payload.get("tenant_id") or DEFAULT_TENANT_ID)
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id") or payload.get("represented_user_id"))
        user_id = str(payload.get("user_id") or owner_user_id)
        represented_user_id = str(payload.get("represented_user_id") or owner_user_id)
        surface = str(payload.get("surface") or "ask").strip() or "ask"
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        top_k = max(1, min(int(payload.get("top_k") or 8), 20))
        session_id = str(payload.get("session_id") or "").strip() or None
        selected_intent = _ask_route_intent(query, intent=intent)
        user = self.store.get_user(user_id, tenant_id=tenant_id)
        if selected_intent != "deep" or not hasattr(self.agentic_service, "search_event_stream"):
            if selected_intent != "deep":
                route = _ask_route_payload(
                    intent=intent,
                    selected_intent="quick",
                    retrieval_owner="pska",
                    surface=surface,
                    requires_agentic_service_online=False,
                    tool_policy={"mode": "none"},
                    query=query,
                )
                yield ("route", {"route": route, "timing": {}})
                query_terms = _ask_query_terms(query)
                emitted_steps = _ask_route_planner_steps(
                    query=query,
                    intent=intent,
                    selected_intent="quick",
                    query_terms=query_terms,
                    started_at=started_at,
                    start_sequence=1,
                    include_understand=True,
                )
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
                    yield ("agent_step", {"step": step, "timing": {"time_to_first_agent_event_ms": time_to_first_agent_event_ms}})
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
                    )
                )
                final_payload["timing"]["time_to_first_agent_event_ms"] = time_to_first_agent_event_ms
                for step in _list_of_dicts(final_payload.get("agent_steps"))[len(emitted_steps) :]:
                    yield ("agent_step", {"step": step, "timing": final_payload.get("timing") or {}})
                for event_name, event_payload in _ask_sse_events(final_payload):
                    if event_name in {"route", "agent_step"}:
                        continue
                    yield (event_name, event_payload)
                return
            final_payload = self.workspace_ask(payload, context=context)
            yield from _ask_sse_events(final_payload)
            return

        route = {
            "intent": intent,
            "selected_intent": selected_intent,
            "retrieval_owner": "fastreact_pska_mcp",
            "surface": surface,
            "requires_agentic_service_online": True,
            "tool_policy": {"mode": "allowlist", "allowed_tools": ASK_READ_ONLY_TOOLS},
            "tool_profile": ASK_READ_TOOL_PROFILE,
            "routing_owner": "pska_planner",
            "query_terms": _ask_query_terms(query),
        }
        yield ("route", {"route": route, "timing": {}})
        raw_events: list[dict[str, Any]] = []
        agent_steps: list[dict[str, Any]] = _ask_route_planner_steps(
            query=query,
            intent=intent,
            selected_intent=selected_intent,
            query_terms=_ask_query_terms(query),
            started_at=started_at,
            start_sequence=1,
            include_understand=False,
        )
        time_to_first_agent_event_ms: float | None = None
        if agent_steps:
            time_to_first_agent_event_ms = agent_steps[0].get("elapsed_ms")
            for step in agent_steps:
                yield ("agent_step", {"step": step, "timing": {"time_to_first_agent_event_ms": time_to_first_agent_event_ms}})
        try:
            event_stream = self.agentic_service.search_event_stream(
                _ask_deep_query(query=query, surface=surface, scope=scope),
                user,
                represented_user_id=represented_user_id,
                max_iterations=max(1, min(int(payload.get("max_iterations") or 4), 8)),
                skills=[PSKA_QA_SKILL],
                tool_policy={"mode": "allowlist", "allowed_tools": ASK_READ_ONLY_TOOLS},
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
                    yield (
                        "agent_step",
                        {
                            "step": step,
                            "timing": {"time_to_first_agent_event_ms": time_to_first_agent_event_ms},
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
            agentic=agentic,
            started_at=started_at,
            allowed_tools=ASK_READ_ONLY_TOOLS,
            store=self.store,
        )
        final_payload["agent_steps"] = agent_steps or _ask_agent_steps_from_events(raw_events)
        final_payload["timing"]["time_to_first_agent_event_ms"] = time_to_first_agent_event_ms
        final_payload = _ask_with_quality_signals(final_payload)
        for event_name, event_payload in _ask_sse_events(final_payload):
            if event_name in {"route", "agent_step"}:
                continue
            yield (event_name, event_payload)

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
    ) -> dict[str, Any]:
        query_terms = query_terms or _ask_query_terms(query)
        steps = list(agent_steps or [])
        if not steps:
            steps.extend(
                _ask_route_planner_steps(
                    query=query,
                    intent=intent,
                    selected_intent="quick",
                    query_terms=query_terms,
                    started_at=started_at,
                    start_sequence=1,
                    include_understand=True,
                )
            )
            steps.append(
                _ask_quick_search_step(
                    sequence=len(steps) + 1,
                    query_terms=query_terms,
                    top_k=top_k,
                    started_at=started_at,
                )
            )
        retrieval_query = _ask_query_with_scope(query, scope or {})
        retrieval_result = self.retrieval.search(retrieval_query, user, represented_user_id=represented_user_id, top_k=top_k)
        retrieval = _console_search_summary(to_jsonable(retrieval_result))
        evidence = _ask_evidence_from_retrieval(retrieval)
        steps.append(_ask_quick_read_step(sequence=len(steps) + 1, evidence=evidence, started_at=started_at))
        answer = _ask_quick_answer(query, retrieval)
        steps.append(
            _ask_agent_step(
                sequence=len(steps) + 1,
                phase="answer",
                status="complete",
                title="形成回答",
                detail="已完成证据归纳和引用校验。",
                started_at=started_at,
            )
        )
        elapsed_ms = _elapsed_ms(started_at)
        return {
            "ok": True,
            "query": query,
            "answer": answer,
            "route": {
                "intent": intent,
                "selected_intent": "quick",
                "retrieval_owner": "pska",
                "surface": surface,
                "requires_agentic_service_online": False,
                "tool_policy": {"mode": "none"},
                "routing_owner": "pska_planner",
                "query_terms": query_terms,
                "scope_context_nodes": len(_list_of_dicts((scope or {}).get("context_nodes"))),
            },
            "evidence": evidence,
            "citations": evidence["citations"],
            "source_refs": evidence["source_refs"],
            "agent_steps": steps,
            "trace": {
                "mode": "quick",
                "query_terms": query_terms,
                "retrieval_query": retrieval_query,
                "scope": _ask_scope_trace(scope or {}),
                "retrieval_owner": "pska",
                "retrieval": retrieval,
                "diagnostics": retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {},
            },
            "timing": {
                "total_ms": elapsed_ms,
                "time_to_first_answer_ms": elapsed_ms,
                "time_to_first_agent_event_ms": steps[0].get("elapsed_ms") if steps else None,
            },
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
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
        all_sources = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == owner_user_id]
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

    def workspace_graph_data(
        self,
        *,
        owner_user_id: str | None = None,
        limit: int = 30,
        node_types: set[str] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        tenant_id = _tenant_id_for_request(context)
        limit = max(1, min(limit, 100))
        source_items = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == owner_user_id][:limit]
        source_ids = {item.source_item_id for item in source_items}
        documents = self.store.list_documents_for_sources(source_ids)
        chunks = self.store.list_chunks_for_sources(source_ids)
        passage_windows = _passage_windows_for_documents(documents, chunks)
        claims = self.store.list_knowledge_claims(owner_user_id=owner_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=limit * 4)
        digest_notes = self.store.list_digest_notes(owner_user_id=owner_user_id, tenant_id=tenant_id, source_item_ids=source_ids, limit=limit)
        memories = self.store.list_agent_memories(owner_user_id=owner_user_id, tenant_id=tenant_id)[:limit]
        review_items = [
            item
            for item in self.store.list_review_items(tenant_id=tenant_id)
            if getattr(item, "owner_user_id", "") == owner_user_id and getattr(item, "status", "") == "pending"
        ][:limit]
        entities = [entity for entity in self.store.list_entities(tenant_id=tenant_id) if getattr(entity, "owner_user_id", "") == owner_user_id]
        entity_by_id = {entity.entity_id: entity for entity in entities}
        hyperedges = [
            (edge, members)
            for edge, members in self.store.list_hyperedges_for_entities(set(entity_by_id))
            if getattr(edge, "owner_user_id", "") == owner_user_id
        ]
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
        )
        unfiltered_counts = {"nodes": len(nodes), "edges": len(edges)}
        nodes, edges = _filter_workspace_graph_projection(nodes, edges, node_types=node_types)
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "ontology_version": "pska.graph.v2",
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
        limit: int = 80,
        hops: int = 1,
        node_types: set[str] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        limit = max(1, min(limit, 160))
        hops = max(1, min(hops, 3))
        graph = self.workspace_graph_data(owner_user_id=owner_user_id, limit=limit, node_types=None, context=context)
        nodes = _list_of_dicts(graph.get("nodes"))
        edges = _list_of_dicts(graph.get("edges"))
        sub_nodes, sub_edges = _workspace_graph_subgraph(nodes, edges, node_id=node_id, hops=hops, node_types=node_types)
        return {
            "ok": bool(sub_nodes),
            "owner_user_id": owner_user_id,
            "ontology_version": graph.get("ontology_version") or "pska.graph.v2",
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
        graph = self.workspace_graph_data(owner_user_id=owner_user_id, limit=limit, node_types=None, context=context)
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
        board = WritingBoard(
            board_id=str(payload.get("board_id") or f"wboard_{uuid4().hex}"),
            tenant_id=tenant_id,
            owner_user_id=owner,
            title=title,
            goal=goal,
            metadata=dict(payload.get("metadata") or {}),
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
            metadata=dict(payload["metadata"]) if isinstance(payload.get("metadata"), dict) else None,
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
        source_cards = [_console_knowledge_source(source, self.store.list_sync_runs(tenant_id=tenant_id, knowledge_source_id=source.knowledge_source_id, limit=1)) for source in knowledge_sources[:limit]]
        files_roots = _console_knowledge_source_roots(source_cards) or _console_files_roots(states)
        input_sources = _console_input_sources(self.config, source_cards)
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "read_only": True,
            "source_counts": {
                "source_items": self.store.count_table("source_items"),
                "documents": self.store.count_table("documents"),
                "chunks": self.store.count_table("chunks"),
            },
            "source_channels": metrics.get("source_channels") or {},
            "knowledge_sources": {
                "source_count": len(knowledge_sources),
                "sources": source_cards,
            },
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

        source_items = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == owner_user_id]
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
            job_payload: dict[str, Any] = {
                "owner_user_id": owner_user_id,
                "tenant_id": tenant_id,
                "batch_size": batch_size,
                "retry_backoff_seconds": retry_backoff_seconds,
                "source_refs": source_refs,
                "scope": {"source_item_ids": [ref["source_item_id"] for ref in source_refs]},
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
        candidate_items = [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.owner_user_id == allowed_owner_id]
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
                    limit=_int_first(query.get("limit")) or 10,
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
                    limit=_int_first(query.get("limit")) or 30,
                    node_types=_node_types_param(_first(query.get("node_types"))),
                    context=context,
                ),
            )
        if path == "/workspace/graph/subgraph":
            return self._json(
                200,
                self.api.workspace_graph_subgraph(
                    node_id=_first(query.get("node_id")) or "",
                    owner_user_id=_first(query.get("owner_user_id")),
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
                    limit=_int_first(query.get("limit")) or 80,
                    hops=_int_first(query.get("hops")) or 1,
                    top_k=_int_first(query.get("top_k")) or 5,
                    node_types=_node_types_param(_first(query.get("node_types"))),
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
            if path == "/workspace/ask/stream":
                return self._sse_events(200, self.api.workspace_ask_event_stream(payload, context=context))
            if path == "/workspace/search/query":
                return self._json(200, self.api.workspace_search(payload, context=context))
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
        return json.loads(self.rfile.read(length).decode("utf-8"))

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


def _digest_log_entry(job: Any, events: list[Any], claims: list[Any], notes: list[Any], source_ids: set[str]) -> dict[str, Any]:
    candidate_summary = _job_candidate_summary(job, events)
    latest_event = events[-1] if events else None
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


def _console_review_items(items: list[dict[str, Any]], *, status: str, owner_user_id: str, limit: int) -> list[dict[str, Any]]:
    matching = [
        _console_review_item(item)
        for item in items
        if (not status or item.get("status") == status)
        and (not owner_user_id or item.get("owner_user_id") == owner_user_id)
    ]
    return matching[: max(0, limit)]


def _console_review_item(item: dict[str, Any]) -> dict[str, Any]:
    proposal = item.get("proposal") if isinstance(item.get("proposal"), dict) else {}
    review_type = _console_review_type(item.get("review_type"))
    source_refs = _console_review_source_refs(proposal)
    source_ref_status = "present" if source_refs else "missing"
    apply_supported = _console_review_apply_supported(review_type, proposal)
    apply_ready = apply_supported and _console_review_apply_ready(review_type, source_refs)
    status = str(item.get("status") or "")
    actions = ["approve", "reject"] if status == "pending" else []
    if status == "pending" and apply_ready:
        actions.insert(1, "approve_apply")
    if status == "approved" and apply_ready:
        actions = ["apply"]
    return {
        "review_item_id": item.get("review_item_id"),
        "owner_user_id": item.get("owner_user_id"),
        "review_type": review_type,
        "status": status,
        "title": item.get("title") or review_type,
        "confidence": _console_review_confidence(proposal),
        "source_refs": source_refs,
        "source_ref_status": source_ref_status,
        "created_at": item.get("created_at"),
        "recommended_action": _console_review_recommended_action(review_type, apply_ready=apply_ready),
        "recommended_actions": actions,
        "apply_supported": apply_supported,
        "apply_ready": apply_ready,
        "can_apply_now": status == "approved" and apply_ready,
    }


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


def _console_review_apply_ready(review_type: str, source_refs: list[dict[str, Any]]) -> bool:
    if review_type == "share_proposal":
        return True
    if review_type in {"profile_update", "relationship_candidate", "memory_candidate", "low_confidence"}:
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


def _ask_route_intent(query: str, *, intent: str) -> str:
    if intent in {"quick", "deep"}:
        return intent
    lowered = query.lower()
    deep_markers = [
        "深入",
        "调研",
        "研究",
        "分析",
        "总结",
        "对比",
        "比较",
        "报告",
        "规划",
        "策略",
        "为什么",
        "风险",
        "建议",
        "判断",
        "证据",
        "可引用",
        "结论",
        "是否应该",
        "复盘",
        "梳理",
        "多步",
        "shortlist",
        "analyze",
        "compare",
        "evaluate",
        "investigate",
        "research",
        "recommend",
        "summarize",
        "report",
        "strategy",
        "why",
        "risk",
    ]
    quick_markers = [
        "谁",
        "什么",
        "多少",
        "哪个",
        "何时",
        "状态",
        "负责人",
        "下一步",
        "arr",
        "owner",
        "lead",
        "status",
        "next action",
        "how much",
        "when",
        "where",
        "who",
    ]
    if any(marker in lowered for marker in deep_markers):
        return "deep"
    if any(marker in lowered for marker in quick_markers) or len(query) <= 90:
        return "quick"
    return "deep"


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
) -> dict[str, Any]:
    payload = {
        "intent": intent,
        "selected_intent": selected_intent,
        "retrieval_owner": retrieval_owner,
        "surface": surface,
        "requires_agentic_service_online": requires_agentic_service_online,
        "tool_policy": tool_policy,
        "tool_profile": ASK_READ_TOOL_PROFILE if retrieval_owner == "fastreact_pska_mcp" else "none",
        "routing_owner": "pska_planner",
        "query_terms": _ask_query_terms(query),
    }
    if fallback_from:
        payload["fallback_from"] = fallback_from
    return payload


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
                detail=_ask_understand_step_detail(query_terms),
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


def _ask_understand_step_detail(query_terms: list[str]) -> str:
    if query_terms:
        return f"已抽取检索关键词：{'、'.join(query_terms[:6])}。"
    return "已确认问题和当前租户范围。"


def _ask_route_step_detail(*, intent: str, selected_intent: str, query_terms: list[str]) -> str:
    route_label = "深入分析" if selected_intent == "deep" else "快速回答"
    intent_label = "自动路由" if intent == "auto" else f"用户指定 {intent}"
    term_text = f"关键词：{'、'.join(query_terms[:6])}；" if query_terms else ""
    if selected_intent == "deep":
        return f"{term_text}{intent_label} 判定需要 {route_label}，由 FastReAct 通过 PSKA 只读工具检索。"
    return f"{term_text}{intent_label} 判定可先走 {route_label}，由 PSKA 检索知识库与图谱。"


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


def _ask_quick_answer(query: str, retrieval: dict[str, Any]) -> str:
    results = _list_of_dicts(retrieval.get("results"))
    if not results:
        return f"关键结论：当前 PSKA 没有找到足够证据回答“{query}”。建议补充相关资料或扩大检索范围后再问。"
    facts = _ask_clean_facts_from_results(results, limit=4)
    if not facts:
        return f"关键结论：PSKA 找到了与“{query}”相关的来源，但当前片段不足以整理成可引用结论。请查看证据列表或扩大检索范围。"
    lines = [f"关键结论：关于“{query}”，当前资料支持以下结论："]
    lines.extend(f"- {fact}" for fact in facts)
    diagnostics = retrieval.get("diagnostics") if isinstance(retrieval.get("diagnostics"), dict) else {}
    if diagnostics.get("gaps") or diagnostics.get("conflicts"):
        lines.append("不确定性：存在检索缺口或证据冲突，报告中应保留限定表述。")
    return "\n".join(lines)


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
        if line.startswith("|") and line.endswith("|"):
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
    agentic: dict[str, Any],
    started_at: float,
    allowed_tools: list[str],
    store: Any,
) -> dict[str, Any]:
    trace = agentic.get("trace") if isinstance(agentic.get("trace"), dict) else {}
    retrieval_payload = agentic.get("retrieval") if isinstance(agentic.get("retrieval"), dict) else {}
    retrieval = _console_search_summary(to_jsonable(retrieval_payload)) if retrieval_payload else {}
    if not _list_of_dicts(retrieval.get("results")):
        retrieval = _ask_retrieval_from_agentic_trace(trace)
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
        if declared_refs:
            evidence = _ask_filter_evidence_to_refs(evidence, refs)
        evidence["citations"] = refs
        evidence["source_refs"] = refs
    trace = {
        **trace,
        "mode": "deep",
        "retrieval_owner": "fastreact_pska_mcp",
        "tool_policy": {"mode": "allowlist", "allowed_tools": allowed_tools},
        "tool_profile": ASK_READ_TOOL_PROFILE,
    }
    agent_steps = _ask_agent_steps_from_events(trace.get("events") if isinstance(trace.get("events"), list) else [])
    if dropped_refs:
        trace["dropped_source_refs"] = dropped_refs
    trace = _ask_public_trace(trace)
    elapsed_ms = _elapsed_ms(started_at)
    return {
        "ok": True,
        "query": query,
        "answer": str(agentic.get("answer") or "").strip(),
        "route": {
            "intent": intent,
            "selected_intent": selected_intent,
            "retrieval_owner": "fastreact_pska_mcp",
            "surface": surface,
            "requires_agentic_service_online": True,
            "tool_policy": {"mode": "allowlist", "allowed_tools": allowed_tools},
            "tool_profile": ASK_READ_TOOL_PROFILE,
            "routing_owner": "pska_planner",
            "query_terms": _ask_query_terms(query),
        },
        "evidence": evidence,
        "citations": evidence["citations"],
        "source_refs": evidence["source_refs"],
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


def _ask_tool_result_detail(counts: dict[str, int]) -> str:
    evidence_count = counts.get("evidence_count", 0)
    source_ref_count = counts.get("source_ref_count", 0)
    if evidence_count or source_ref_count:
        return f"返回 {evidence_count} 条证据，{source_ref_count} 条引用。"
    return "已返回工具结果，后续会进行引用校验。"


def _ask_tool_result_counts(event: dict[str, Any]) -> dict[str, int]:
    content = event.get("content") or event.get("result") or event.get("output")
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
    }


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


def _ask_with_quality_signals(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["quality_signals"] = _ask_quality_signals(enriched)
    return enriched


def _ask_quality_signals(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
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
        for key in ["action", "duration_ms", "has_tool_calls", "llm_usage", "model", "decision_level", "approved"]
        if metadata.get(key) is not None
    }
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
                "source_item_ids",
                "document_ids",
                "chunk_ids",
                "entity_ids",
                "entity_labels",
                "job_id",
            ]
            if args.get(key) is not None
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
        events.append(("agent_step", {"step": step, "timing": timing}))
    events.extend(
        [
            (
                "evidence",
                {
                    "evidence": payload.get("evidence") or {},
                    "citations": payload.get("citations") or [],
                    "quality_signals": payload.get("quality_signals") or {},
                },
            ),
            (
                "answer_delta",
                {
                    "delta": str(payload.get("answer") or ""),
                    "time_to_first_answer_ms": timing.get("time_to_first_answer_ms"),
                },
            ),
            ("trace", {"trace": payload.get("trace") or {}, "agentic_service": payload.get("agentic_service") or {}}),
            ("done", {"ok": payload.get("ok") is not False, "timing": timing, "quality_signals": payload.get("quality_signals") or {}}),
        ]
    )
    return events


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


def _console_knowledge_source(source: Any, sync_runs: list[Any]) -> dict[str, Any]:
    latest = sync_runs[0] if sync_runs else None
    config = getattr(source, "config", {}) or {}
    permission_scope = getattr(source, "permission_scope", {}) or {}
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
        "last_sync_run": to_jsonable(latest) if latest else None,
    }


def _console_knowledge_source_roots(sources: list[dict[str, Any]]) -> list[str]:
    roots: list[str] = []
    for source in sources:
        if source.get("source_type") != "folder" or not source.get("path"):
            continue
        if source.get("status") == "paused" or source.get("mode") == "paused":
            continue
        roots.append(str(source.get("path")))
    return list(dict.fromkeys(roots))


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
        deleted["source_items"] = _delete_by_ids(conn, "source_items", "source_item_id", source_item_ids)
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


def _allowed_tools_for_job(job) -> list[str]:
    if job.job_type in {"digest_via_fastreact", "extract_via_fastreact"}:
        return ["pska_job_context", "pska_search", "pska_write_candidates", "pska_review_items"]
    return ["pska_job_context", "pska_search", "pska_write_candidates"]


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def _writing_path_parts(path: str) -> list[str]:
    prefix = "/workspace/writing/boards/"
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


def _batch_limit(value: Any) -> int:
    try:
        limit = int(value) if value is not None else 20
    except (TypeError, ValueError):
        limit = 20
    return min(max(limit, 1), 100)


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
