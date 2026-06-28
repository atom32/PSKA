from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pska_core.keyfile import read_api_key_file
from pska_core.models import Chunk


BGE_M3_MODEL = "BAAI/bge-m3"
BGE_M3_DIMENSIONS = 1024
API_EMBEDDING_MODEL = "text-embedding-3-small"
API_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
API_EMBEDDING_PROVIDERS = {"api", "openai", "openai-compatible", "openai_compatible", "http"}


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
    api_key: str | None = None
    api_key_file: Path | None = None
    base_url: str | None = None
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, *, default_provider: str = "disabled") -> "EmbeddingConfig":
        provider = os.getenv("PSKA_EMBEDDING_PROVIDER", default_provider).strip().lower()
        default_model = API_EMBEDDING_MODEL if is_api_embedding_provider(provider) else BGE_M3_MODEL
        model = os.getenv("PSKA_EMBEDDING_MODEL", default_model).strip() or default_model
        dimensions = int(os.getenv("PSKA_EMBEDDING_DIMENSIONS", str(BGE_M3_DIMENSIONS)))
        batch_size = int(os.getenv("PSKA_EMBEDDING_BATCH_SIZE", "16"))
        api_key_file = os.getenv("PSKA_EMBEDDING_API_KEY_FILE")
        return cls(
            provider=provider,
            model=model,
            dimensions=dimensions,
            batch_size=batch_size,
            api_key=os.getenv("PSKA_EMBEDDING_API_KEY") or None,
            api_key_file=Path(api_key_file).expanduser() if api_key_file else None,
            base_url=os.getenv("PSKA_EMBEDDING_BASE_URL") or os.getenv("PSKA_EMBEDDING_API_BASE") or None,
            timeout_seconds=float(os.getenv("PSKA_EMBEDDING_TIMEOUT_SECONDS", "60")),
        )


def is_api_embedding_provider(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in API_EMBEDDING_PROVIDERS


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


class APIEmbeddingProvider:
    """OpenAI-compatible embedding provider backed by a remote HTTP API."""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str = API_EMBEDDING_MODEL,
        base_url: str = API_EMBEDDING_BASE_URL,
        dimensions: int = BGE_M3_DIMENSIONS,
        provider_name: str = "api",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, object] = {
            "model": self.model_name,
            "input": texts,
        }
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Embedding API HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - surface provider failures explicitly.
            raise RuntimeError(f"Embedding API request failed: {type(exc).__name__}: {exc}") from exc

        vectors = self._vectors_from_response(body, expected_count=len(texts))
        for vector in vectors:
            if self.dimensions and len(vector) != self.dimensions:
                raise ValueError(
                    f"Embedding dimension mismatch for {self.model_name}: "
                    f"expected {self.dimensions}, got {len(vector)}"
                )
        return vectors

    def _vectors_from_response(self, body: object, *, expected_count: int) -> list[list[float]]:
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise RuntimeError("Embedding API response must contain a data array")
        data = body["data"]
        if len(data) != expected_count:
            raise RuntimeError(f"Embedding API returned {len(data)} vectors for {expected_count} texts")
        if all(isinstance(item, dict) and "index" in item for item in data):
            data = sorted(data, key=lambda item: int(item.get("index") or 0))
        vectors: list[list[float]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise RuntimeError("Embedding API data item must contain an embedding array")
            vectors.append([float(value) for value in item["embedding"]])
        return vectors


class DisabledEmbeddingProvider:
    provider_name = "disabled"
    model_name = "disabled"
    dimensions = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Embedding provider is disabled")


def build_embedding_provider(config: EmbeddingConfig | None = None) -> EmbeddingProvider | None:
    config = config or EmbeddingConfig.from_env()
    provider = config.provider.strip().lower()
    if provider in {"", "disabled", "none", "off"}:
        return None
    if provider in {"bge-m3", "bge_m3", "bge"}:
        return BgeM3EmbeddingProvider(config.model, dimensions=config.dimensions)
    if is_api_embedding_provider(provider):
        return APIEmbeddingProvider(
            api_key=_embedding_api_key(config),
            model_name=config.model or API_EMBEDDING_MODEL,
            base_url=config.base_url or API_EMBEDDING_BASE_URL,
            dimensions=config.dimensions,
            provider_name=provider,
            timeout_seconds=float(config.timeout_seconds or 60.0),
        )
    raise ValueError(f"Unsupported embedding provider: {config.provider}")


def _embedding_api_key(config: EmbeddingConfig) -> str:
    api_key = (config.api_key or os.getenv("PSKA_EMBEDDING_API_KEY") or "").strip()
    if not api_key and config.api_key_file:
        api_key = read_api_key_file(config.api_key_file).api_key
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "API embedding is enabled but no API key is configured. "
            "Set embedding.api_key_file, PSKA_EMBEDDING_API_KEY, or OPENAI_API_KEY."
        )
    return api_key


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
