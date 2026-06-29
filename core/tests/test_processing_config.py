from __future__ import annotations

from pska_core.processing import resolve_processing_config


def test_processing_config_merges_source_and_run_overrides() -> None:
    config = resolve_processing_config(
        {
            "processing": {
                "chunking": {"strategy": "heading", "chunk_size": 900},
                "digest": {"enabled": False},
            }
        },
        {
            "chunking": {"chunk_overlap": 120},
            "digest": {"enabled": True},
        },
    )

    assert config["chunking"]["strategy"] == "heading"
    assert config["chunking"]["chunk_size"] == 900
    assert config["chunking"]["chunk_overlap"] == 120
    assert config["digest"]["enabled"] is True
    assert config["graph"]["enabled"] is True


def test_processing_config_normalizes_invalid_chunking_values() -> None:
    config = resolve_processing_config(
        {
            "chunking": {
                "strategy": "unknown",
                "chunk_size": -1,
                "chunk_overlap": 5000,
            }
        }
    )

    assert config["chunking"]["strategy"] == "auto"
    assert config["chunking"]["chunk_size"] == 1200
    assert config["chunking"]["chunk_overlap"] < config["chunking"]["chunk_size"]
