from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path


def load_wrapper_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "fastreact_http_sse_e2e.py"
    spec = importlib.util.spec_from_file_location("fastreact_http_sse_e2e", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fastreact_http_sse_wrapper_delegates_to_live_smoke(tmp_path) -> None:
    wrapper = load_wrapper_module()
    smoke = tmp_path / "pska-fastreact-kb-scope-smoke"
    smoke.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    smoke.chmod(smoke.stat().st_mode | stat.S_IXUSR)

    command = wrapper.build_command(smoke, timeout=12.5, passthrough=["--marker", "REAL"])

    assert command == [str(smoke), "--timeout-seconds", "12.5", "--marker", "REAL"]


def test_fastreact_http_sse_wrapper_does_not_duplicate_timeout_seconds(tmp_path) -> None:
    wrapper = load_wrapper_module()
    smoke = tmp_path / "pska-fastreact-kb-scope-smoke"
    smoke.write_text("", encoding="utf-8")

    command = wrapper.build_command(smoke, timeout=12.5, passthrough=["--timeout-seconds", "99"])

    assert command == [sys.executable, str(smoke), "--timeout-seconds", "99"]
    assert os.fspath(smoke) in command
