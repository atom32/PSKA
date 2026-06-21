from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pska_core.acl import ACLService
from pska_core.agent_capture import capture_agent_conversation
from pska_core.agentic_service import AgenticServiceError, build_agentic_service_client
from pska_core.auth import AuthError, RequestContext, authenticate_headers, context_from_headers, service_token_required
from pska_core.candidates import CandidateWriteService
from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.discovery import DiscoveryService
from pska_core.embeddings import EmbeddingConfig, build_embedding_provider
from pska_core.extraction import ExtractionService
from pska_core.fastreact_protocol import compact_trace_for_context
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.memory import MemoryService
from pska_core.mcp_server import MCPServer, PROTOCOL_VERSION
from pska_core.models import ChannelIngestPayload, ReviewItem, SourceRef, WorkspaceActivityEvent
from pska_core.offline_index import OfflineIndexService
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.serde import to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


class PSKAApi:
    def __init__(self, database_url: str) -> None:
        self.store = PostgresKnowledgeStore(database_url)
        embedding_provider = build_embedding_provider(EmbeddingConfig.from_env())
        self.retrieval = RetrievalService(self.store, ACLService(self.store), embedding_provider=embedding_provider)
        self.agentic_service = build_agentic_service_client()
        self.ingest = IngestService(self.store, embedding_provider=embedding_provider)
        self.extraction = ExtractionService(self.store)
        self.jobs = JobService(self.store)
        self.reviews = ReviewService(self.store)
        self.memory = MemoryService(self.store)
        self.candidates = CandidateWriteService(self.store)
        self.mcp = MCPServer(database_url, store=self.store)

    def health(self) -> dict[str, Any]:
        return {"ok": True, "database": getattr(self.store, "database_url", "in_memory")}

    def ready(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "database": self._database_ready(),
            "schema": self._schema_ready(),
            "index": self._index_ready(),
            "embedding": {
                "provider": os.environ.get("PSKA_EMBEDDING_PROVIDER", "disabled"),
                "configured": bool(os.environ.get("PSKA_EMBEDDING_PROVIDER")),
            },
            "llm": {
                "api_key_file_configured": bool(os.environ.get("PSKA_LLM_API_KEY_FILE")),
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
            "review_items": self.store.count_table("review_items"),
            "jobs": self.store.count_table("jobs"),
            "offline_index_states": self.store.count_table("offline_index_states"),
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
            "entities",
            "hyperedges",
            "review_items",
            "jobs",
            "connector_states",
            "offline_index_states",
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
            return {"ok": False, "provider": os.environ.get("PSKA_AGENTIC_SERVICE_PROVIDER", "fastreact"), "error": str(exc)}

    def _mcp_ready(self) -> dict[str, Any]:
        response = self.mcp.handle({"jsonrpc": "2.0", "id": "ready", "method": "tools/list", "params": {}})
        tools = ((response or {}).get("result") or {}).get("tools") or []
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        required = ["pska_search", "pska_index_status", "pska_job_context", "pska_write_candidates"]
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

    def ingest_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        owner_user_id: str | None = None,
        connector_id: str | None = None,
        connector_state_id: str | None = None,
    ) -> dict[str, Any]:
        if connector_state_id:
            return {"connector_state": to_jsonable(self.store.get_connector_state(connector_state_id))}
        return {
            "connector_states": to_jsonable(
                self.store.list_connector_states(owner_user_id=owner_user_id, connector_id=connector_id)
            )
        }

    def search(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        user = self.store.get_user(payload.get("user_id") or "user_primary")
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
        user = self.store.get_user(payload.get("user_id") or "user_primary")
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
    ) -> dict[str, Any]:
        response = self.agentic_service.search(
            query,
            user,
            represented_user_id=represented_user_id,
            max_iterations=max_iterations,
        )
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

    def extract_all(self, payload: dict[str, Any]) -> dict[str, Any]:
        reports = self.extraction.extract_all_visible(owner_user_id=payload.get("owner_user_id"))
        return {"reports": to_jsonable(reports), "index_status": self.index_status()}

    def review_items(self) -> dict[str, Any]:
        return {"review_items": to_jsonable(self.store.list_review_items())}

    def propose_profile_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_delta = payload.get("profile_delta") or payload.get("profile")
        if not isinstance(profile_delta, dict) or not profile_delta:
            raise ValueError("profile_delta must be a non-empty object")

        result = self.memory.propose_profile_update(
            owner_user_id=str(payload.get("owner_user_id") or "user_primary"),
            profile_delta=profile_delta,
            source_refs=_source_refs_from_payload(payload.get("source_refs")),
            sensitivity=str(payload.get("sensitivity") or "normal"),
            confidence=float(payload.get("confidence", 0.8)),
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
        return {"review_item": to_jsonable(review_item)}

    def reject_review_item(self, review_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "")
        review_item = self.reviews.reject(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"review_item": to_jsonable(review_item)}

    def apply_review_item(self, review_item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor_user_id = str(payload.get("actor_user_id") or "user_primary")
        reason = str(payload.get("reason") or "")
        review_item = self.reviews.apply(review_item_id, actor_user_id=actor_user_id, reason=reason)
        return {"review_item": to_jsonable(review_item)}

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.submit(
            str(payload["job_type"]),
            dict(payload.get("payload") or {}),
            max_attempts=int(payload.get("max_attempts") or 3),
            priority=int(payload.get("priority") or (payload.get("payload") or {}).get("priority") or 0),
        )
        return {"job": to_jsonable(job)}

    def run_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        report = self.jobs.run_available(limit=int(payload.get("limit") or 1))
        return {"run": to_jsonable(report)}

    def job_status(self, job_id: str | None = None, *, status: str | None = None, job_type: str | None = None, limit: int = 50) -> dict[str, Any]:
        if job_id:
            return {
                "job": to_jsonable(self.store.get_job(job_id)),
                "events": to_jsonable(self.store.list_job_events(job_id)),
            }
        return {"jobs": to_jsonable(self.store.list_jobs(status=status, job_type=job_type, limit=limit))}

    def metrics(self) -> dict[str, Any]:
        source_items = self.store.list_source_items()
        chunks = self.store.list_chunks_for_sources({item.source_item_id for item in source_items})
        return {
            "index": self.index_status(),
            "offline_index": OfflineIndexService(self.store).freshness(),
            "embedding": _embedding_metrics(chunks),
            "connectors": _connector_metrics(source_items, self.store.list_connector_states()),
            "jobs": self.job_stats()["stats"],
        }

    def console_dashboard(self, *, owner_user_id: str = "user_primary", limit: int = 5) -> dict[str, Any]:
        limit = max(0, limit)
        try:
            ready = self.ready()
        except Exception as exc:  # noqa: BLE001 - console should explain local service failures.
            ready = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "checks": {}}
        try:
            metrics = self.metrics()
        except Exception as exc:  # noqa: BLE001
            metrics = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "index": {}, "connectors": {}}
        try:
            stats = self.job_stats()["stats"]
        except Exception as exc:  # noqa: BLE001
            stats = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "by_status": {}, "digest_backlog": {}}
        try:
            reviews = self.review_items()
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
                "recent_sources": _console_recent_sources(self.store.list_source_items(), owner_user_id=owner_user_id, limit=limit),
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

    def job_stats(self, *, limit: int = 1000) -> dict[str, Any]:
        jobs = self.store.list_jobs(limit=limit)
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

    def console_reviews(self, *, status: str = "pending", owner_user_id: str = "user_primary", limit: int = 50) -> dict[str, Any]:
        limit = max(0, limit)
        items = _console_review_items(
            to_jsonable(self.store.list_review_items()),
            status=status,
            owner_user_id=owner_user_id,
            limit=limit,
        )
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "status": status,
            "review_items": items,
            "count": len(items),
            "total_matching": len(
                _console_review_items(
                    to_jsonable(self.store.list_review_items()),
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
        represented_user_id = payload.get("represented_user_id")
        mode = str(payload.get("mode") or "direct")
        user = self.store.get_user(user_id)
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
                return {
                    "ok": False,
                    "mode": "agentic",
                    "display_mode": "direct_fallback",
                    "requires_agentic_service_online": True,
                    "query": query,
                    "error": {
                        "type": "agentic_service_unavailable",
                        "message": "Agentic service is unavailable. Direct retrieval fallback is shown.",
                        "detail": str(exc),
                    },
                    "fallback": {
                        "mode": "direct",
                        "display_mode": "direct_fallback",
                        "retrieval": _console_search_summary(to_jsonable(fallback)),
                    },
                }
            retrieval_payload = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
            result["retrieval"] = _console_search_summary(retrieval_payload)
            if payload.get("capture"):
                captured = capture_agent_conversation(
                    self.store,
                    owner_user_id=str(represented_user_id or user_id),
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

    def workspace_today(
        self,
        *,
        owner_user_id: str | None = None,
        limit: int = 10,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        limit = max(1, min(limit, 50))
        dashboard = self.console_dashboard(owner_user_id=owner_user_id, limit=limit)
        reviews = self.console_reviews(status="pending", owner_user_id=owner_user_id, limit=limit)
        corpus = self.workspace_corpus(owner_user_id=owner_user_id, limit=limit, context=context)
        activity = self.workspace_activity(owner_user_id=owner_user_id, limit=limit, context=context)
        discoveries = self.workspace_discoveries(owner_user_id=owner_user_id, limit=limit, context=context)
        stats = self.job_stats()["stats"]
        review_items = reviews.get("review_items") if isinstance(reviews.get("review_items"), list) else []
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
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
        limit: int = 50,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, owner_user_id)
        DiscoveryService(self.store, owner_user_id=owner_user_id).produce()
        since = datetime.now(UTC) - timedelta(days=7)
        items = self.store.list_discovery_items(
            owner_user_id=owner_user_id,
            status="new",
            since=since,
            limit=max(1, min(limit, 100)),
        )
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "window_days": 7,
            "discoveries": [_discovery_item_payload(item) for item in items],
            "count": len(items),
        }

    def record_workspace_activity(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        owner_user_id = _workspace_owner_user_id(context, payload.get("owner_user_id"))
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
        events = self.store.list_workspace_activity_events(
            owner_user_id=owner_user_id,
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
        limit = max(1, min(limit, 100))
        query_text = str(query or "").strip().lower()
        channel = str(source_channel or "").strip()
        all_sources = [item for item in self.store.list_source_items() if item.owner_user_id == owner_user_id]
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
            self.store.list_agent_memories(owner_user_id=owner_user_id),
            key=lambda memory: (float(getattr(memory, "confidence", 0.0) or 0.0), getattr(memory, "agent_memory_id", "")),
            reverse=True,
        )[:limit]
        profiles = sorted(
            self.store.list_profile_cards(owner_user_id=owner_user_id),
            key=lambda card: (float(getattr(card, "confidence", 0.0) or 0.0), getattr(card, "profile_card_id", "")),
            reverse=True,
        )[:limit]
        entities = [entity for entity in self.store.list_entities() if getattr(entity, "owner_user_id", "") == owner_user_id]
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

    def workspace_writer_suggest(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        payload = context.apply_to_payload(payload) if context else payload
        selected_text = str(payload.get("selected_text") or "").strip()
        draft_text = str(payload.get("draft_text") or "").strip()
        instruction = str(payload.get("instruction") or "请基于 PSKA 证据给出中文写作建议。").strip()
        query = str(payload.get("query") or "").strip() or _workspace_writer_query(selected_text, draft_text, instruction)
        if not selected_text and not query:
            raise ValueError("selected_text or query is required")
        user_id = str(payload.get("user_id") or "user_primary")
        represented_user_id = payload.get("represented_user_id")
        user = self.store.get_user(user_id)
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

    def console_memory(self, *, owner_user_id: str = "user_primary", limit: int = 50) -> dict[str, Any]:
        limit = max(0, limit)
        memories = sorted(
            self.store.list_agent_memories(owner_user_id=owner_user_id),
            key=lambda memory: (
                memory.confidence,
                memory.last_verified_at.isoformat() if memory.last_verified_at else "",
                memory.agent_memory_id,
            ),
            reverse=True,
        )[:limit]
        profile_cards = sorted(
            self.store.list_profile_cards(owner_user_id=owner_user_id),
            key=lambda card: (card.confidence, card.profile_card_id),
            reverse=True,
        )[:limit]
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "read_only": True,
            "agent_memories": [_console_agent_memory(memory) for memory in memories],
            "profile_cards": [_console_profile_card(card) for card in profile_cards],
            "memory_count": len(memories),
            "profile_count": len(profile_cards),
            "limit": limit,
        }

    def console_jobs(self, *, limit: int = 20) -> dict[str, Any]:
        limit = max(1, limit)
        try:
            ready = self.ready()
        except Exception as exc:  # noqa: BLE001
            ready = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "checks": {}}
        stats = self.job_stats(limit=1000)["stats"]
        failed_jobs = [to_jsonable(job) for job in self.store.list_jobs(status="failed", limit=limit)]
        running_jobs = [to_jsonable(job) for job in self.store.list_jobs(status="running", limit=limit)]
        stale_running = stats.get("stale_running") or [
            _console_job_summary(job)
            for job in self.store.list_jobs(status="running", limit=limit)
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

    def console_sources(self, *, owner_user_id: str = "user_primary", limit: int = 20) -> dict[str, Any]:
        limit = max(1, limit)
        source_items = self.store.list_source_items()
        connector_states = self.store.list_connector_states(owner_user_id=owner_user_id)
        metrics = _connector_metrics(source_items, connector_states)
        recent_sources = _console_recent_sources(source_items, owner_user_id=owner_user_id, limit=limit)
        states = [_console_connector_state(state) for state in connector_states[:limit]]
        files_roots = _console_files_roots(states)
        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "read_only": True,
            "source_counts": {
                "source_items": self.store.count_table("source_items"),
                "documents": self.store.count_table("documents"),
                "chunks": self.store.count_table("chunks"),
            },
            "source_channels": metrics.get("source_channels") or {},
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
            "recommended_commands": [
                "./scripts/pska connector-state list --owner-user-id user_primary",
                *_console_files_commands(files_roots),
            ],
            "notes": [
                "This page is read-only and does not add connector scope.",
                "Authorize new files roots explicitly with files-sync or files-scan before expecting them here.",
            ],
        }

    def schedule_digest(self, payload: dict[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        owner_user_id = _owner_user_id_for_write(payload, context)
        source_item_ids = _string_list(payload.get("source_item_ids"))
        scoped_source_item_ids = set(_string_list(context.scope.get("source_item_ids"))) if context and context.scope else set()
        force = bool(payload.get("force", False))
        limit = _batch_limit(payload.get("limit") or 20)
        batch_size = _batch_limit(payload.get("batch_size") or payload.get("limit") or 20)
        priority = int(payload.get("priority") or 0)
        max_attempts = int(payload.get("max_attempts") or 3)
        retry_backoff_seconds = int(payload.get("retry_backoff_seconds") or payload.get("backoff_seconds") or 60)
        quota = _digest_schedule_quota(self.store, owner_user_id=owner_user_id, payload=payload, force=force)
        if quota["limited"]:
            return {
                "job": None,
                "owner_user_id": owner_user_id,
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

        source_items = [item for item in self.store.list_source_items() if item.owner_user_id == owner_user_id]
        if source_item_ids:
            requested = set(source_item_ids)
            source_items = [item for item in source_items if item.source_item_id in requested]
        if scoped_source_item_ids:
            source_items = [item for item in source_items if item.source_item_id in scoped_source_item_ids]
        source_items = sorted(source_items, key=lambda item: (item.created_at, item.source_item_id), reverse=True)

        coverage = {} if force else _digest_source_coverage(self.store)
        skipped_items = [
            _digest_source_explanation(item, selected=False, reason=coverage[item.source_item_id]["reason"], job=coverage[item.source_item_id]["job"])
            for item in source_items
            if item.source_item_id in coverage
        ]
        eligible = [item for item in source_items if force or item.source_item_id not in coverage]
        selected = eligible[:limit]
        selected_items = [
            _digest_source_explanation(
                item,
                selected=True,
                reason="force_selected" if force else "new_or_triggered_source",
                job=None,
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
        user_id = context.effective_user_id if context else str(job.payload.get("owner_user_id") or "user_primary")
        represented_user_id = context.represented_user_id if context else None
        allowed_owner_id = represented_user_id or user_id
        source_item_ids = _job_source_item_ids(job)
        candidate_items = [item for item in self.store.list_source_items() if item.owner_user_id == allowed_owner_id]
        if source_item_ids:
            candidate_items = [item for item in candidate_items if item.source_item_id in source_item_ids]
        candidate_items = sorted(candidate_items, key=lambda item: (item.created_at, item.source_item_id))
        offset = _cursor_offset(cursor)
        batch_size = _batch_limit(limit if limit is not None else (job.payload.get("batch_size") if isinstance(job.payload, dict) else None))
        source_items = candidate_items[offset : offset + batch_size]
        next_offset = offset + len(source_items)
        has_more = next_offset < len(candidate_items)
        chunks = self.store.list_chunks_for_sources({item.source_item_id for item in source_items})
        return {
            "job": to_jsonable(job),
            "request_user_id": allowed_owner_id,
            "source_items": to_jsonable(source_items),
            "chunks": to_jsonable(chunks),
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

    def complete_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload.get("result") or {})
        if payload.get("summary") is not None:
            result.setdefault("summary", payload.get("summary"))
        return {"job": to_jsonable(self.store.finish_job(job_id, result))}

    def fail_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        error = str(payload.get("error") or "job failed")
        retryable = bool(payload.get("retryable", True))
        return {"job": to_jsonable(self.store.fail_job(job_id, error, retryable=retryable))}

    def retry_job(self, job_id: str) -> dict[str, Any]:
        return {"job": to_jsonable(self.store.retry_job(job_id))}

    def cancel_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"job": to_jsonable(self.store.cancel_job(job_id, reason=str(payload.get("reason") or "")))}

    def recover_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        jobs = self.store.recover_stale_jobs(max_age_seconds=int(payload.get("max_age_seconds") or 3600))
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
                    limit=_int_first(query.get("limit")) or 5,
                ),
            )
        if path == "/console/reviews/data":
            return self._json(
                200,
                self.api.console_reviews(
                    status=_first(query.get("status")) or "pending",
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    limit=_int_first(query.get("limit")) or 50,
                ),
            )
        if path == "/console/memory/data":
            return self._json(
                200,
                self.api.console_memory(
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
                    limit=_int_first(query.get("limit")) or 50,
                ),
            )
        if path == "/console/jobs/data":
            return self._json(200, self.api.console_jobs(limit=_int_first(query.get("limit")) or 20))
        if path == "/console/sources/data":
            return self._json(
                200,
                self.api.console_sources(
                    owner_user_id=_first(query.get("owner_user_id")) or "user_primary",
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
        if path == "/review-items":
            return self._json(200, self.api.review_items())
        if path == "/connectors/states":
            return self._json(
                200,
                self.api.connector_states(
                    owner_user_id=_first(query.get("owner_user_id")),
                    connector_id=_first(query.get("connector_id")),
                ),
            )
        if path.startswith("/connectors/states/"):
            return self._json(200, self.api.connector_states(connector_state_id=path.removeprefix("/connectors/states/")))
        if path == "/jobs/stats":
            return self._json(200, self.api.job_stats(limit=_int_first(query.get("limit")) or 1000))
        if path == "/jobs":
            return self._json(
                200,
                self.api.job_status(
                    status=_first(query.get("status")),
                    job_type=_first(query.get("job_type")),
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
            return self._json(200, self.api.job_status(path.removeprefix("/jobs/")))
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
                return self._json(200, self.api.ingest_payload(payload))
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
            if path == "/workspace/search/query":
                return self._json(200, self.api.workspace_search(payload, context=context))
            if path == "/workspace/writer/suggest":
                return self._json(200, self.api.workspace_writer_suggest(payload, context=context))
            if path == "/workspace/activity":
                return self._json(200, self.api.record_workspace_activity(payload, context=context))
            if path == "/extract/all":
                return self._json(200, self.api.extract_all(payload))
            if path == "/profile/update-proposals":
                return self._json(200, self.api.propose_profile_update(payload))
            if path == "/candidates":
                return self._json(200, self.api.write_candidates(payload, context=context))
            if path == "/digest/candidates":
                return self._json(200, self.api.write_candidates(payload, context=context))
            if path == "/digest/schedule":
                return self._json(200, self.api.schedule_digest(payload, context=context))
            if path == "/jobs":
                return self._json(200, self.api.submit_job(payload))
            if path == "/jobs/run":
                return self._json(200, self.api.run_jobs(payload))
            if path == "/jobs/recover":
                return self._json(200, self.api.recover_jobs(payload))
            if path == "/jobs/recover-stale":
                return self._json(200, self.api.recover_jobs(payload))
            if path.startswith("/jobs/") and path.endswith("/lease"):
                job_id = path.removeprefix("/jobs/").removesuffix("/lease")
                return self._json(200, self.api.lease_job(job_id, payload, context=context))
            if path.startswith("/jobs/") and path.endswith("/complete"):
                job_id = path.removeprefix("/jobs/").removesuffix("/complete")
                return self._json(200, self.api.complete_job(job_id, payload))
            if path.startswith("/jobs/") and path.endswith("/fail"):
                job_id = path.removeprefix("/jobs/").removesuffix("/fail")
                return self._json(200, self.api.fail_job(job_id, payload))
            if path.startswith("/jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/jobs/").removesuffix("/cancel")
                return self._json(200, self.api.cancel_job(job_id, payload))
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
                return self._json(200, self.api.retry_job(job_id))
            self._json(404, {"error": f"not found: {path}"})
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
        try:
            authenticated = authenticate_headers(self.headers)
        except AuthError as exc:
            self._json(401, {"error": str(exc)})
            return None
        if service_token_required() and not authenticated:
            self._json(401, {"error": "PSKA service token required"})
            return None
        context = context_from_headers(self.headers, payload, service_authenticated=authenticated)
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
        }
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        self._request_meta["logged"] = True


def serve(host: str = "127.0.0.1", port: int = 8765, database_url: str | None = None) -> None:
    api = PSKAApi(database_url or os.environ.get("PSKA_DATABASE_URL", "postgresql:///pska"))

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
    return {
        "response_answer_chars": len(str(payload.get("answer") or "")),
        "response_event_count": trace.get("event_count") or len(events) or None,
        "response_tool_call_count": len(tool_calls) if tool_calls else None,
        "response_display_mode": payload.get("display_mode"),
    }


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


def _digest_source_coverage(store: PostgresKnowledgeStore) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    priority = {"queued": 0, "running": 1, "succeeded": 2, "failed": 3, "canceled": 4}
    for job in store.list_jobs(job_type=DIGEST_VIA_FASTREACT, limit=10000):
        reason = _digest_coverage_reason(job.status)
        for source_item_id in _job_source_item_ids(job):
            current = coverage.get(source_item_id)
            if current is None or priority.get(job.status, 99) < priority.get(current["job"].status, 99):
                coverage[source_item_id] = {"reason": reason, "job": job}
    return coverage


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
    }
    if job is not None:
        payload["covering_job"] = {
            "job_id": job.job_id,
            "status": job.status,
            "job_type": job.job_type,
            "updated_at": job.updated_at,
        }
    return payload


def _digest_budget_policy(*, limit: int, batch_size: int, force: bool) -> dict[str, Any]:
    return {
        "dedupe": "skip any source already covered by digest_via_fastreact unless force=true",
        "successful_source_repeat": "skip completed digest sources until force=true or a future trigger policy selects them",
        "failed_source_repeat": "skip failed digest sources unless force=true to avoid infinite retry loops",
        "frequency": "optional quota_window_seconds/max_jobs_per_window limits new jobs",
        "max_source_items": limit,
        "max_source_items_per_job": batch_size,
        "token_budget": "not enforced yet; digest worker owns model token limits",
        "trigger_policy": "new_or_explicit_source_ids; similarity/tag/entity triggers are reserved for a later policy revision",
        "force": force,
    }


def _digest_schedule_quota(store: PostgresKnowledgeStore, *, owner_user_id: str, payload: dict[str, Any], force: bool) -> dict[str, Any]:
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
    for job in store.list_jobs(job_type=DIGEST_VIA_FASTREACT, limit=10000):
        job_payload = job.payload if isinstance(job.payload, dict) else {}
        if str(job_payload.get("owner_user_id") or "") != owner_user_id:
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


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _embedding_metrics(chunks: list[Any]) -> dict[str, Any]:
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
    diagnostics = score_debug.get("diagnostics") if isinstance(score_debug.get("diagnostics"), dict) else {}
    return {
        "request_user_id": payload.get("request_user_id"),
        "results": [
            {
                "title": result.get("title"),
                "snippet": result.get("snippet"),
                "score": result.get("score"),
                "citation": _console_citation(result.get("citation")),
            }
            for result in _list_of_dicts(payload.get("results"))
        ],
        "citations": [_console_citation(citation) for citation in _list_of_dicts(payload.get("citations"))],
        "graph_paths": [_console_graph_path(path) for path in _list_of_dicts(payload.get("graph_paths"))],
        "diagnostics": {
            "gaps": list(payload.get("gaps") or []),
            "conflicts": list(payload.get("conflicts") or []),
            "sensitivity": list(payload.get("sensitivity") or []),
            "score_debug": diagnostics,
        },
        "memory_context": [_console_memory_context(item) for item in _list_of_dicts(payload.get("memory_context"))],
        "profile_context": [_console_memory_context(item) for item in _list_of_dicts(payload.get("profile_context"))],
    }


def _console_citation(value: Any) -> dict[str, Any]:
    citation = value if isinstance(value, dict) else {}
    return {
        "source_item_id": citation.get("source_item_id"),
        "chunk_id": citation.get("chunk_id"),
        "title": citation.get("title"),
        "url": citation.get("url"),
        "snippet": citation.get("snippet"),
    }


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


def _workspace_owner_user_id(context: RequestContext | None, requested_owner_user_id: str | None) -> str:
    if context is None:
        return requested_owner_user_id or "user_primary"
    if context.caller == "agent_service":
        return context.represented_user_id or "agent_service"
    return requested_owner_user_id or context.represented_user_id or context.user_id


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
        "producer": item.producer,
        "created_at": item.created_at.isoformat(),
        "status": item.status,
        "label": _discovery_type_label(discovery_type),
        "summary": _discovery_summary(discovery_type, evidence),
        "evidence_count": len(evidence),
    }


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


def _console_files_roots(states: list[dict[str, Any]]) -> list[str]:
    roots: list[str] = []
    for state in states:
        connector_id = str(state.get("connector_id") or "")
        if connector_id and connector_id != "files":
            continue
        roots.extend(_string_list(state.get("roots")))
    env_roots = os.getenv("PSKA_FILES_ROOTS")
    if env_roots:
        roots.extend([root for root in env_roots.split(os.pathsep) if root])
    return list(dict.fromkeys(roots))


def _console_files_commands(roots: list[str]) -> list[str]:
    if not roots:
        return ["./scripts/pska files-sync --root <authorized-root>"]
    root_args = " ".join(f"--root {root}" for root in roots)
    return [
        f"./scripts/pska files-sync {root_args}",
        f"./scripts/pska files-watch {root_args} --initial-sync",
    ]


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


def _int_first(values: list[str] | None) -> int | None:
    value = _first(values)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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
      return event.content || event.metadata?.final_content || event.metadata?.final || "";
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
