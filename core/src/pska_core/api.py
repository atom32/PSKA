from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.auth import AuthError, RequestContext, authenticate_headers, context_from_headers, service_token_required
from pska_core.candidates import CandidateWriteService
from pska_core.embeddings import EmbeddingConfig, build_embedding_provider
from pska_core.extraction import ExtractionService
from pska_core.fastreact_client import FastreactError, HttpFastreactClient
from pska_core.ingest import IngestService
from pska_core.jobs import JobService
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
        tables = ["source_items", "documents", "chunks", "entities", "hyperedges", "review_items", "jobs"]
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
            jobs = self.store.list_jobs(limit=1000)
            by_status = {status: 0 for status in ["queued", "running", "failed", "succeeded", "canceled"]}
            by_type: dict[str, int] = {}
            worker_ids: set[str] = set()
            stale_running: list[dict[str, Any]] = []
            now = datetime.now(UTC)
            for job in jobs:
                by_status[job.status] = by_status.get(job.status, 0) + 1
                by_type[job.job_type] = by_type.get(job.job_type, 0) + 1
                if job.worker_id:
                    worker_ids.add(job.worker_id)
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
                "ok": True,
                "sample_size": len(jobs),
                "by_status": by_status,
                "by_type": by_type,
                "active_worker_ids": sorted(worker_ids),
                "running_stale_count": len(stale_running),
                "stale_running": stale_running[:10],
                "recent_failed": recent_failed,
            }
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

    def job_status(self, job_id: str | None = None) -> dict[str, Any]:
        if job_id:
            return {
                "job": to_jsonable(self.store.get_job(job_id)),
                "events": to_jsonable(self.store.list_job_events(job_id)),
            }
        return {"jobs": to_jsonable(self.store.list_jobs())}

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

    def recover_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        jobs = self.jobs.recover_stale(max_age_seconds=int(payload.get("max_age_seconds") or 3600))
        return {"recovered": to_jsonable(jobs)}


class PSKARequestHandler(BaseHTTPRequestHandler):
    api: PSKAApi

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        if path == "/health":
            return self._json(200, self.api.health())
        context = self._context({})
        if context is None:
            return
        if path == "/ready":
            return self._json(200, self.api.ready())
        if path == "/index-status":
            return self._json(200, self.api.index_status())
        if path == "/review-items":
            return self._json(200, self.api.review_items())
        if path == "/jobs":
            return self._json(200, self.api.job_status())
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
            if path == "/jobs":
                return self._json(200, self.api.submit_job(payload))
            if path == "/jobs/run":
                return self._json(200, self.api.run_jobs(payload))
            if path == "/jobs/recover":
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
        return context_from_headers(self.headers, payload, service_authenticated=authenticated)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("content-length", "0")
        self.end_headers()


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
