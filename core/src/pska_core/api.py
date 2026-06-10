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
from pska_core.models import ChannelIngestPayload
from pska_core.retrieval import RetrievalService
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
