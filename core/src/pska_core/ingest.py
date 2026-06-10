from __future__ import annotations

import hashlib
import re
from uuid import uuid5, NAMESPACE_URL

from pska_core.models import ChannelIngestPayload, Chunk, Document, SourceItem
from pska_core.store import KnowledgeStore


class IngestService:
    """Converts channel payloads into source items, documents, and chunks."""

    def __init__(self, store: KnowledgeStore, *, chunk_chars: int = 1200) -> None:
        self.store = store
        self.chunk_chars = chunk_chars

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
        for ordinal, chunk_text in enumerate(self._chunk_text(stored.content_text)):
            self.store.add_chunk(
                Chunk(
                    chunk_id=f"chk_{source_item_id[4:]}_{ordinal}",
                    document_id=document.document_id,
                    source_item_id=stored.source_item_id,
                    owner_user_id=stored.owner_user_id,
                    space_id=stored.space_id,
                    visibility=stored.visibility,
                    visible_team_ids=stored.visible_team_ids,
                    text=chunk_text,
                    ordinal=ordinal,
                )
            )
        return stored

    def _hash_payload(self, payload: ChannelIngestPayload, text: str) -> str:
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
        return [clean[index : index + self.chunk_chars] for index in range(0, len(clean), self.chunk_chars)]

    def _default_title(self, record_type: str, text: str) -> str:
        first = re.sub(r"\s+", " ", text).strip()[:80]
        return first or record_type
