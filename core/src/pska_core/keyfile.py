from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class APIKeyFile:
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    service_token: str = ""


def read_api_key_file(path: Path) -> APIKeyFile:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return APIKeyFile()
    if not text:
        return APIKeyFile()

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            return _from_mapping(data)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return APIKeyFile(
        api_key=lines[0] if len(lines) > 0 else "",
        model=lines[1] if len(lines) > 1 else "",
        base_url=lines[2] if len(lines) > 2 else "",
        service_token=lines[3] if len(lines) > 3 else "",
    )


def _from_mapping(data: dict[str, Any]) -> APIKeyFile:
    return APIKeyFile(
        api_key=str(data.get("api_key") or data.get("key") or "").strip(),
        model=str(data.get("model") or "").strip(),
        base_url=str(data.get("base_url") or data.get("api_base") or "").strip(),
        service_token=str(data.get("service_token") or data.get("fastreact_service_token") or "").strip(),
    )
