from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Sequence
from urllib.parse import urlparse

from pska_core.config import PSKAConfig, WorkspaceConfig


DEFAULT_RUN_DIR = WorkspaceConfig().run_dir
DEFAULT_LOG_DIR = WorkspaceConfig().log_dir


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    command: list[str]


def build_process_specs(
    *,
    config_path: Path | None,
    config: PSKAConfig,
    database_url: str,
    include_worker: bool = True,
    include_digest_scheduler: bool = True,
    worker_id: str = "pska-worker-local",
    poll_interval: float = 5.0,
    lease_seconds: int = 300,
    recover_stale_seconds: int = 900,
    worker_excluded_job_types: Sequence[str] = ("digest_via_fastreact",),
    digest_interval_seconds: float = 300.0,
    digest_limit: int = 20,
    digest_batch_size: int = 20,
    digest_max_backlog_jobs: int = 10,
) -> list[ProcessSpec]:
    base = [sys.executable, "-m", "pska_core.cli"]
    if config_path:
        base.extend(["--config", str(config_path)])
    base.extend(["--database-url", database_url])

    specs = [
        ProcessSpec(
            "pska-service",
            [
                *base,
                "serve",
                "--host",
                config.service.host,
                "--port",
                str(config.service.port),
            ],
        )
    ]
    if include_worker:
        specs.append(
            ProcessSpec(
                "pska-job-worker",
                [
                    *base,
                    "job-worker",
                    "--worker-id",
                    worker_id,
                    "--poll-interval",
                    str(poll_interval),
                    "--lease-seconds",
                    str(lease_seconds),
                    "--recover-stale-seconds",
                    str(recover_stale_seconds),
                    *[
                        item
                        for job_type in worker_excluded_job_types
                        for item in ("--exclude-job-type", job_type)
                    ],
                ],
            )
        )
    if include_digest_scheduler:
        specs.append(
            ProcessSpec(
                "pska-digest-scheduler",
                [
                    *base,
                    "digest-scheduler",
                    "--owner-user-id",
                    "user_primary",
                    "--interval-seconds",
                    str(digest_interval_seconds),
                    "--limit",
                    str(digest_limit),
                    "--batch-size",
                    str(digest_batch_size),
                    "--max-backlog-jobs",
                    str(digest_max_backlog_jobs),
                    "--recover-stale-seconds",
                    str(recover_stale_seconds),
                ],
            )
        )
    return specs


def daemon_status(specs: Sequence[ProcessSpec], *, run_dir: Path = DEFAULT_RUN_DIR, log_dir: Path = DEFAULT_LOG_DIR) -> dict[str, Any]:
    run_dir = run_dir.expanduser()
    log_dir = log_dir.expanduser()
    processes = []
    for spec in specs:
        pid = _read_pid(_pid_path(run_dir, spec.name))
        running = _pid_running(pid)
        status_source = "pid_file" if pid is not None else "missing_pid_file"
        if not running:
            recovered_pid = _find_process_for_spec(spec)
            if recovered_pid is not None:
                pid = recovered_pid
                running = True
                status_source = "command_scan"
        processes.append(
            {
                "name": spec.name,
                "pid": pid,
                "running": running,
                "status": "running" if running else "stopped",
                "status_source": status_source,
                "pid_path": str(_pid_path(run_dir, spec.name)),
                "log_path": str(_log_path(log_dir, spec.name)),
                "command": spec.command,
            }
        )
    return {
        "ok": all(process["running"] for process in processes),
        "run_dir": str(run_dir),
        "log_dir": str(log_dir),
        "processes": processes,
        "restart_guidance": [
            "./scripts/pska local-daemon --restart",
            "./scripts/pska local-daemon supervisor-config --supervisor supervisord --dry-run",
        ],
    }


def config_check(config: PSKAConfig, *, database_url: str) -> dict[str, Any]:
    checks = {
        "database_url": _database_url_check(database_url),
        "workspace": _workspace_check(config.workspace.root),
        "service_port": _service_port_check(config.service.host, config.service.port),
        "fastreact": _fastreact_config_check(config),
    }
    return {
        "ok": all(check["ok"] for check in checks.values()),
        "checks": checks,
        "recovery_commands": _config_check_recovery_commands(checks),
    }


def supervisor_config(
    specs: Sequence[ProcessSpec],
    *,
    supervisor: str,
    run_dir: Path = DEFAULT_RUN_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    working_directory: Path | None = None,
    label_prefix: str = "local.pska",
) -> dict[str, Any]:
    run_dir = run_dir.expanduser()
    log_dir = log_dir.expanduser()
    if supervisor == "supervisord":
        content = _supervisord_config(specs, run_dir=run_dir, log_dir=log_dir)
        target_path = run_dir / "pska-supervisord.conf"
        install_commands = [
            f"mkdir -p {run_dir} {log_dir}",
            f"supervisord -c {target_path}",
            f"supervisorctl -c {target_path} status",
        ]
        return {
            "ok": True,
            "supervisor": supervisor,
            "dry_run": True,
            "target_path": str(target_path),
            "content": content,
            "install_commands": install_commands,
        }
    if supervisor == "launchd":
        plists = []
        for spec in specs:
            label = f"{label_prefix}.{spec.name}"
            target_path = Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist"
            plists.append(
                {
                    "name": spec.name,
                    "label": label,
                    "target_path": str(target_path),
                    "content": _launchd_plist(
                        spec,
                        label=label,
                        run_dir=run_dir,
                        log_dir=log_dir,
                        working_directory=working_directory,
                    ),
                }
            )
        return {
            "ok": True,
            "supervisor": supervisor,
            "dry_run": True,
            "plists": plists,
            "install_commands": [
                "mkdir -p ~/Library/LaunchAgents",
                "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.pska.<name>.plist",
                "launchctl print gui/$(id -u)/local.pska.<name>",
            ],
        }
    raise ValueError(f"Unsupported supervisor: {supervisor}")


def run_supervisor(
    specs: Sequence[ProcessSpec],
    *,
    restart: bool = False,
    restart_delay_seconds: float = 2.0,
    run_dir: Path = DEFAULT_RUN_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> int:
    processes: dict[str, subprocess.Popen] = {}
    log_files = []
    stopping = False

    def stop(_signum=None, _frame=None) -> None:
        nonlocal stopping
        stopping = True
        for process in processes.values():
            if process.poll() is None:
                process.terminate()

    run_dir = run_dir.expanduser()
    log_dir = log_dir.expanduser()
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for spec in specs:
            process, log_file = _start(spec, run_dir=run_dir, log_dir=log_dir)
            processes[spec.name] = process
            log_files.append(log_file)
        while processes:
            for spec in specs:
                process = processes.get(spec.name)
                if process is None:
                    continue
                returncode = process.poll()
                if returncode is None:
                    continue
                if stopping:
                    processes.pop(spec.name, None)
                    continue
                print(f"[pska-local-daemon] {spec.name} exited with {returncode}", file=sys.stderr, flush=True)
                if restart:
                    time.sleep(restart_delay_seconds)
                    _remove_pid(run_dir, spec.name)
                    process, log_file = _start(spec, run_dir=run_dir, log_dir=log_dir)
                    processes[spec.name] = process
                    log_files.append(log_file)
                else:
                    stop()
                    _wait_all(processes)
                    return returncode or 1
            time.sleep(0.5)
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        stop()
        _wait_all(processes)
        for spec in specs:
            _remove_pid(run_dir, spec.name)
        for log_file in log_files:
            log_file.close()


def _start(spec: ProcessSpec, *, run_dir: Path, log_dir: Path) -> tuple[subprocess.Popen, Any]:
    env = os.environ.copy()
    log_path = _log_path(log_dir, spec.name)
    log_file = log_path.open("ab")
    print(f"[pska-local-daemon] starting {spec.name}: {' '.join(spec.command)}", file=sys.stderr, flush=True)
    process = subprocess.Popen(spec.command, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    _pid_path(run_dir, spec.name).write_text(str(process.pid), encoding="utf-8")
    return process, log_file


def _wait_all(processes: dict[str, subprocess.Popen]) -> None:
    deadline = time.monotonic() + 10
    for process in processes.values():
        if process.poll() is not None:
            continue
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()


def _pid_path(run_dir: Path, name: str) -> Path:
    return run_dir / f"{name}.pid"


def _log_path(log_dir: Path, name: str) -> Path:
    return log_dir / f"{name}.log"


def _remove_pid(run_dir: Path, name: str) -> None:
    _pid_path(run_dir, name).unlink(missing_ok=True)


def _read_pid(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _find_process_for_spec(spec: ProcessSpec) -> int | None:
    try:
        result = subprocess.run(["ps", "-ax", "-o", "pid=", "-o", "command="], check=False, capture_output=True, text=True)
    except OSError:
        return None
    expected_args = [arg for arg in spec.command[1:] if arg]
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            pid_text, command = stripped.split(maxsplit=1)
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        if all(arg in command for arg in expected_args):
            return pid
    return None


def _database_url_check(database_url: str) -> dict[str, Any]:
    parsed = urlparse(database_url)
    ok = bool(database_url and parsed.scheme in {"postgresql", "postgres"})
    return {
        "ok": ok,
        "database_url": database_url,
        "diagnostic": "PostgreSQL URL configured." if ok else "Missing or non-PostgreSQL database URL.",
    }


def _workspace_check(workspace_root: Path) -> dict[str, Any]:
    root = workspace_root.expanduser()
    resolved = root.resolve(strict=False)
    legacy_repo_workspace = Path.cwd().resolve(strict=False) / "workspaces" / "default"
    warning = None
    if resolved == legacy_repo_workspace or legacy_repo_workspace in resolved.parents:
        warning = "Workspace root is inside repo workspaces/default; use ~/PSKA_workspaces/default for runtime data."
    return {
        "ok": True,
        "root": str(root),
        "imports_dir": str(root / "imports"),
        "twitter_archive_dir": str(root / "twitter_archive"),
        "run_dir": str(root / "run"),
        "log_dir": str(root / "logs"),
        "warning": warning,
    }


def _service_port_check(host: str, port: int) -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        result = sock.connect_ex((host, port))
    available = result != 0
    return {
        "ok": available,
        "host": host,
        "port": port,
        "diagnostic": "Port appears available." if available else "Port is already accepting TCP connections.",
    }


def _fastreact_config_check(config: PSKAConfig) -> dict[str, Any]:
    url = config.fastreact.url.rstrip("/")
    parsed = urlparse(url)
    url_ok = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    token_present = bool(config.fastreact.service_token)
    return {
        "ok": url_ok,
        "url": url,
        "service_token_present": token_present,
        "api_endpoint": url,
        "ui_endpoint_note": "FastReAct UI may run on a different port such as http://127.0.0.1:3000/service; PSKA agentic calls use this API endpoint.",
        "diagnostic": "FastReAct API URL is configured for PSKA agentic calls." if url_ok else "FastReAct API URL is missing or invalid.",
        "warning": None if token_present else "No FastReAct service token configured for PSKA->FastReAct API calls; /ready and /v1/runs may return 401 even if the FastReAct UI works.",
    }


def _config_check_recovery_commands(checks: dict[str, dict[str, Any]]) -> list[str]:
    commands = []
    if not checks["database_url"]["ok"]:
        commands.append("./scripts/pska --database-url postgresql:///pska db-init")
    if not checks["service_port"]["ok"]:
        commands.append("lsof -iTCP:<port> -sTCP:LISTEN")
        commands.append("edit .pska/config.json service.port, then run ./start.sh")
    if not checks["fastreact"]["ok"] or not checks["fastreact"].get("service_token_present"):
        commands.append("edit .pska/config.json fastreact.url and fastreact.service_token")
    return commands


def _supervisord_config(specs: Sequence[ProcessSpec], *, run_dir: Path, log_dir: Path) -> str:
    lines = [
        "[supervisord]",
        "nodaemon=true",
        f"pidfile={run_dir / 'supervisord.pid'}",
        "",
    ]
    for spec in specs:
        lines.extend(
            [
                f"[program:{spec.name}]",
                f"command={shlex.join(spec.command)}",
                "autorestart=true",
                "startsecs=2",
                f"stdout_logfile={_log_path(log_dir, spec.name)}",
                "redirect_stderr=true",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _launchd_plist(
    spec: ProcessSpec,
    *,
    label: str,
    run_dir: Path,
    log_dir: Path,
    working_directory: Path | None = None,
) -> str:
    payload = {
        "Label": label,
        "ProgramArguments": spec.command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(_log_path(log_dir, spec.name)),
        "StandardErrorPath": str(_log_path(log_dir, spec.name)),
        "WorkingDirectory": str((working_directory or Path(__file__).resolve().parents[3]).expanduser().resolve()),
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")
