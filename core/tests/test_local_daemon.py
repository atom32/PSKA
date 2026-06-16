from __future__ import annotations

from pathlib import Path

from pska_core.config import PSKAConfig
from pska_core.local_daemon import build_process_specs


def test_build_process_specs_includes_service_worker_and_digest_scheduler() -> None:
    config = PSKAConfig.from_dict(
        {
            "service": {"host": "127.0.0.1", "port": 8765},
            "database": {"url": "postgresql:///pska_test"},
        }
    )

    specs = build_process_specs(
        config_path=Path(".pska/config.json"),
        config=config,
        database_url=config.database.url,
        worker_id="worker_test",
        digest_interval_seconds=60,
    )

    assert [spec.name for spec in specs] == ["pska-service", "pska-job-worker", "pska-digest-scheduler"]
    assert specs[0].command[-4:] == ["serve", "--host", "127.0.0.1", "--port", "8765"][-4:]
    assert "worker_test" in specs[1].command
    assert "digest-scheduler" in specs[2].command
    assert "60" in specs[2].command


def test_build_process_specs_can_disable_background_processes() -> None:
    specs = build_process_specs(
        config_path=None,
        config=PSKAConfig(),
        database_url="postgresql:///pska_test",
        include_worker=False,
        include_digest_scheduler=False,
    )

    assert [spec.name for spec in specs] == ["pska-service"]
