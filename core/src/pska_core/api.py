from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any
from urllib.parse import urlparse

from pska_core.acl import ACLService
from pska_core.agentic import AgenticSearchService
from pska_core.embeddings import EmbeddingConfig, build_embedding_provider
from pska_core.extraction import ExtractionService
from pska_core.ingest import IngestService
from pska_core.jobs import JobService
from pska_core.memory import MemoryService
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

    def health(self) -> dict[str, Any]:
        return {"ok": True, "database": self.store.database_url}

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

    def ingest_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = self.ingest.ingest_channel_payload(ChannelIngestPayload.from_mapping(payload))
        return to_jsonable(item)

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        user = self.store.get_user(payload.get("user_id") or "user_primary")
        return to_jsonable(
            self.retrieval.search(
                payload["query"],
                user,
                represented_user_id=payload.get("represented_user_id"),
                top_k=int(payload.get("top_k") or 5),
            )
        )

    def agentic_search(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    def retry_job(self, job_id: str) -> dict[str, Any]:
        return {"job": to_jsonable(self.store.retry_job(job_id))}

    def recover_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        jobs = self.jobs.recover_stale(max_age_seconds=int(payload.get("max_age_seconds") or 3600))
        return {"recovered": to_jsonable(jobs)}


class PSKARequestHandler(BaseHTTPRequestHandler):
    api: PSKAApi

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, self.api.health())
        if path == "/index-status":
            return self._json(200, self.api.index_status())
        if path == "/review-items":
            return self._json(200, self.api.review_items())
        if path == "/jobs":
            return self._json(200, self.api.job_status())
        if path.startswith("/jobs/"):
            return self._json(200, self.api.job_status(path.removeprefix("/jobs/")))
        self._json(404, {"error": f"not found: {path}"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/ingest/channel-payload":
                return self._json(200, self.api.ingest_payload(payload))
            if path == "/search":
                return self._json(200, self.api.search(payload))
            if path == "/agentic-search":
                return self._json(200, self.api.agentic_search(payload))
            if path == "/extract/all":
                return self._json(200, self.api.extract_all(payload))
            if path == "/profile/update-proposals":
                return self._json(200, self.api.propose_profile_update(payload))
            if path == "/jobs":
                return self._json(200, self.api.submit_job(payload))
            if path == "/jobs/run":
                return self._json(200, self.api.run_jobs(payload))
            if path == "/jobs/recover":
                return self._json(200, self.api.recover_jobs(payload))
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

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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
