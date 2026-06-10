from __future__ import annotations

from pathlib import Path


def test_extension_writes_v2_metadata_and_payload() -> None:
    background = Path("extension/background.js").read_text(encoding="utf-8")
    manifest = Path("extension/manifest.json").read_text(encoding="utf-8")

    assert '"version": "0.4.0"' in manifest
    assert "const SCHEMA_VERSION = \"pska.archive.v2\"" in background
    assert "source_id: record.id" in background
    assert "canonical_url:" in background
    assert "capture:" in background
    assert "artifacts:" in background
    assert "pska:" in background
    assert "extraction:" in background
    assert "quoted_items:" in background
    assert "pska_payload.json" in background


def test_popup_exposes_anonymous_pska_defaults() -> None:
    popup = Path("extension/popup.html").read_text(encoding="utf-8")
    script = Path("extension/popup.js").read_text(encoding="utf-8")

    assert 'id="owner-user-id"' in popup
    assert 'value="user_primary"' in popup
    assert 'id="space-id"' in popup
    assert 'value="private_primary"' in popup
    assert "visible_team_ids" in script
