from __future__ import annotations

import hashlib
from typing import Any

from pska_core.embeddings import EmbeddingProvider, EmbeddingService
from pska_core.models import Chunk, SourceItem
from pska_core.store import KnowledgeStore


OFFLINE_INDEX_VERSION = "hipporag_offline.v1"


class OfflineIndexService:
    """Tracks dirty/indexed state for the HippoRAG-style offline stage."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        index_version: str = OFFLINE_INDEX_VERSION,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider
        self.index_version = index_version

    def mark_source_dirty(
        self,
        source: SourceItem,
        chunks: list[Chunk],
        *,
        reason: str,
        mtime: str | None = None,
    ) -> None:
        visibility_version = self.visibility_version(
            source.owner_user_id,
            source.visibility.value,
            source.visible_team_ids,
        )
        provider_name = self.embedding_provider.provider_name if self.embedding_provider else None
        model_name = self.embedding_provider.model_name if self.embedding_provider else None
        self.store.mark_offline_index_dirty(
            object_type="source_item",
            object_id=source.source_item_id,
            owner_user_id=source.owner_user_id,
            source_item_id=source.source_item_id,
            content_hash=source.content_hash,
            visibility_version=visibility_version,
            dirty_reason=reason,
            embedding_provider=provider_name,
            embedding_model=model_name,
            index_version=self.index_version,
            tenant_id=source.tenant_id,
        )
        for chunk in chunks:
            self.store.mark_offline_index_dirty(
                object_type="chunk",
                object_id=chunk.chunk_id,
                owner_user_id=chunk.owner_user_id,
                source_item_id=chunk.source_item_id,
                content_hash=self.chunk_content_hash(chunk),
                visibility_version=self.visibility_version(
                    chunk.owner_user_id,
                    chunk.visibility.value,
                    chunk.visible_team_ids,
                ),
                dirty_reason=reason,
                embedding_provider=provider_name or chunk.metadata.get("embedding_provider"),
                embedding_model=model_name or chunk.metadata.get("embedding_model"),
                index_version=self.index_version,
                tenant_id=chunk.tenant_id,
            )

    def process_dirty_embeddings(self, *, tenant_id: str | None = None, limit: int | None = None) -> dict[str, Any]:
        dirty_chunk_states = self.store.list_offline_index_states(tenant_id=tenant_id, status="dirty", object_type="chunk", limit=limit)
        dirty_chunk_ids = {state.object_id for state in dirty_chunk_states}
        if not dirty_chunk_ids:
            return self._empty_report("no_dirty_chunks", tenant_id=tenant_id)
        if self.embedding_provider is None:
            return {
                **self._empty_report("embedding_provider_disabled", tenant_id=tenant_id),
                "dirty_chunk_ids": sorted(dirty_chunk_ids),
            }
        source_ids = {state.source_item_id for state in dirty_chunk_states if state.source_item_id}
        chunks = [
            chunk
            for chunk in self.store.list_chunks_for_sources(set(source_ids))
            if chunk.chunk_id in dirty_chunk_ids
        ]
        report = EmbeddingService(self.store, self.embedding_provider).embed_chunks(chunks)
        indexed_chunk_ids: list[str] = []
        for chunk in chunks:
            if chunk.text.strip() and chunk.chunk_id not in _failed_chunk_ids(report.errors):
                self.store.mark_offline_indexed(
                    object_type="chunk",
                    object_id=chunk.chunk_id,
                    embedding_provider=self.embedding_provider.provider_name,
                    embedding_model=self.embedding_provider.model_name,
                    index_version=self.index_version,
                )
                indexed_chunk_ids.append(chunk.chunk_id)
        self._mark_sources_indexed_when_clean({chunk.source_item_id for chunk in chunks})
        return {
            "ok": report.failed == 0,
            "reason": "processed_dirty_chunks",
            "provider": report.provider,
            "model": report.model,
            "dirty_chunks": len(dirty_chunk_ids),
            "embedded": report.embedded,
            "skipped": report.skipped,
            "failed": report.failed,
            "indexed_chunk_ids": indexed_chunk_ids,
            "errors": report.errors,
            "offline_index": self.store.offline_index_status(tenant_id=tenant_id),
        }

    def tombstone_source(self, source_item_id: str, *, tenant_id: str | None = None, reason: str = "source_tombstoned") -> dict[str, Any]:
        states = self.store.tombstone_offline_index_for_source(source_item_id, reason=reason)
        effective_tenant_id = tenant_id or (states[0].tenant_id if states else None)
        return {
            "source_item_id": source_item_id,
            "reason": reason,
            "tombstoned": len(states),
            "offline_index": self.store.offline_index_status(tenant_id=effective_tenant_id),
        }

    def freshness(self, *, tenant_id: str | None = None, owner_user_id: str | None = None) -> dict[str, Any]:
        return {
            **self.store.offline_index_status(tenant_id=tenant_id, owner_user_id=owner_user_id),
            "fallback": "request_scoped_rebuild",
        }

    def _mark_sources_indexed_when_clean(self, source_item_ids: set[str]) -> None:
        for source_item_id in source_item_ids:
            dirty = self.store.list_offline_index_states(status="dirty", source_item_id=source_item_id, object_type="chunk")
            if dirty:
                continue
            source_states = self.store.list_offline_index_states(source_item_id=source_item_id, object_type="source_item", limit=1)
            if source_states:
                self.store.mark_offline_indexed(
                    object_type="source_item",
                    object_id=source_states[0].object_id,
                    embedding_provider=self.embedding_provider.provider_name if self.embedding_provider else None,
                    embedding_model=self.embedding_provider.model_name if self.embedding_provider else None,
                    index_version=self.index_version,
                )

    def _empty_report(self, reason: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        return {
            "ok": reason == "no_dirty_chunks",
            "reason": reason,
            "dirty_chunks": 0,
            "embedded": 0,
            "skipped": 0,
            "failed": 0,
            "offline_index": self.store.offline_index_status(tenant_id=tenant_id),
        }

    @staticmethod
    def chunk_content_hash(chunk: Chunk) -> str:
        return hashlib.sha256(f"{chunk.source_item_id}:{chunk.ordinal}:{chunk.text}".encode("utf-8")).hexdigest()

    @staticmethod
    def visibility_version(owner_user_id: str, visibility: str, visible_team_ids: list[str]) -> str:
        return "|".join([owner_user_id, visibility, ",".join(sorted(visible_team_ids))])


def _failed_chunk_ids(errors: list[dict[str, str]]) -> set[str]:
    ids: set[str] = set()
    for error in errors:
        raw = error.get("chunk_ids") or ""
        ids.update(chunk_id for chunk_id in raw.split(",") if chunk_id)
    return ids
