from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


DEFAULT_PROCESSING_CONFIG: dict[str, Any] = {
    "chunking": {
        "strategy": "auto",
        "chunk_size": 1200,
        "chunk_overlap": 0,
        "separators": ["\n\n", "\n", "。", ". ", " "],
    },
    "extraction": {
        "enabled": True,
    },
    "digest": {
        "enabled": True,
        "auto": False,
    },
    "graph": {
        "enabled": True,
    },
}


def resolve_processing_config(
    source_config: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge processing config from defaults, source, then run-level overrides."""

    config = deepcopy(DEFAULT_PROCESSING_CONFIG)
    for layer in (defaults, source_config, overrides):
        extracted = _extract_processing_config(layer)
        if extracted:
            _deep_merge(config, extracted)
    config["chunking"] = normalize_chunking_config(config.get("chunking"))
    return config


def chunking_config_from_processing(config: Mapping[str, Any] | None) -> dict[str, Any]:
    processing = resolve_processing_config(config)
    return normalize_chunking_config(processing.get("chunking"))


def normalize_chunking_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    strategy = str(raw.get("strategy") or raw.get("mode") or "auto").strip().lower()
    if strategy in {"legacy", "chars", "character"}:
        strategy = "fixed"
    if strategy not in {"auto", "heading", "recursive", "fixed"}:
        strategy = "auto"

    chunk_size = _positive_int(raw.get("chunk_size") or raw.get("chunk_chars"), 1200)
    chunk_overlap = _non_negative_int(raw.get("chunk_overlap"), 0)
    if chunk_overlap >= chunk_size:
        chunk_overlap = max(0, chunk_size // 5)

    separators = raw.get("separators")
    if not isinstance(separators, list) or not separators:
        separators = list(DEFAULT_PROCESSING_CONFIG["chunking"]["separators"])
    separators = [str(separator) for separator in separators if str(separator)]

    return {
        **raw,
        "strategy": strategy,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "separators": separators,
    }


def _extract_processing_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    if isinstance(value.get("processing"), Mapping):
        return dict(value["processing"])
    if any(key in value for key in ("chunking", "extraction", "digest", "graph")):
        return dict(value)
    return {}


def _deep_merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return parsed if parsed > 0 else default


def _non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return parsed if parsed >= 0 else default
