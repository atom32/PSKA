from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pska_core.keyfile import read_api_key_file
from pska_core.models import DEFAULT_TENANT_ID

if TYPE_CHECKING:
    from pska_core.agentic_service import AgenticServiceConfig
    from pska_core.embeddings import EmbeddingConfig
    from pska_core.fastreact_client import FastreactConfig as RuntimeFastreactConfig


DEFAULT_DATABASE_URL = "postgresql:///pska"
DEFAULT_WORKSPACE_ROOT = Path("~/PSKA_workspaces/default")
DEFAULT_JWT_TENANT_CLAIMS = ("tenant_id", "tenant_key", "tenant", "org_id")
DEFAULT_TRUSTED_HEADER_USER_ID = "X-PSKA-User-Id"
DEFAULT_TRUSTED_HEADER_TENANT_ID = "X-PSKA-Tenant-Id"
DEFAULT_TRUSTED_HEADER_REPRESENTED_USER_ID = "X-PSKA-Represented-User-Id"
DEFAULT_TRUSTED_HEADER_SUBJECT = "X-PSKA-Subject"
DEFAULT_TRUSTED_HEADER_DISPLAY_NAME = "X-PSKA-Display-Name"
DEFAULT_TRUSTED_HEADER_EMAIL = "X-PSKA-Email"
DEFAULT_TRUSTED_HEADER_GROUPS = "X-PSKA-Groups"
DEFAULT_TRUSTED_HEADER_ROLES = "X-PSKA-Roles"
DEFAULT_TRUSTED_HEADER_PROVIDER = "X-PSKA-Auth-Provider"


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
class AuthConfig:
    mode: str = "service_token"
    trusted_header_user_id: str = DEFAULT_TRUSTED_HEADER_USER_ID
    trusted_header_tenant_id: str = DEFAULT_TRUSTED_HEADER_TENANT_ID
    trusted_header_represented_user_id: str = DEFAULT_TRUSTED_HEADER_REPRESENTED_USER_ID
    trusted_header_subject: str = DEFAULT_TRUSTED_HEADER_SUBJECT
    trusted_header_display_name: str = DEFAULT_TRUSTED_HEADER_DISPLAY_NAME
    trusted_header_email: str = DEFAULT_TRUSTED_HEADER_EMAIL
    trusted_header_groups: str = DEFAULT_TRUSTED_HEADER_GROUPS
    trusted_header_roles: str = DEFAULT_TRUSTED_HEADER_ROLES
    trusted_header_provider: str = DEFAULT_TRUSTED_HEADER_PROVIDER
    jwt_secret: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_tenant_claims: tuple[str, ...] = DEFAULT_JWT_TENANT_CLAIMS
    jwt_user_claim: str = "sub"
    jwt_represented_user_claim: str = "represented_user_id"
    jwt_display_name_claim: str = "name"
    jwt_email_claim: str = "email"
    jwt_groups_claim: str = "groups"
    jwt_roles_claim: str = "roles"
    jwt_provider_claim: str = "iss"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AuthConfig":
        data = data or {}
        tenant_claims = data.get("jwt_tenant_claims", DEFAULT_JWT_TENANT_CLAIMS)
        if isinstance(tenant_claims, str):
            tenant_claims = tuple(item.strip() for item in tenant_claims.split(",") if item.strip())
        return cls(
            mode=str(data.get("mode") or "service_token"),
            trusted_header_user_id=str(data.get("trusted_header_user_id") or DEFAULT_TRUSTED_HEADER_USER_ID),
            trusted_header_tenant_id=str(data.get("trusted_header_tenant_id") or DEFAULT_TRUSTED_HEADER_TENANT_ID),
            trusted_header_represented_user_id=str(data.get("trusted_header_represented_user_id") or DEFAULT_TRUSTED_HEADER_REPRESENTED_USER_ID),
            trusted_header_subject=str(data.get("trusted_header_subject") or DEFAULT_TRUSTED_HEADER_SUBJECT),
            trusted_header_display_name=str(data.get("trusted_header_display_name") or DEFAULT_TRUSTED_HEADER_DISPLAY_NAME),
            trusted_header_email=str(data.get("trusted_header_email") or DEFAULT_TRUSTED_HEADER_EMAIL),
            trusted_header_groups=str(data.get("trusted_header_groups") or DEFAULT_TRUSTED_HEADER_GROUPS),
            trusted_header_roles=str(data.get("trusted_header_roles") or DEFAULT_TRUSTED_HEADER_ROLES),
            trusted_header_provider=str(data.get("trusted_header_provider") or DEFAULT_TRUSTED_HEADER_PROVIDER),
            jwt_secret=str(data["jwt_secret"]) if data.get("jwt_secret") else None,
            jwt_issuer=str(data["jwt_issuer"]) if data.get("jwt_issuer") else None,
            jwt_audience=str(data["jwt_audience"]) if data.get("jwt_audience") else None,
            jwt_algorithm=str(data.get("jwt_algorithm") or "HS256"),
            jwt_tenant_claims=tuple(str(item) for item in (tenant_claims or DEFAULT_JWT_TENANT_CLAIMS)),
            jwt_user_claim=str(data.get("jwt_user_claim") or "sub"),
            jwt_represented_user_claim=str(data.get("jwt_represented_user_claim") or "represented_user_id"),
            jwt_display_name_claim=str(data.get("jwt_display_name_claim") or "name"),
            jwt_email_claim=str(data.get("jwt_email_claim") or "email"),
            jwt_groups_claim=str(data.get("jwt_groups_claim") or "groups"),
            jwt_roles_claim=str(data.get("jwt_roles_claim") or "roles"),
            jwt_provider_claim=str(data.get("jwt_provider_claim") or "iss"),
        )

    @classmethod
    def from_env(cls, base: "AuthConfig") -> "AuthConfig":
        tenant_claims = _csv_env_tuple("PSKA_AUTH_JWT_TENANT_CLAIMS") or base.jwt_tenant_claims
        return cls(
            mode=os.getenv("PSKA_AUTH_MODE", base.mode),
            trusted_header_user_id=os.getenv("PSKA_AUTH_HEADER_USER_ID", base.trusted_header_user_id),
            trusted_header_tenant_id=os.getenv("PSKA_AUTH_HEADER_TENANT_ID", base.trusted_header_tenant_id),
            trusted_header_represented_user_id=os.getenv("PSKA_AUTH_HEADER_REPRESENTED_USER_ID", base.trusted_header_represented_user_id),
            trusted_header_subject=os.getenv("PSKA_AUTH_HEADER_SUBJECT", base.trusted_header_subject),
            trusted_header_display_name=os.getenv("PSKA_AUTH_HEADER_DISPLAY_NAME", base.trusted_header_display_name),
            trusted_header_email=os.getenv("PSKA_AUTH_HEADER_EMAIL", base.trusted_header_email),
            trusted_header_groups=os.getenv("PSKA_AUTH_HEADER_GROUPS", base.trusted_header_groups),
            trusted_header_roles=os.getenv("PSKA_AUTH_HEADER_ROLES", base.trusted_header_roles),
            trusted_header_provider=os.getenv("PSKA_AUTH_HEADER_PROVIDER", base.trusted_header_provider),
            jwt_secret=os.getenv("PSKA_AUTH_JWT_SECRET") or base.jwt_secret,
            jwt_issuer=os.getenv("PSKA_AUTH_JWT_ISSUER") or base.jwt_issuer,
            jwt_audience=os.getenv("PSKA_AUTH_JWT_AUDIENCE") or base.jwt_audience,
            jwt_algorithm=os.getenv("PSKA_AUTH_JWT_ALGORITHM", base.jwt_algorithm),
            jwt_tenant_claims=tenant_claims,
            jwt_user_claim=os.getenv("PSKA_AUTH_JWT_USER_CLAIM", base.jwt_user_claim),
            jwt_represented_user_claim=os.getenv("PSKA_AUTH_JWT_REPRESENTED_USER_CLAIM", base.jwt_represented_user_claim),
            jwt_display_name_claim=os.getenv("PSKA_AUTH_JWT_DISPLAY_NAME_CLAIM", base.jwt_display_name_claim),
            jwt_email_claim=os.getenv("PSKA_AUTH_JWT_EMAIL_CLAIM", base.jwt_email_claim),
            jwt_groups_claim=os.getenv("PSKA_AUTH_JWT_GROUPS_CLAIM", base.jwt_groups_claim),
            jwt_roles_claim=os.getenv("PSKA_AUTH_JWT_ROLES_CLAIM", base.jwt_roles_claim),
            jwt_provider_claim=os.getenv("PSKA_AUTH_JWT_PROVIDER_CLAIM", base.jwt_provider_claim),
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
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, api_key_file: Path | None = None) -> "FastreactConfig":
        data = data or {}
        token = data.get("service_token") or data.get("token") or _fastreact_token_from_key_file(api_key_file)
        return cls(
            url=str(data.get("url") or "http://127.0.0.1:8000").rstrip("/"),
            service_token=str(token).strip() if token else None,
            timeout_seconds=float(data["timeout_seconds"]) if data.get("timeout_seconds") else None,
            model=str(data["model"]).strip() if data.get("model") else None,
            temperature=float(data["temperature"]) if data.get("temperature") is not None else None,
            top_p=float(data["top_p"]) if data.get("top_p") is not None else None,
            max_tokens=int(data["max_tokens"]) if data.get("max_tokens") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class AgenticServiceConfigFile:
    provider: str = "fastreact"
    url: str = "http://127.0.0.1:8000"
    service_token: str | None = None
    timeout_seconds: float | None = None
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None

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
            model=str(data["model"]).strip() if data.get("model") else fallback.model,
            temperature=float(data["temperature"]) if data.get("temperature") is not None else fallback.temperature,
            top_p=float(data["top_p"]) if data.get("top_p") is not None else fallback.top_p,
            max_tokens=int(data["max_tokens"]) if data.get("max_tokens") is not None else fallback.max_tokens,
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
    tenant_id: str = DEFAULT_TENANT_ID

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
            tenant_id=str(data.get("tenant_id") or DEFAULT_TENANT_ID),
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
class FrontendConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 5173

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FrontendConfig":
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            host=str(data.get("host") or "127.0.0.1"),
            port=int(data.get("port") or 5173),
        )


@dataclass(frozen=True, slots=True)
class StartupConfig:
    bootstrap: bool = True
    backend: bool = True
    frontend: FrontendConfig = field(default_factory=FrontendConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StartupConfig":
        data = data or {}
        return cls(
            bootstrap=bool(data.get("bootstrap", True)),
            backend=bool(data.get("backend", True)),
            frontend=FrontendConfig.from_dict(data.get("frontend")),
        )


@dataclass(frozen=True, slots=True)
class PSKAConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    fastreact: FastreactConfig = field(default_factory=FastreactConfig)
    agentic_service: AgenticServiceConfigFile = field(default_factory=AgenticServiceConfigFile)
    embedding: EmbeddingConfigFile = field(default_factory=EmbeddingConfigFile)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    startup: StartupConfig = field(default_factory=StartupConfig)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "PSKAConfig":
        path = _find_config_path(config_path)
        if path is None:
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("PSKA config must be a JSON object")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PSKAConfig":
        llm = LLMConfig.from_dict(data.get("llm"))
        fastreact = FastreactConfig.from_dict(data.get("fastreact"), api_key_file=llm.api_key_file)
        return cls(
            database=DatabaseConfig.from_dict(data.get("database")),
            service=ServiceConfig.from_dict(data.get("service"), api_key_file=llm.api_key_file),
            auth=AuthConfig.from_dict(data.get("auth")),
            llm=llm,
            fastreact=fastreact,
            agentic_service=AgenticServiceConfigFile.from_dict(data.get("agentic_service"), fallback=fastreact, api_key_file=llm.api_key_file),
            embedding=EmbeddingConfigFile.from_dict(data.get("embedding")),
            ingest=IngestConfig.from_dict(data.get("ingest")),
            files=FilesConfig.from_dict(data.get("files")),
            workspace=WorkspaceConfig.from_dict(data.get("workspace")),
            startup=StartupConfig.from_dict(data.get("startup")),
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
        agentic_model = os.getenv("PSKA_AGENTIC_SERVICE_MODEL")
        if not agentic_model and base.agentic_service.model == default_agentic.model:
            agentic_model = os.getenv("PSKA_FASTREACT_MODEL")
        agentic_temperature = os.getenv("PSKA_AGENTIC_SERVICE_TEMPERATURE")
        if not agentic_temperature and base.agentic_service.temperature == default_agentic.temperature:
            agentic_temperature = os.getenv("PSKA_FASTREACT_TEMPERATURE")
        agentic_top_p = os.getenv("PSKA_AGENTIC_SERVICE_TOP_P")
        if not agentic_top_p and base.agentic_service.top_p == default_agentic.top_p:
            agentic_top_p = os.getenv("PSKA_FASTREACT_TOP_P")
        agentic_max_tokens = os.getenv("PSKA_AGENTIC_SERVICE_MAX_TOKENS")
        if not agentic_max_tokens and base.agentic_service.max_tokens == default_agentic.max_tokens:
            agentic_max_tokens = os.getenv("PSKA_FASTREACT_MAX_TOKENS")
        return cls(
            database=DatabaseConfig(url=os.getenv("PSKA_DATABASE_URL", base.database.url)),
            service=ServiceConfig(
                host=os.getenv("PSKA_SERVICE_HOST", base.service.host),
                port=int(os.getenv("PSKA_SERVICE_PORT", str(base.service.port))),
                service_token=os.getenv("PSKA_SERVICE_TOKEN") or base.service.service_token,
            ),
            auth=AuthConfig.from_env(base.auth),
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
                model=os.getenv("PSKA_FASTREACT_MODEL") or base.fastreact.model,
                temperature=float(os.getenv("PSKA_FASTREACT_TEMPERATURE")) if os.getenv("PSKA_FASTREACT_TEMPERATURE") else base.fastreact.temperature,
                top_p=float(os.getenv("PSKA_FASTREACT_TOP_P")) if os.getenv("PSKA_FASTREACT_TOP_P") else base.fastreact.top_p,
                max_tokens=int(os.getenv("PSKA_FASTREACT_MAX_TOKENS")) if os.getenv("PSKA_FASTREACT_MAX_TOKENS") else base.fastreact.max_tokens,
            ),
            agentic_service=AgenticServiceConfigFile(
                provider=os.getenv("PSKA_AGENTIC_SERVICE_PROVIDER") or os.getenv("PSKA_AGENTIC_PROVIDER") or base.agentic_service.provider,
                url=(agentic_url or base.agentic_service.url).rstrip("/"),
                service_token=agentic_token or base.agentic_service.service_token,
                timeout_seconds=float(agentic_timeout)
                if agentic_timeout
                else base.agentic_service.timeout_seconds,
                model=agentic_model or base.agentic_service.model,
                temperature=float(agentic_temperature) if agentic_temperature else base.agentic_service.temperature,
                top_p=float(agentic_top_p) if agentic_top_p else base.agentic_service.top_p,
                max_tokens=int(agentic_max_tokens) if agentic_max_tokens else base.agentic_service.max_tokens,
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
                ignore=tuple(item for item in os.getenv("PSKA_FILES_IGNORE", "").split(os.pathsep) if item)
                or base.files.ignore,
                max_bytes=int(os.getenv("PSKA_FILES_MAX_BYTES", str(base.files.max_bytes))),
                owner_user_id=os.getenv("PSKA_FILES_OWNER_USER_ID", base.files.owner_user_id),
                space_id=os.getenv("PSKA_FILES_SPACE_ID", base.files.space_id),
                visibility=os.getenv("PSKA_FILES_VISIBILITY", base.files.visibility),
            ),
            workspace=WorkspaceConfig(
                root=expand_path(os.getenv("PSKA_WORKSPACE_ROOT")) if os.getenv("PSKA_WORKSPACE_ROOT") else base.workspace.root,
            ),
        )

    def embedding_runtime_config(self, *, default_provider: str | None = None) -> "EmbeddingConfig":
        from pska_core.embeddings import BGE_M3_DIMENSIONS, BGE_M3_MODEL, EmbeddingConfig

        return EmbeddingConfig(
            provider=(default_provider if self.embedding.provider in {"", "disabled"} and default_provider is not None else self.embedding.provider),
            model=self.embedding.model or BGE_M3_MODEL,
            dimensions=self.embedding.dimensions or BGE_M3_DIMENSIONS,
            batch_size=self.embedding.batch_size or 16,
        )

    def fastreact_runtime_config(self) -> "RuntimeFastreactConfig":
        from pska_core.fastreact_client import FastreactConfig as RuntimeFastreactConfig

        return RuntimeFastreactConfig(
            url=self.fastreact.url.rstrip("/"),
            service_token=self.fastreact.service_token,
            timeout_seconds=float(self.fastreact.timeout_seconds or 30.0),
            model=self.fastreact.model,
            temperature=self.fastreact.temperature,
            top_p=self.fastreact.top_p,
            max_tokens=self.fastreact.max_tokens,
        )

    def agentic_service_runtime_config(self) -> "AgenticServiceConfig":
        from pska_core.agentic_service import AgenticServiceConfig

        return AgenticServiceConfig(
            provider=self.agentic_service.provider,
            url=self.agentic_service.url.rstrip("/"),
            service_token=self.agentic_service.service_token,
            timeout_seconds=float(self.agentic_service.timeout_seconds or 30.0),
            model=self.agentic_service.model,
            temperature=self.agentic_service.temperature,
            top_p=self.agentic_service.top_p,
            max_tokens=self.agentic_service.max_tokens,
        )

    def ingest_kwargs(self) -> dict[str, int]:
        return {
            "chunk_size": self.ingest.chunk_size,
            "chunk_overlap": self.ingest.chunk_overlap,
        }


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


def _service_token_from_key_file(path: Path | None) -> str | None:
    if path is None:
        return None
    return read_api_key_file(path).service_token or None


def _fastreact_token_from_key_file(path: Path | None) -> str | None:
    if path is None:
        return None
    key_file = read_api_key_file(path)
    return key_file.service_token or None


def _csv_env_tuple(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())
