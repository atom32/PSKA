from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.auth import AuthError, RequestContext, authenticate_headers, context_from_headers, service_token_required
from pska_core.candidates import CandidateWriteService
from pska_core.connectors import connector_state_from_mapping, connector_record_to_payload
from pska_core.embeddings import EmbeddingConfig, build_embedding_provider
from pska_core.extraction import ExtractionService
from pska_core.fastreact_client import FastreactError, HttpFastreactClient
from pska_core.ingest import IngestService
from pska_core.jobs import DIGEST_VIA_FASTREACT, JobService
from pska_core.memory import MemoryService
from pska_core.mcp_server import MCPServer, PROTOCOL_VERSION
from pska_core.models import ChannelIngestPayload, ReviewItem, SourceRef
from pska_core.retrieval import RetrievalService
from pska_core.review import ReviewService
from pska_core.serde import to_jsonable
from pska_core.store_postgres import PostgresKnowledgeStore


class PSKAApi:
    def __init__(self, database_url: str) -> None:
        self.store = PostgresKnowledgeStore(database_url)
        embedding_provider = build_embedding_provider(EmbeddingConfig.from_env())
        self.retrieval = RetrievalService(self.store, ACLService(self.store), embedding_provider=embedding_provider)
        self.agentic = AgenticSearchService(self.retrieval)
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
            "fastreact": self._fastreact_ready(),
            "mcp": self._mcp_ready(),
        }
        required_ok = checks["database"]["ok"] and checks["schema"]["ok"] and checks["mcp"]["ok"]
        return {"ok": required_ok, "checks": checks}

    def index_status(self) -> dict[str, int]:
        return {
            "source_items": self.store.count_table("source_items"),
            "documents": self.store.count_table("documents"),
            "chunks": self.store.count_table("chunks"),
            "entities": self.store.count_table("entities"),
            "hyperedges": self.store.count_table("hyperedges"),
            "review_items": self.store.count_table("review_items"),
            "jobs": self.store.count_table("jobs"),
        }

    def _database_ready(self) -> dict[str, Any]:
        try:
            return {"ok": True, "source_items": self.store.count_table("source_items")}
        except Exception as exc:  # noqa: BLE001 - readiness reports dependency failures.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _schema_ready(self) -> dict[str, Any]:
        tables = ["source_items", "documents", "chunks", "entities", "hyperedges", "review_items", "jobs", "connector_states"]
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
            return {"ok": True, "counts": self.index_status()}
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

    def _fastreact_ready(self) -> dict[str, Any]:
        try:
            return HttpFastreactClient().ready()
        except FastreactError as exc:
            return {"ok": False, "url": os.environ.get("PSKA_FASTREACT_URL", "http://127.0.0.1:8000"), "error": str(exc)}

    def _mcp_ready(self) -> dict[str, Any]:
        response = self.mcp.handle({"jsonrpc": "2.0", "id": "ready", "method": "tools/list", "params": {}})
        tools = ((response or {}).get("result") or {}).get("tools") or []
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        required = ["pska_search", "pska_agentic_search", "pska_index_status", "pska_job_context", "pska_write_candidates"]
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
        return to_jsonable(
            self.agentic.search(
                payload["query"],
                user,
                represented_user_id=payload.get("represented_user_id"),
                max_iterations=int(payload.get("max_iterations") or 3),
            )
        )

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
            "embedding": _embedding_metrics(chunks),
            "connectors": _connector_metrics(source_items, self.store.list_connector_states()),
            "jobs": self.job_stats()["stats"],
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

        source_items = [item for item in self.store.list_source_items() if item.owner_user_id == owner_user_id]
        if source_item_ids:
            requested = set(source_item_ids)
            source_items = [item for item in source_items if item.source_item_id in requested]
        if scoped_source_item_ids:
            source_items = [item for item in source_items if item.source_item_id in scoped_source_item_ids]
        source_items = sorted(source_items, key=lambda item: (item.created_at, item.source_item_id), reverse=True)

        already_scheduled = set() if force else _active_digest_source_item_ids(self.store)
        skipped_source_item_ids = [item.source_item_id for item in source_items if item.source_item_id in already_scheduled]
        selected = [item for item in source_items if force or item.source_item_id not in already_scheduled][:limit]
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
                },
            )

        return {
            "job": to_jsonable(job) if job else None,
            "owner_user_id": owner_user_id,
            "scheduled_source_item_ids": [ref["source_item_id"] for ref in source_refs],
            "skipped_source_item_ids": skipped_source_item_ids,
            "force": force,
            "limit": limit,
            "batch_size": batch_size,
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


def _active_digest_source_item_ids(store: PostgresKnowledgeStore) -> set[str]:
    ids: set[str] = set()
    for job in store.list_jobs(job_type=DIGEST_VIA_FASTREACT, limit=10000):
        if job.status in {"queued", "running", "succeeded"}:
            ids.update(_job_source_item_ids(job))
    return ids


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
        return ["pska_job_context", "pska_search", "pska_agentic_search", "pska_write_candidates", "pska_review_items"]
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
