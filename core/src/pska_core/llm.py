from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMClient(Protocol):
    def complete_json(self, *, system: str, prompt: str, temperature: float = 0.0) -> dict[str, Any]:
        ...


def record_recovery_event(kind: str, detail: dict[str, Any]) -> None:
    path = os.getenv("PSKA_LLM_RECOVERY_LOG")
    if not path:
        return
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "detail": detail,
    }
    try:
        with Path(path).expanduser().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
