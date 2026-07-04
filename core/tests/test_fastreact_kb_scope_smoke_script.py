from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pska-fastreact-kb-scope-smoke"


def load_smoke_script():
    loader = SourceFileLoader("pska_fastreact_kb_scope_smoke", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scope_from_event_reads_top_level_nested_and_policy_scope() -> None:
    smoke = load_smoke_script()

    scope = smoke.scope_from_event(
        {
            "tool_name": "pska_pska_search",
            "tool_args": {
                "knowledge_base_ids": ["kb_alpha"],
                "scope_mode": "hard",
                "scope": {"source_item_ids": ["src_alpha"]},
            },
            "metadata": {
                "tool_policy_scope_applied": True,
                "tool_policy": {
                    "scope": {
                        "mode": "hard",
                        "scope_mode": "hard",
                        "knowledge_base_ids": ["kb_alpha"],
                        "source_item_ids": ["src_alpha"],
                    }
                },
            },
        }
    )

    assert scope == {
        "tool_name": "pska_pska_search",
        "knowledge_base_ids": ["kb_alpha"],
        "source_item_ids": ["src_alpha"],
        "scope_mode": "hard",
        "metadata_scope_applied": True,
        "metadata_tool_policy_scope": {
            "mode": "hard",
            "scope_mode": "hard",
            "knowledge_base_ids": ["kb_alpha"],
            "source_item_ids": ["src_alpha"],
        },
    }


def test_cleanup_zero_requires_all_residue_tables() -> None:
    smoke = load_smoke_script()

    assert smoke.cleanup_zero({"residue_counts": {table: 0 for table in smoke.RESIDUE_TABLES}}) is True
    assert smoke.cleanup_zero({"residue_counts": {"knowledge_bases": 0}}) is False
    assert smoke.cleanup_zero({"residue_counts": {**{table: 0 for table in smoke.RESIDUE_TABLES}, "chunks": 1}}) is False


def test_cleanup_sql_uses_precise_marker_and_expected_columns() -> None:
    smoke = load_smoke_script()

    sql = smoke.cleanup_sql(tenant_id="tenant_graphintell", owner_user_id="test_user", marker="REAL_DEEP_KB_SCOPE_abc")

    assert "knowledge_sources" in sql
    assert "name like '%REAL_DEEP_KB_SCOPE_abc%'" in sql
    assert "title like '%REAL_DEEP_KB_SCOPE_abc%'" in sql
    assert "passage_windows" in sql
    assert "json_build_object" in sql
