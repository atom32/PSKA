from __future__ import annotations

import json
import os
from pathlib import Path

from pska_core.config import PSKAConfig


def test_pska_config_loads_json_and_keyfile_token(tmp_path: Path) -> None:
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
    assert config.llm.api_key_file == key_file
    assert config.files.roots == (tmp_path / "notes",)
    assert config.files.ignore == ("*.tmp",)
    assert config.files.max_bytes == 1234


def test_pska_config_env_overrides_file(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"database": {"url": "postgresql:///from_file"}}), encoding="utf-8")
    monkeypatch.setenv("PSKA_DATABASE_URL", "postgresql:///from_env")

    config = PSKAConfig.load(config_file)

    assert config.database.url == "postgresql:///from_env"


def test_pska_config_apply_to_env_does_not_overwrite_existing(tmp_path: Path, monkeypatch) -> None:
    config = PSKAConfig.from_dict({"database": {"url": "postgresql:///from_file"}})
    monkeypatch.setenv("PSKA_DATABASE_URL", "postgresql:///existing")

    config.apply_to_env()

    assert os.environ["PSKA_DATABASE_URL"] == "postgresql:///existing"
