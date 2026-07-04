from __future__ import annotations

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pska-phase1-multikb-release-gate"


def load_gate_script():
    loader = SourceFileLoader("pska_phase1_multikb_release_gate", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


def make_fastreact_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "FastReAct" / "fastreact-nano"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = \"fastreact-nano\"\n", encoding="utf-8")
    return repo


def test_normalize_fastreact_repo_accepts_parent_directory(tmp_path: Path) -> None:
    gate = load_gate_script()
    repo = make_fastreact_repo(tmp_path)

    assert gate.normalize_fastreact_repo(repo.parent) == repo
    assert gate.normalize_fastreact_repo(repo) == repo


def test_default_gate_plan_avoids_live_browser_and_llm_checks(tmp_path: Path) -> None:
    gate = load_gate_script()
    repo = make_fastreact_repo(tmp_path)
    args = argparse.Namespace(
        fastreact_repo=repo,
        skip_core=False,
        skip_frontend_build=False,
        skip_fastreact_contracts=False,
        skip_diff_check=False,
        include_browser_e2e=False,
        include_fastreact_smoke=False,
    )

    steps = gate.build_steps(args)

    assert [step.name for step in steps] == [
        "pska_core_pytest",
        "frontend_build",
        "fastreact_contracts",
        "pska_diff_check",
        "fastreact_diff_check",
    ]
    assert all(not step.live for step in steps)


def test_live_flags_add_real_browser_and_fastreact_scope_gates(tmp_path: Path) -> None:
    gate = load_gate_script()
    repo = make_fastreact_repo(tmp_path)
    args = argparse.Namespace(
        fastreact_repo=repo,
        skip_core=True,
        skip_frontend_build=True,
        skip_fastreact_contracts=True,
        skip_diff_check=True,
        include_browser_e2e=True,
        include_fastreact_smoke=True,
    )

    steps = gate.build_steps(args)

    assert [step.name for step in steps] == [
        "browser_multi_kb_scoped_ask",
        "real_fastreact_kb_scope_smoke",
    ]
    assert all(step.live for step in steps)


def test_empty_gate_plan_is_rejected(tmp_path: Path) -> None:
    gate = load_gate_script()
    repo = make_fastreact_repo(tmp_path)
    args = argparse.Namespace(
        fastreact_repo=repo,
        skip_core=True,
        skip_frontend_build=True,
        skip_fastreact_contracts=True,
        skip_diff_check=True,
        include_browser_e2e=False,
        include_fastreact_smoke=False,
    )

    try:
        gate.build_steps(args)
    except gate.GateFailure as exc:
        assert "No gate steps selected" in str(exc)
    else:
        raise AssertionError("expected GateFailure")
