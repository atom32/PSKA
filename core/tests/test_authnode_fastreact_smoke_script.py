from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pska-fastreact-authnode-smoke"


def load_smoke_script():
    loader = SourceFileLoader("pska_fastreact_authnode_smoke", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_url_includes_authnode_identity() -> None:
    smoke = load_smoke_script()

    url = smoke.proxy_url(
        "http://127.0.0.1:8788/",
        "/agentic-search",
        user_key="pska:user_primary",
        tenant_id="tenant_default",
        authnode_mode="jwt",
    )

    assert url.startswith("http://127.0.0.1:8788/proxy/pska/agentic-search?")
    assert "authnode_user_key=pska%3Auser_primary" in url
    assert "authnode_tenant_id=tenant_default" in url
    assert "authnode_mode=jwt" in url


def test_normalized_pska_tool_names_accept_fastreact_namespace() -> None:
    smoke = load_smoke_script()

    assert smoke.normalized_pska_tool_names({"pska_pska_search", "pska_pska_job_context", "read_file"}) == {
        "pska_pska_search",
        "pska_search",
        "pska_pska_job_context",
        "pska_job_context",
    }


def test_marker_visible_ignores_query_echoes() -> None:
    smoke = load_smoke_script()
    marker = "pska-smoke-marker"

    assert smoke.marker_visible({"query": marker, "results": [], "citations": []}, marker) is False
    assert smoke.marker_visible({"results": [{"snippet": f"answer {marker}"}], "citations": []}, marker) is True


def test_run_identity_summarizes_agentic_response() -> None:
    smoke = load_smoke_script()

    summary = smoke.run_identity(
        {
            "agentic_service": {"run_id": "run_1"},
            "trace": {"session_id": "sess_1"},
            "source_refs": [{"source_item_id": "src_1"}],
        }
    )

    assert summary == {"run_id": "run_1", "session_id": "sess_1", "source_ref_count": 1}
