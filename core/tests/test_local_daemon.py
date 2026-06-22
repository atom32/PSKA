from __future__ import annotations

from pathlib import Path
import socket

from pska_core.config import PSKAConfig
from pska_core.local_daemon import build_process_specs, config_check, daemon_status, supervisor_config


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
    assert "--exclude-job-type" in specs[1].command
    assert "digest_via_fastreact" in specs[1].command
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


def test_daemon_status_reports_pid_and_log_paths(tmp_path) -> None:
    specs = build_process_specs(
        config_path=None,
        config=PSKAConfig(),
        database_url="postgresql:///pska_test",
        include_worker=False,
        include_digest_scheduler=False,
    )
    run_dir = tmp_path / "run"
    log_dir = tmp_path / "logs"
    run_dir.mkdir()
    (run_dir / "pska-service.pid").write_text("999999", encoding="utf-8")

    status = daemon_status(specs, run_dir=run_dir, log_dir=log_dir)

    assert status["ok"] is False
    assert status["processes"][0]["name"] == "pska-service"
    assert status["processes"][0]["pid"] == 999999
    assert status["processes"][0]["running"] is False
    assert status["processes"][0]["pid_path"].endswith("pska-service.pid")
    assert status["processes"][0]["log_path"].endswith("pska-service.log")
    assert "./scripts/pska local-daemon --restart" in status["restart_guidance"]


def test_config_check_reports_missing_db_port_conflict_and_fastreact_warning() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        config = PSKAConfig.from_dict({"service": {"host": "127.0.0.1", "port": port}, "fastreact": {"url": "http://127.0.0.1:8000"}})

        report = config_check(config, database_url="")

    assert report["ok"] is False
    assert report["checks"]["database_url"]["ok"] is False
    assert report["checks"]["workspace"]["ok"] is True
    assert report["checks"]["workspace"]["root"].endswith("PSKA_workspaces/default")
    assert report["checks"]["service_port"]["ok"] is False
    assert report["checks"]["fastreact"]["ok"] is True
    assert "No FastReAct service token configured for PSKA->FastReAct API calls" in report["checks"]["fastreact"]["warning"]
    assert "3000/service" in report["checks"]["fastreact"]["ui_endpoint_note"]
    assert any("db-init" in command for command in report["recovery_commands"])
    assert any("lsof" in command for command in report["recovery_commands"])


def test_supervisor_config_dry_run_outputs_supervisord_and_launchd(tmp_path) -> None:
    specs = build_process_specs(
        config_path=Path(".pska/config.json"),
        config=PSKAConfig(),
        database_url="postgresql:///pska_test",
        include_worker=False,
        include_digest_scheduler=False,
    )

    supervisord = supervisor_config(specs, supervisor="supervisord", run_dir=tmp_path / "run", log_dir=tmp_path / "logs")
    launchd = supervisor_config(
        specs,
        supervisor="launchd",
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
        working_directory=tmp_path / "repo",
    )

    assert supervisord["dry_run"] is True
    assert "[program:pska-service]" in supervisord["content"]
    assert "stdout_logfile" in supervisord["content"]
    assert any("supervisord -c" in command for command in supervisord["install_commands"])
    assert launchd["dry_run"] is True
    assert launchd["plists"][0]["label"] == "local.pska.pska-service"
    assert "<key>ProgramArguments</key>" in launchd["plists"][0]["content"]
    assert str(tmp_path / "repo") in launchd["plists"][0]["content"]
