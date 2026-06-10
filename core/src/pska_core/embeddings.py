from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol

from pska_core.models import Chunk


BGE_M3_MODEL = "BAAI/bge-m3"
BGE_M3_DIMENSIONS = 1024


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str
    dimensions: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str = "disabled"
    model: str = BGE_M3_MODEL
    dimensions: int = BGE_M3_DIMENSIONS
    batch_size: int = 16

    @classmethod
    def from_env(cls, *, default_provider: str = "disabled") -> "EmbeddingConfig":
        provider = os.getenv("PSKA_EMBEDDING_PROVIDER", default_provider).strip().lower()
        model = os.getenv("PSKA_EMBEDDING_MODEL", BGE_M3_MODEL).strip() or BGE_M3_MODEL
        dimensions = int(os.getenv("PSKA_EMBEDDING_DIMENSIONS", str(BGE_M3_DIMENSIONS)))
        batch_size = int(os.getenv("PSKA_EMBEDDING_BATCH_SIZE", "16"))
        return cls(provider=provider, model=model, dimensions=dimensions, batch_size=batch_size)


class BgeM3EmbeddingProvider:
    """Local BGE-M3 embedding provider backed by FlagEmbedding.

    This provider does not silently degrade. If FlagEmbedding or the configured
    model cannot be loaded, callers get a RuntimeError and the embedding job
    fails visibly.
    """

    provider_name = "bge-m3"

    def __init__(self, model_name: str = BGE_M3_MODEL, *, dimensions: int = BGE_M3_DIMENSIONS) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        try:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore
        except Exception as exc:  # pragma: no cover - exercised only without optional dependency
            raise RuntimeError(
                "BGE-M3 embedding is enabled but FlagEmbedding is not installed. "
                "Install the embedding extras/dependency before running this job."
            ) from exc
        try:
            self._model = BGEM3FlagModel(model_name, use_fp16=os.getenv("PSKA_EMBEDDING_USE_FP16", "true").lower() == "true")
        except Exception as exc:  # pragma: no cover - depends on local model/runtime
            raise RuntimeError(f"Failed to load embedding model {model_name!r}") from exc

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        encoded = self._model.encode(texts, batch_size=int(os.getenv("PSKA_EMBEDDING_BATCH_SIZE", "16")))
        vectors = encoded.get("dense_vecs") if isinstance(encoded, dict) else encoded
        result = [self._coerce_vector(vector) for vector in vectors]
        for vector in result:
            if len(vector) != self.dimensions:
                raise ValueError(
                    f"Embedding dimension mismatch for {self.model_name}: "
                    f"expected {self.dimensions}, got {len(vector)}"
                )
        return result

    def _coerce_vector(self, vector) -> list[float]:
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        return [float(value) for value in vector]


class DisabledEmbeddingProvider:
    provider_name = "disabled"
    model_name = "disabled"
    dimensions = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Embedding provider is disabled")


def build_embedding_provider(config: EmbeddingConfig | None = None) -> EmbeddingProvider | None:
    config = config or EmbeddingConfig.from_env()
    if config.provider in {"", "disabled", "none", "off"}:
        return None
    if config.provider in {"bge-m3", "bge_m3", "bge"}:
        return BgeM3EmbeddingProvider(config.model, dimensions=config.dimensions)
    raise ValueError(f"Unsupported embedding provider: {config.provider}")


@dataclass(slots=True)
class EmbeddingBackfillReport:
    provider: str
    model: str
    dimensions: int
    embedded: int
    skipped: int
    failed: int
    errors: list[dict[str, str]]


class EmbeddingService:
    def __init__(self, store, provider: EmbeddingProvider, *, batch_size: int = 16) -> None:
        self.store = store
        self.provider = provider
        self.batch_size = batch_size

    def embed_chunks(self, chunks: list[Chunk]) -> EmbeddingBackfillReport:
        report = EmbeddingBackfillReport(
            provider=self.provider.provider_name,
            model=self.provider.model_name,
            dimensions=self.provider.dimensions,
            embedded=0,
            skipped=0,
            failed=0,
            errors=[],
        )
        pending = [chunk for chunk in chunks if chunk.text.strip()]
        report.skipped = len(chunks) - len(pending)
        for offset in range(0, len(pending), self.batch_size):
            batch = pending[offset : offset + self.batch_size]
            try:
                vectors = self.provider.embed_texts([chunk.text for chunk in batch])
                if len(vectors) != len(batch):
                    raise ValueError(f"provider returned {len(vectors)} vectors for {len(batch)} chunks")
                for chunk, vector in zip(batch, vectors):
                    self.store.update_chunk_embedding(
                        chunk.chunk_id,
                        vector,
                        provider=self.provider.provider_name,
                        model=self.provider.model_name,
                    )
                    chunk.embedding = vector
                    report.embedded += 1
            except Exception as exc:
                report.failed += len(batch)
                report.errors.append({"chunk_ids": ",".join(chunk.chunk_id for chunk in batch), "error": str(exc)})
        return report

    def backfill_missing(self, *, limit: int | None = None) -> EmbeddingBackfillReport:
        chunks = self.store.list_chunks_missing_embedding(
            provider=self.provider.provider_name,
            model=self.provider.model_name,
            limit=limit,
        )
        return self.embed_chunks(chunks)
