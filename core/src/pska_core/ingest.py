from __future__ import annotations

import hashlib
import os
import re
from uuid import uuid5, NAMESPACE_URL

from pska_core.models import ChannelIngestPayload, Chunk, Document, SourceItem
from pska_core.offline_index import OfflineIndexService
from pska_core.store import KnowledgeStore
from pska_core.embeddings import EmbeddingProvider


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
    ) -> None:
        self.store = store
        self.chunk_size = self._resolve_chunk_size(chunk_size=chunk_size, chunk_chars=chunk_chars)
        self.chunk_overlap = self._resolve_chunk_overlap(chunk_overlap)
        self.embedding_provider = embedding_provider

    def ingest_channel_payload(self, payload: ChannelIngestPayload | dict) -> SourceItem:
        if isinstance(payload, dict):
            payload = ChannelIngestPayload.from_mapping(payload)
        text = str(payload.content.get("text") or payload.content.get("raw_text") or "")
        title = payload.title or self._default_title(payload.record_type, text)
        content_hash = self._hash_payload(payload, text)
        source_item_id = f"src_{uuid5(NAMESPACE_URL, content_hash).hex}"
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
        )
        self.store.add_document(document)
        chunk_texts = self._chunk_text(stored.content_text)
        chunk_embeddings = self.embedding_provider.embed_texts(chunk_texts) if self.embedding_provider else [None] * len(chunk_texts)
        chunks: list[Chunk] = []
        for ordinal, (chunk_text, embedding) in enumerate(zip(chunk_texts, chunk_embeddings)):
            chunk = Chunk(
                chunk_id=f"chk_{source_item_id[4:]}_{ordinal}",
                document_id=document.document_id,
                source_item_id=stored.source_item_id,
                owner_user_id=stored.owner_user_id,
                space_id=stored.space_id,
                visibility=stored.visibility,
                visible_team_ids=stored.visible_team_ids,
                text=chunk_text,
                ordinal=ordinal,
                embedding=embedding,
                metadata={
                    "embedding_provider": self.embedding_provider.provider_name if self.embedding_provider else None,
                    "embedding_model": self.embedding_provider.model_name if self.embedding_provider else None,
                    "chunk_size": self.chunk_size,
                    "chunk_overlap": self.chunk_overlap,
                },
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
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return [""]
        step = self.chunk_size - self.chunk_overlap
        return [clean[index : index + self.chunk_size] for index in range(0, len(clean), step)]

    def _default_title(self, record_type: str, text: str) -> str:
        first = re.sub(r"\s+", " ", text).strip()[:80]
        return first or record_type

    def _resolve_chunk_size(self, *, chunk_size: int | None, chunk_chars: int | None) -> int:
        value = chunk_size if chunk_size is not None else chunk_chars
        if value is None:
            value = int(os.getenv("PSKA_INGEST_CHUNK_SIZE") or os.getenv("PSKA_INGEST_CHUNK_CHARS") or 1200)
        if value <= 0:
            raise ValueError("chunk_size must be greater than 0")
        return value

    def _resolve_chunk_overlap(self, chunk_overlap: int | None) -> int:
        value = chunk_overlap
        if value is None:
            value = int(os.getenv("PSKA_INGEST_CHUNK_OVERLAP") or 0)
        if value < 0:
            raise ValueError("chunk_overlap must be greater than or equal to 0")
        if value >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return value
