from __future__ import annotations

import argparse

from scripts.digest_e2e_gate import build_digest_e2e_gate_report
from pska_core.store import InMemoryKnowledgeStore


def test_digest_e2e_gate_covers_schedule_writeback_complete_and_fail() -> None:
    report = build_digest_e2e_gate_report(_args(), store=InMemoryKnowledgeStore())

    assert report["ok"] is True
    assert report["contract"]["ok"] is True
    assert report["contract"]["checks"]["scheduled"] is True
    assert report["contract"]["checks"]["leased"] is True
    assert report["contract"]["checks"]["context_has_source"] is True
    assert report["contract"]["checks"]["missing_source_refs_rejected"] is True
    assert report["contract"]["checks"]["candidate_write_has_source_refs"] is True
    assert report["contract"]["checks"]["completed"] is True
    assert report["contract"]["checks"]["fail_path_failed"] is True
    assert report["steps"]["candidate_write"]["summary"]["review_items"]
    assert report["steps"]["candidate_write"]["summary"]["agent_memories"]
    assert report["steps"]["complete"]["job"]["status"] == "succeeded"
    assert report["steps"]["fail_path"]["job"]["status"] == "failed"
    assert report["fastreact"]["ok"] is False
    assert "worker_command_hint" in report["fastreact"]


def test_digest_e2e_gate_can_require_fastreact_online() -> None:
    args = _args(require_fastreact_online=True)

    report = build_digest_e2e_gate_report(args, store=InMemoryKnowledgeStore())

    assert report["ok"] is False
    assert report["contract"]["ok"] is True
    assert report["fastreact"]["ok"] is False
    assert any("FastReAct is required" in item for item in report["diagnostics"])


def _args(**overrides) -> argparse.Namespace:
    values = {
        "database_url": "postgresql:///unused",
        "owner_user_id": "user_primary",
        "worker_id": "digest-e2e-gate-test",
        "lease_seconds": 120,
        "batch_size": 1,
        "fastreact_url": "http://127.0.0.1:9",
        "fastreact_timeout_seconds": 0.05,
        "require_fastreact_online": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)
