from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from pska_core.keyfile import read_api_key_file


DEFAULT_DATABASE_URL = "postgresql:///pska"
DEFAULT_WORKSPACE_ROOT = Path("~/PSKA_workspaces/default")


def expand_path(value: str | Path) -> Path:
    if isinstance(value, Path):
        return value.expanduser()
    return Path(os.path.expandvars(value)).expanduser()


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    url: str = DEFAULT_DATABASE_URL

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DatabaseConfig":
        data = data or {}
        return cls(url=str(data.get("url") or data.get("database_url") or DEFAULT_DATABASE_URL))


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    service_token: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, api_key_file: Path | None = None) -> "ServiceConfig":
        data = data or {}
        token = data.get("service_token") or data.get("token") or _service_token_from_key_file(api_key_file)
        return cls(
            host=str(data.get("host") or "127.0.0.1"),
            port=int(data.get("port") or 8765),
            service_token=str(token).strip() if token else None,
        )


@dataclass(frozen=True, slots=True)
class LLMConfig:
    api_key_file: Path | None = None
    model: str | None = None
    base_url: str | None = None
    timeout_seconds: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LLMConfig":
        data = data or {}
        api_key_file = data.get("api_key_file") or data.get("key_file")
        return cls(
            api_key_file=expand_path(api_key_file) if api_key_file else None,
            model=str(data["model"]) if data.get("model") else None,
            base_url=str(data.get("base_url") or data.get("api_base")) if data.get("base_url") or data.get("api_base") else None,
            timeout_seconds=int(data["timeout_seconds"]) if data.get("timeout_seconds") else None,
        )


@dataclass(frozen=True, slots=True)
class FastreactConfig:
    url: str = "http://127.0.0.1:8000"
    service_token: str | None = None
    timeout_seconds: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, api_key_file: Path | None = None) -> "FastreactConfig":
        data = data or {}
        token = data.get("service_token") or data.get("token") or _fastreact_token_from_key_file(api_key_file)
        return cls(
            url=str(data.get("url") or "http://127.0.0.1:8000").rstrip("/"),
            service_token=str(token).strip() if token else None,
            timeout_seconds=float(data["timeout_seconds"]) if data.get("timeout_seconds") else None,
        )


@dataclass(frozen=True, slots=True)
class AgenticServiceConfigFile:
    provider: str = "fastreact"
    url: str = "http://127.0.0.1:8000"
    service_token: str | None = None
    timeout_seconds: float | None = None

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        fallback: FastreactConfig,
        api_key_file: Path | None = None,
    ) -> "AgenticServiceConfigFile":
        data = data or {}
        token = data.get("service_token") or data.get("token") or fallback.service_token or _fastreact_token_from_key_file(api_key_file)
        return cls(
            provider=str(data.get("provider") or "fastreact"),
            url=str(data.get("url") or fallback.url or "http://127.0.0.1:8000").rstrip("/"),
            service_token=str(token).strip() if token else None,
            timeout_seconds=float(data["timeout_seconds"]) if data.get("timeout_seconds") else fallback.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class EmbeddingConfigFile:
    provider: str = "disabled"
    model: str | None = None
    dimensions: int | None = None
    batch_size: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EmbeddingConfigFile":
        data = data or {}
        return cls(
            provider=str(data.get("provider") or "disabled"),
            model=str(data["model"]) if data.get("model") else None,
            dimensions=int(data["dimensions"]) if data.get("dimensions") else None,
            batch_size=int(data["batch_size"]) if data.get("batch_size") else None,
        )


@dataclass(frozen=True, slots=True)
class IngestConfig:
    chunk_size: int = 1200
    chunk_overlap: int = 0

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise ValueError("ingest.chunk_size must be greater than 0")
        if self.chunk_overlap < 0:
            raise ValueError("ingest.chunk_overlap must be greater than or equal to 0")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("ingest.chunk_overlap must be smaller than ingest.chunk_size")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "IngestConfig":
        data = data or {}
        return cls(
            chunk_size=int(data.get("chunk_size") or data.get("chunk_chars") or 1200),
            chunk_overlap=int(data.get("chunk_overlap") or 0),
        )


@dataclass(frozen=True, slots=True)
class FilesConfig:
    roots: tuple[Path, ...] = ()
    ignore: tuple[str, ...] = ()
    max_bytes: int = 1_000_000
    owner_user_id: str = "user_primary"
    space_id: str = "private_primary"
    visibility: str = "private"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FilesConfig":
        data = data or {}
        return cls(
            roots=tuple(expand_path(root) for root in data.get("roots") or []),
            ignore=tuple(str(item) for item in data.get("ignore") or []),
            max_bytes=int(data.get("max_bytes") or 1_000_000),
            owner_user_id=str(data.get("owner_user_id") or "user_primary"),
            space_id=str(data.get("space_id") or "private_primary"),
            visibility=str(data.get("visibility") or "private"),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path = DEFAULT_WORKSPACE_ROOT

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WorkspaceConfig":
        data = data or {}
        return cls(root=expand_path(data.get("root") or DEFAULT_WORKSPACE_ROOT))

    @property
    def imports_dir(self) -> Path:
        return self.root / "imports"

    @property
    def twitter_archive_dir(self) -> Path:
        return self.root / "twitter_archive"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def cold_start_dir(self) -> Path:
        return self.root / "cold_start"


@dataclass(frozen=True, slots=True)
class PSKAConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    fastreact: FastreactConfig = field(default_factory=FastreactConfig)
    agentic_service: AgenticServiceConfigFile = field(default_factory=AgenticServiceConfigFile)
    embedding: EmbeddingConfigFile = field(default_factory=EmbeddingConfigFile)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "PSKAConfig":
        path = _find_config_path(config_path)
        if path is None:
            return cls.from_env(cls())
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("PSKA config must be a JSON object")
        config = cls.from_dict(data)
        return cls.from_env(config)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PSKAConfig":
        llm = LLMConfig.from_dict(data.get("llm"))
        fastreact = FastreactConfig.from_dict(data.get("fastreact"), api_key_file=llm.api_key_file)
        return cls(
            database=DatabaseConfig.from_dict(data.get("database")),
            service=ServiceConfig.from_dict(data.get("service"), api_key_file=llm.api_key_file),
            llm=llm,
            fastreact=fastreact,
            agentic_service=AgenticServiceConfigFile.from_dict(data.get("agentic_service"), fallback=fastreact, api_key_file=llm.api_key_file),
            embedding=EmbeddingConfigFile.from_dict(data.get("embedding")),
            ingest=IngestConfig.from_dict(data.get("ingest")),
            files=FilesConfig.from_dict(data.get("files")),
            workspace=WorkspaceConfig.from_dict(data.get("workspace")),
        )

    @classmethod
    def from_env(cls, base: "PSKAConfig | None" = None) -> "PSKAConfig":
        base = base or cls()
        default_agentic = AgenticServiceConfigFile()
        agentic_url = os.getenv("PSKA_AGENTIC_SERVICE_URL")
        if not agentic_url and base.agentic_service.url == default_agentic.url:
            agentic_url = os.getenv("PSKA_FASTREACT_URL")
        agentic_token = os.getenv("PSKA_AGENTIC_SERVICE_TOKEN")
        if not agentic_token and base.agentic_service.service_token == default_agentic.service_token:
            agentic_token = os.getenv("PSKA_FASTREACT_SERVICE_TOKEN")
        agentic_timeout = os.getenv("PSKA_AGENTIC_SERVICE_TIMEOUT_SECONDS")
        if not agentic_timeout and base.agentic_service.timeout_seconds == default_agentic.timeout_seconds:
            agentic_timeout = os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS")
        return cls(
            database=DatabaseConfig(url=os.getenv("PSKA_DATABASE_URL", base.database.url)),
            service=ServiceConfig(
                host=os.getenv("PSKA_SERVICE_HOST", base.service.host),
                port=int(os.getenv("PSKA_SERVICE_PORT", str(base.service.port))),
                service_token=os.getenv("PSKA_SERVICE_TOKEN") or base.service.service_token,
            ),
            llm=LLMConfig(
                api_key_file=expand_path(os.getenv("PSKA_LLM_API_KEY_FILE")) if os.getenv("PSKA_LLM_API_KEY_FILE") else base.llm.api_key_file,
                model=os.getenv("PSKA_LLM_MODEL") or base.llm.model,
                base_url=os.getenv("PSKA_LLM_BASE_URL") or base.llm.base_url,
                timeout_seconds=int(os.getenv("PSKA_LLM_TIMEOUT_SECONDS")) if os.getenv("PSKA_LLM_TIMEOUT_SECONDS") else base.llm.timeout_seconds,
            ),
            fastreact=FastreactConfig(
                url=os.getenv("PSKA_FASTREACT_URL", base.fastreact.url).rstrip("/"),
                service_token=os.getenv("PSKA_FASTREACT_SERVICE_TOKEN") or base.fastreact.service_token,
                timeout_seconds=float(os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS")) if os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS") else base.fastreact.timeout_seconds,
            ),
            agentic_service=AgenticServiceConfigFile(
                provider=os.getenv("PSKA_AGENTIC_SERVICE_PROVIDER") or os.getenv("PSKA_AGENTIC_PROVIDER") or base.agentic_service.provider,
                url=(agentic_url or base.agentic_service.url).rstrip("/"),
                service_token=agentic_token or base.agentic_service.service_token,
                timeout_seconds=float(agentic_timeout)
                if agentic_timeout
                else base.agentic_service.timeout_seconds,
            ),
            embedding=EmbeddingConfigFile(
                provider=os.getenv("PSKA_EMBEDDING_PROVIDER", base.embedding.provider),
                model=os.getenv("PSKA_EMBEDDING_MODEL") or base.embedding.model,
                dimensions=int(os.getenv("PSKA_EMBEDDING_DIMENSIONS")) if os.getenv("PSKA_EMBEDDING_DIMENSIONS") else base.embedding.dimensions,
                batch_size=int(os.getenv("PSKA_EMBEDDING_BATCH_SIZE")) if os.getenv("PSKA_EMBEDDING_BATCH_SIZE") else base.embedding.batch_size,
            ),
            ingest=IngestConfig(
                chunk_size=int(os.getenv("PSKA_INGEST_CHUNK_SIZE") or os.getenv("PSKA_INGEST_CHUNK_CHARS") or base.ingest.chunk_size),
                chunk_overlap=int(os.getenv("PSKA_INGEST_CHUNK_OVERLAP")) if os.getenv("PSKA_INGEST_CHUNK_OVERLAP") else base.ingest.chunk_overlap,
            ),
            files=FilesConfig(
                roots=tuple(expand_path(root) for root in os.getenv("PSKA_FILES_ROOTS", "").split(os.pathsep) if root)
                or base.files.roots,
                ignore=base.files.ignore,
                max_bytes=int(os.getenv("PSKA_FILES_MAX_BYTES", str(base.files.max_bytes))),
                owner_user_id=os.getenv("PSKA_FILES_OWNER_USER_ID", base.files.owner_user_id),
                space_id=os.getenv("PSKA_FILES_SPACE_ID", base.files.space_id),
                visibility=os.getenv("PSKA_FILES_VISIBILITY", base.files.visibility),
            ),
            workspace=WorkspaceConfig(
                root=expand_path(os.getenv("PSKA_WORKSPACE_ROOT")) if os.getenv("PSKA_WORKSPACE_ROOT") else base.workspace.root,
            ),
        )

    def apply_to_env(self) -> None:
        _setdefault("PSKA_DATABASE_URL", self.database.url)
        _setdefault("PSKA_SERVICE_HOST", self.service.host)
        _setdefault("PSKA_SERVICE_PORT", str(self.service.port))
        if self.service.service_token:
            _setdefault("PSKA_SERVICE_TOKEN", self.service.service_token)
        if self.llm.api_key_file:
            _setdefault("PSKA_LLM_API_KEY_FILE", str(self.llm.api_key_file))
        if self.llm.model:
            _setdefault("PSKA_LLM_MODEL", self.llm.model)
        if self.llm.base_url:
            _setdefault("PSKA_LLM_BASE_URL", self.llm.base_url.rstrip("/"))
        if self.llm.timeout_seconds:
            _setdefault("PSKA_LLM_TIMEOUT_SECONDS", str(self.llm.timeout_seconds))
        _setdefault("PSKA_FASTREACT_URL", self.fastreact.url.rstrip("/"))
        if self.fastreact.service_token:
            _setdefault("PSKA_FASTREACT_SERVICE_TOKEN", self.fastreact.service_token)
        if self.fastreact.timeout_seconds:
            _setdefault("PSKA_FASTREACT_TIMEOUT_SECONDS", str(self.fastreact.timeout_seconds))
        _setdefault("PSKA_AGENTIC_SERVICE_PROVIDER", self.agentic_service.provider)
        _setdefault("PSKA_AGENTIC_SERVICE_URL", self.agentic_service.url.rstrip("/"))
        if self.agentic_service.service_token:
            _setdefault("PSKA_AGENTIC_SERVICE_TOKEN", self.agentic_service.service_token)
        if self.agentic_service.timeout_seconds:
            _setdefault("PSKA_AGENTIC_SERVICE_TIMEOUT_SECONDS", str(self.agentic_service.timeout_seconds))
        _setdefault("PSKA_EMBEDDING_PROVIDER", self.embedding.provider)
        if self.embedding.model:
            _setdefault("PSKA_EMBEDDING_MODEL", self.embedding.model)
        if self.embedding.dimensions:
            _setdefault("PSKA_EMBEDDING_DIMENSIONS", str(self.embedding.dimensions))
        if self.embedding.batch_size:
            _setdefault("PSKA_EMBEDDING_BATCH_SIZE", str(self.embedding.batch_size))
        _setdefault("PSKA_INGEST_CHUNK_SIZE", str(self.ingest.chunk_size))
        _setdefault("PSKA_INGEST_CHUNK_OVERLAP", str(self.ingest.chunk_overlap))
        _setdefault("PSKA_WORKSPACE_ROOT", str(self.workspace.root))


def _find_config_path(config_path: str | Path | None) -> Path | None:
    if config_path:
        path = expand_path(config_path)
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    for path in [
        Path.home() / ".pska" / "config.json",
        Path.cwd() / ".pska" / "config.json",
        Path.cwd() / "config.pska.json",
    ]:
        if path.exists():
            return path
    return None


def _setdefault(key: str, value: str) -> None:
    if value and not os.getenv(key):
        os.environ[key] = value


def _service_token_from_key_file(path: Path | None) -> str | None:
    if path is None:
        return None
    return read_api_key_file(path).service_token or None


def _fastreact_token_from_key_file(path: Path | None) -> str | None:
    if path is None:
        return None
    key_file = read_api_key_file(path)
    return key_file.service_token or None
