from __future__ import annotations

import json
import os
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


def test_pska_config_env_overrides_file(tmp_path: Path, monkeypatch) -> None:
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

    assert config.database.url == "postgresql:///from_env"
    assert config.workspace.root == tmp_path / "from_env"
    assert config.ingest.chunk_size == 512
    assert config.ingest.chunk_overlap == 64


def test_pska_config_apply_to_env_does_not_overwrite_existing(tmp_path: Path, monkeypatch) -> None:
    config = PSKAConfig.from_dict({"database": {"url": "postgresql:///from_file"}, "workspace": {"root": str(tmp_path / "workspace")}})
    monkeypatch.setenv("PSKA_DATABASE_URL", "postgresql:///existing")
    monkeypatch.setenv("PSKA_WORKSPACE_ROOT", str(tmp_path / "existing"))

    config.apply_to_env()

    assert os.environ["PSKA_DATABASE_URL"] == "postgresql:///existing"
    assert os.environ["PSKA_WORKSPACE_ROOT"] == str(tmp_path / "existing")


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
