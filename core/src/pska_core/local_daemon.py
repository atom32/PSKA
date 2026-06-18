from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Sequence

from pska_core.config import PSKAConfig


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


def run_supervisor(specs: Sequence[ProcessSpec], *, restart: bool = False, restart_delay_seconds: float = 2.0) -> int:
    processes: dict[str, subprocess.Popen] = {}
    stopping = False

    def stop(_signum=None, _frame=None) -> None:
        nonlocal stopping
        stopping = True
        for process in processes.values():
            if process.poll() is None:
                process.terminate()

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for spec in specs:
            processes[spec.name] = _start(spec)
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
                    processes[spec.name] = _start(spec)
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


def _start(spec: ProcessSpec) -> subprocess.Popen:
    env = os.environ.copy()
    print(f"[pska-local-daemon] starting {spec.name}: {' '.join(spec.command)}", file=sys.stderr, flush=True)
    return subprocess.Popen(spec.command, env=env)


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
