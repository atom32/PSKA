from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Mapping
from uuid import uuid5, NAMESPACE_URL

from pska_core.chunking import ChunkSpan, chunk_text
from pska_core.models import DEFAULT_TENANT_ID, ChannelIngestPayload, Chunk, Document, SourceItem
from pska_core.offline_index import OfflineIndexService
from pska_core.processing import normalize_chunking_config, resolve_processing_config
from pska_core.store import KnowledgeStore
from pska_core.embeddings import EmbeddingProvider


POSTGRES_NUL_REPLACEMENT = "\uFFFD"


def postgres_safe_text(value: str) -> str:
    return value.replace("\x00", POSTGRES_NUL_REPLACEMENT)


def postgres_safe_json(value: Any) -> Any:
    if isinstance(value, str):
        return postgres_safe_text(value)
    if isinstance(value, dict):
        return {
            postgres_safe_text(str(key)): postgres_safe_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [postgres_safe_json(item) for item in value]
    if isinstance(value, tuple):
        return [postgres_safe_json(item) for item in value]
    return value


class IngestService:
    """Converts channel payloads into source items, documents, and chunks."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        chunk_chars: int | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        processing_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.processing_config = resolve_processing_config(processing_config)
        chunking_config = dict(self.processing_config.get("chunking") or {})
        if chunk_size is not None or chunk_chars is not None or os.getenv("PSKA_INGEST_CHUNK_SIZE") or os.getenv("PSKA_INGEST_CHUNK_CHARS"):
            chunking_config["chunk_size"] = self._resolve_chunk_size(chunk_size=chunk_size, chunk_chars=chunk_chars)
        if chunk_overlap is not None or os.getenv("PSKA_INGEST_CHUNK_OVERLAP"):
            chunking_config["chunk_overlap"] = self._resolve_chunk_overlap(chunk_overlap, chunk_size=int(chunking_config.get("chunk_size") or 1200))
        self.chunking_config = normalize_chunking_config(chunking_config)
        self.processing_config["chunking"] = self.chunking_config
        self.chunk_size = int(self.chunking_config["chunk_size"])
        self.chunk_overlap = int(self.chunking_config["chunk_overlap"])
        self.embedding_provider = embedding_provider

    def ingest_channel_payload(self, payload: ChannelIngestPayload | dict) -> SourceItem:
        if isinstance(payload, dict):
            payload = ChannelIngestPayload.from_mapping(payload)
        payload = self._sanitize_payload(payload)
        text = str(payload.content.get("text") or payload.content.get("raw_text") or "")
        title = payload.title or self._default_title(payload.record_type, text)
        content_hash = self._hash_payload(payload, text)
        source_item_id = f"src_{uuid5(NAMESPACE_URL, f'{payload.tenant_id}:{content_hash}').hex}"
        item = SourceItem(
            source_item_id=source_item_id,
            source_channel=payload.source_channel,
            record_type=payload.record_type,
            source_id=payload.source_id,
            owner_user_id=payload.owner_user_id,
            space_id=payload.space_id,
            visibility=payload.visibility,
            visible_team_ids=payload.visible_team_ids,
            title=title,
            url=payload.url,
            content_text=text,
            content_hash=content_hash,
            tenant_id=payload.tenant_id,
            metadata={
                "schema_version": payload.schema_version,
                "author": payload.author,
                "created_at": payload.created_at,
                "captured_at": payload.captured_at,
                "content": payload.content,
                "media": payload.media,
                "raw_paths": payload.raw_paths,
                "extra": payload.extra,
            },
        )
        stored = self.store.upsert_source_item(item)
        if stored.source_item_id != source_item_id:
            return stored
        document = Document(
            document_id=f"doc_{source_item_id[4:]}",
            source_item_id=stored.source_item_id,
            owner_user_id=stored.owner_user_id,
            space_id=stored.space_id,
            visibility=stored.visibility,
            visible_team_ids=stored.visible_team_ids,
            title=stored.title,
            body=stored.content_text,
            metadata={"url": stored.url},
            tenant_id=stored.tenant_id,
        )
        self.store.add_document(document)
        chunk_spans = self._chunk_spans(stored.content_text)
        embedding_texts = [span.embedding_text() for span in chunk_spans]
        chunk_embeddings = self.embedding_provider.embed_texts(embedding_texts) if self.embedding_provider else [None] * len(chunk_spans)
        chunks: list[Chunk] = []
        for ordinal, (span, embedding) in enumerate(zip(chunk_spans, chunk_embeddings)):
            chunk = Chunk(
                chunk_id=f"chk_{source_item_id[4:]}_{ordinal}",
                document_id=document.document_id,
                source_item_id=stored.source_item_id,
                owner_user_id=stored.owner_user_id,
                space_id=stored.space_id,
                visibility=stored.visibility,
                visible_team_ids=stored.visible_team_ids,
                text=span.text,
                ordinal=ordinal,
                embedding=embedding,
                metadata={
                    "embedding_provider": self.embedding_provider.provider_name if self.embedding_provider else None,
                    "embedding_model": self.embedding_provider.model_name if self.embedding_provider else None,
                    "chunk_size": self.chunk_size,
                    "chunk_overlap": self.chunk_overlap,
                    "chunk_strategy": span.strategy,
                    "start": span.start,
                    "end": span.end,
                    "context_header": span.context_header,
                },
                tenant_id=stored.tenant_id,
            )
            self.store.add_chunk(chunk)
            chunks.append(chunk)
        OfflineIndexService(self.store, embedding_provider=self.embedding_provider).mark_source_dirty(
            stored,
            chunks,
            reason="source_ingested",
        )
        return stored

    def _hash_payload(self, payload: ChannelIngestPayload, text: str) -> str:
        connector_hash = payload.content.get("content_hash")
        if connector_hash:
            return str(connector_hash)
        basis = "\n".join([
            payload.source_channel,
            payload.record_type,
            payload.source_id,
            payload.url or "",
            text,
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def _chunk_text(self, text: str) -> list[str]:
        return [span.text for span in self._chunk_spans(text)]

    def _chunk_spans(self, text: str) -> list[ChunkSpan]:
        return chunk_text(text, self.chunking_config)

    def _default_title(self, record_type: str, text: str) -> str:
        first = re.sub(r"\s+", " ", text).strip()[:80]
        return first or record_type

    def _sanitize_payload(self, payload: ChannelIngestPayload) -> ChannelIngestPayload:
        return ChannelIngestPayload(
            schema_version=self._postgres_safe_text(payload.schema_version),
            source_channel=self._postgres_safe_text(payload.source_channel),
            record_type=self._postgres_safe_text(payload.record_type),
            source_id=self._postgres_safe_text(payload.source_id),
            owner_user_id=self._postgres_safe_text(payload.owner_user_id),
            space_id=self._postgres_safe_text(payload.space_id),
            visibility=payload.visibility,
            visible_team_ids=[self._postgres_safe_text(team_id) for team_id in payload.visible_team_ids],
            url=self._postgres_safe_text(payload.url) if payload.url is not None else None,
            title=self._postgres_safe_text(payload.title) if payload.title is not None else None,
            author=self._postgres_safe_json(payload.author),
            content=self._postgres_safe_json(payload.content),
            created_at=self._postgres_safe_text(payload.created_at) if payload.created_at is not None else None,
            captured_at=self._postgres_safe_text(payload.captured_at) if payload.captured_at is not None else None,
            media=self._postgres_safe_json(payload.media),
            raw_paths=self._postgres_safe_json(payload.raw_paths),
            extra=self._postgres_safe_json(payload.extra),
            tenant_id=self._postgres_safe_text(payload.tenant_id or DEFAULT_TENANT_ID),
        )

    def _postgres_safe_json(self, value: Any) -> Any:
        return postgres_safe_json(value)

    def _postgres_safe_text(self, value: str) -> str:
        return postgres_safe_text(value)

    def _resolve_chunk_size(self, *, chunk_size: int | None, chunk_chars: int | None) -> int:
        value = chunk_size if chunk_size is not None else chunk_chars
        if value is None:
            value = int(os.getenv("PSKA_INGEST_CHUNK_SIZE") or os.getenv("PSKA_INGEST_CHUNK_CHARS") or 1200)
        if value <= 0:
            raise ValueError("chunk_size must be greater than 0")
        return value

    def _resolve_chunk_overlap(self, chunk_overlap: int | None, *, chunk_size: int | None = None) -> int:
        value = chunk_overlap
        if value is None:
            value = int(os.getenv("PSKA_INGEST_CHUNK_OVERLAP") or 0)
        if value < 0:
            raise ValueError("chunk_overlap must be greater than or equal to 0")
        effective_chunk_size = chunk_size if chunk_size is not None else self.chunk_size
        if value >= effective_chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return value
