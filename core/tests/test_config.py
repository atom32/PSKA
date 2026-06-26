from __future__ import annotations

import json
from pathlib import Path

from pska_core.config import DEFAULT_WORKSPACE_ROOT, PSKAConfig


def test_pska_config_loads_json_and_keyfile_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PSKA_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("PSKA_INGEST_CHUNK_SIZE", raising=False)
    monkeypatch.delenv("PSKA_INGEST_CHUNK_CHARS", raising=False)
    monkeypatch.delenv("PSKA_INGEST_CHUNK_OVERLAP", raising=False)
    key_file = tmp_path / "api_key.txt"
    key_file.write_text(
        json.dumps(
            {
                "api_key": "sk-test",
                "model": "deepseek-v4-flash",
                "base_url": "https://api.deepseek.com",
                "service_token": "shared-local-token",
            }
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "database": {"url": "postgresql:///pska_test"},
                "service": {"host": "127.0.0.1", "port": 9876},
                "llm": {"api_key_file": str(key_file)},
                "fastreact": {"url": "http://127.0.0.1:9000"},
                "embedding": {"provider": "disabled"},
                "ingest": {"chunk_size": 2048, "chunk_overlap": 256},
                "workspace": {"root": str(tmp_path / "workspace")},
                "files": {"roots": [str(tmp_path / "notes")], "ignore": ["*.tmp"], "max_bytes": 1234},
            }
        ),
        encoding="utf-8",
    )

    config = PSKAConfig.load(config_file)

    assert config.database.url == "postgresql:///pska_test"
    assert config.service.port == 9876
    assert config.service.service_token == "shared-local-token"
    assert config.fastreact.url == "http://127.0.0.1:9000"
    assert config.fastreact.service_token == "shared-local-token"
    assert config.agentic_service.provider == "fastreact"
    assert config.agentic_service.url == "http://127.0.0.1:9000"
    assert config.agentic_service.service_token == "shared-local-token"
    assert config.ingest.chunk_size == 2048
    assert config.ingest.chunk_overlap == 256
    assert config.llm.api_key_file == key_file
    assert config.files.roots == (tmp_path / "notes",)
    assert config.files.ignore == ("*.tmp",)
    assert config.files.max_bytes == 1234
    assert config.workspace.root == tmp_path / "workspace"
    assert config.workspace.imports_dir == tmp_path / "workspace" / "imports"
    assert config.workspace.twitter_archive_dir == tmp_path / "workspace" / "twitter_archive"


def test_pska_config_load_does_not_use_env_overrides(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"database": {"url": "postgresql:///from_file"}, "workspace": {"root": str(tmp_path / "from_file")}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PSKA_DATABASE_URL", "postgresql:///from_env")
    monkeypatch.setenv("PSKA_WORKSPACE_ROOT", str(tmp_path / "from_env"))
    monkeypatch.setenv("PSKA_INGEST_CHUNK_SIZE", "512")
    monkeypatch.setenv("PSKA_INGEST_CHUNK_OVERLAP", "64")

    config = PSKAConfig.load(config_file)

    assert config.database.url == "postgresql:///from_file"
    assert config.workspace.root == tmp_path / "from_file"
    assert config.ingest.chunk_size == 1200
    assert config.ingest.chunk_overlap == 0


def test_pska_config_default_workspace_root(monkeypatch) -> None:
    monkeypatch.delenv("PSKA_WORKSPACE_ROOT", raising=False)

    config = PSKAConfig.from_env(PSKAConfig.from_dict({}))

    assert config.workspace.root == DEFAULT_WORKSPACE_ROOT.expanduser()


def test_pska_config_loads_generic_agentic_service(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PSKA_AGENTIC_SERVICE_URL", raising=False)
    monkeypatch.delenv("PSKA_AGENTIC_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("PSKA_AGENTIC_SERVICE_TIMEOUT_SECONDS", raising=False)
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "agentic_service": {
                    "provider": "fastreact",
                    "url": "http://127.0.0.1:9010",
                    "service_token": "agent-token",
                    "timeout_seconds": 12,
                    "authnode_url": "http://127.0.0.1:8788",
                    "authnode_admin_token": "admin-token",
                    "authnode_audience": "fastreact",
                    "authnode_token_ttl_seconds": 600,
                }
            }
        ),
        encoding="utf-8",
    )

    config = PSKAConfig.load(config_file)

    assert config.agentic_service.provider == "fastreact"
    assert config.agentic_service.url == "http://127.0.0.1:9010"
    assert config.agentic_service.service_token == "agent-token"
    assert config.agentic_service.timeout_seconds == 12
    assert config.agentic_service.authnode_url == "http://127.0.0.1:8788"
    assert config.agentic_service.authnode_admin_token == "admin-token"
    assert config.agentic_service.authnode_audience == "fastreact"
    assert config.agentic_service.authnode_token_ttl_seconds == 600


def test_pska_config_loads_startup_config(tmp_path: Path) -> None:
    config = PSKAConfig.from_dict(
        {
            "startup": {
                "bootstrap": False,
                "backend": True,
                "frontend": {"enabled": False, "host": "127.0.0.2", "port": 5174},
            }
        }
    )

    assert config.startup.bootstrap is False
    assert config.startup.backend is True
    assert config.startup.frontend.enabled is False
    assert config.startup.frontend.host == "127.0.0.2"
    assert config.startup.frontend.port == 5174


def test_pska_config_loads_auth_config(monkeypatch) -> None:
    monkeypatch.delenv("PSKA_AUTH_MODE", raising=False)
    config = PSKAConfig.from_dict(
        {
            "auth": {
                "mode": "jwt",
                "jwt_secret": "jwt-secret",
                "jwt_issuer": "issuer",
                "jwt_audience": "pska",
                "jwt_tenant_claims": "tenant_key,org_id",
                "trusted_header_user_id": "X-Identity-User",
            }
        }
    )

    assert config.auth.mode == "jwt"
    assert config.auth.jwt_secret == "jwt-secret"
    assert config.auth.jwt_issuer == "issuer"
    assert config.auth.jwt_audience == "pska"
    assert config.auth.jwt_tenant_claims == ("tenant_key", "org_id")
    assert config.auth.trusted_header_user_id == "X-Identity-User"


def test_pska_config_auth_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_AUTH_MODE", "trusted_headers")
    monkeypatch.setenv("PSKA_AUTH_HEADER_USER_ID", "X-SSO-User")
    monkeypatch.setenv("AUTHNODE_JWT_SECRET", "authnode-secret")
    monkeypatch.setenv("PSKA_AUTH_JWT_TENANT_CLAIMS", "tenant_key,tenant")
    monkeypatch.delenv("PSKA_AUTH_JWT_SECRET", raising=False)

    config = PSKAConfig.from_env(PSKAConfig.from_dict({}))

    assert config.auth.mode == "trusted_headers"
    assert config.auth.trusted_header_user_id == "X-SSO-User"
    assert config.auth.jwt_secret == "authnode-secret"
    assert config.auth.jwt_tenant_claims == ("tenant_key", "tenant")


def test_pska_config_builds_runtime_configs(tmp_path: Path) -> None:
    config = PSKAConfig.from_dict(
        {
            "fastreact": {
                "url": "http://127.0.0.1:9000",
                "service_token": "fast-token",
                "timeout_seconds": 9,
                "model": "deepseek-v4-flash",
                "temperature": 0.3,
                "top_p": 0.9,
                "max_tokens": 4096,
                "authnode_url": "http://127.0.0.1:8788",
                "authnode_admin_token": "fast-admin",
                "authnode_token_ttl_seconds": 300,
            },
            "agentic_service": {
                "url": "http://127.0.0.1:9010",
                "service_token": "agent-token",
                "timeout_seconds": 12,
                "authnode_admin_token": "agent-admin",
            },
            "embedding": {"provider": "bge-m3", "model": "custom-bge", "dimensions": 768, "batch_size": 8},
            "ingest": {"chunk_size": 512, "chunk_overlap": 32},
        }
    )

    fastreact = config.fastreact_runtime_config()
    agentic = config.agentic_service_runtime_config()
    embedding = config.embedding_runtime_config()

    assert fastreact.url == "http://127.0.0.1:9000"
    assert fastreact.service_token == "fast-token"
    assert fastreact.timeout_seconds == 9
    assert fastreact.model == "deepseek-v4-flash"
    assert fastreact.temperature == 0.3
    assert fastreact.top_p == 0.9
    assert fastreact.max_tokens == 4096
    assert fastreact.authnode_url == "http://127.0.0.1:8788"
    assert fastreact.authnode_admin_token == "fast-admin"
    assert fastreact.authnode_audience == "fastreact"
    assert fastreact.authnode_token_ttl_seconds == 300
    assert agentic.url == "http://127.0.0.1:9010"
    assert agentic.service_token == "agent-token"
    assert agentic.timeout_seconds == 12
    assert agentic.model == "deepseek-v4-flash"
    assert agentic.temperature == 0.3
    assert agentic.top_p == 0.9
    assert agentic.max_tokens == 4096
    assert agentic.authnode_url == "http://127.0.0.1:8788"
    assert agentic.authnode_admin_token == "agent-admin"
    assert agentic.authnode_audience == "fastreact"
    assert agentic.authnode_token_ttl_seconds == 300
    assert embedding.provider == "bge-m3"
    assert embedding.model == "custom-bge"
    assert embedding.dimensions == 768
    assert embedding.batch_size == 8
    assert config.ingest_kwargs() == {"chunk_size": 512, "chunk_overlap": 32}


def test_pska_config_from_env_remains_explicit_legacy_loader(tmp_path: Path, monkeypatch) -> None:
    notes = tmp_path / "notes"
    docs = tmp_path / "docs"
    monkeypatch.setenv("PSKA_FILES_ROOTS", f"{notes}:{docs}")
    monkeypatch.setenv("PSKA_FILES_IGNORE", "*.tmp:*.bak")
    monkeypatch.setenv("PSKA_FILES_MAX_BYTES", "777")
    reloaded = PSKAConfig.from_env(PSKAConfig.from_dict({}))

    assert reloaded.files.roots == (notes, docs)
    assert reloaded.files.ignore == ("*.tmp", "*.bak")
    assert reloaded.files.max_bytes == 777


def test_pska_config_agentic_authnode_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("PSKA_AGENTIC_SERVICE_AUTHNODE_URL", "http://authnode.test/")
    monkeypatch.setenv("PSKA_AGENTIC_SERVICE_AUTHNODE_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("PSKA_AGENTIC_SERVICE_AUTHNODE_AUDIENCE", "fastreact")
    monkeypatch.setenv("PSKA_AGENTIC_SERVICE_AUTHNODE_TOKEN_TTL_SECONDS", "900")

    config = PSKAConfig.from_env(PSKAConfig.from_dict({}))
    runtime = config.agentic_service_runtime_config()

    assert runtime.authnode_url == "http://authnode.test"
    assert runtime.authnode_admin_token == "admin-token"
    assert runtime.authnode_audience == "fastreact"
    assert runtime.authnode_token_ttl_seconds == 900
