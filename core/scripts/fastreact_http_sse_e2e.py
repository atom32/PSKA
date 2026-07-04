from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
PSKA_ROOT = ROOT.parent
LIVE_SMOKE = PSKA_ROOT / "scripts" / "pska-fastreact-kb-scope-smoke"


def main(argv: Sequence[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    script = args.smoke_script.expanduser()
    if not script.exists():
        print(f"[fastreact-http-sse-e2e] live smoke script not found: {script}", file=sys.stderr)
        return 2

    if args.python:
        print(
            "[fastreact-http-sse-e2e] --python is deprecated and ignored; "
            "this gate uses the running PSKA service, running FastReAct daemon, real LLM, and HTTP MCP.",
            file=sys.stderr,
        )

    command = build_command(script, timeout=args.timeout, passthrough=passthrough)
    print(f"[fastreact-http-sse-e2e] delegating to live smoke: {shlex.join(command)}", flush=True)
    return subprocess.run(command, cwd=PSKA_ROOT, check=False).returncode


def parse_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Compatibility wrapper for the live PSKA -> FastReAct -> PSKA HTTP MCP smoke. "
            "It no longer injects a deterministic FastReAct test agent."
        )
    )
    parser.add_argument(
        "--python",
        help="Deprecated compatibility option from the old in-process fake-agent gate; ignored.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Compatibility alias for pska-fastreact-kb-scope-smoke --timeout-seconds.",
    )
    parser.add_argument(
        "--smoke-script",
        type=Path,
        default=LIVE_SMOKE,
        help="Path to the live pska-fastreact-kb-scope-smoke script.",
    )
    return parser.parse_known_args(argv)


def build_command(script: Path, *, timeout: float | None, passthrough: Sequence[str]) -> list[str]:
    command = [str(script)] if os.access(script, os.X_OK) else [sys.executable, str(script)]
    if timeout is not None and "--timeout-seconds" not in passthrough:
        command.extend(["--timeout-seconds", str(timeout)])
    command.extend(passthrough)
    return command


if __name__ == "__main__":
    raise SystemExit(main())
