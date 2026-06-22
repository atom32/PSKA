from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pska_core.config import LLMConfig
from pska_core.keyfile import read_api_key_file


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


@dataclass(slots=True)
class OpenAILLMClient:
    api_key: str
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "OpenAILLMClient":
        return cls.from_config(
            LLMConfig(
                api_key_file=Path(os.getenv("PSKA_LLM_API_KEY_FILE")).expanduser()
                if os.getenv("PSKA_LLM_API_KEY_FILE")
                else None,
                model=os.getenv("PSKA_LLM_MODEL") or None,
                base_url=os.getenv("PSKA_LLM_BASE_URL") or None,
                timeout_seconds=int(os.getenv("PSKA_LLM_TIMEOUT_SECONDS", "60")),
            )
        )

    @classmethod
    def from_config(cls, config: LLMConfig) -> "OpenAILLMClient":
        api_key = os.getenv("PSKA_LLM_API_KEY")
        file_model = ""
        file_base_url = ""
        if not api_key and config.api_key_file:
            key_config = read_api_key_file(config.api_key_file)
            api_key = key_config.api_key
            file_model = key_config.model
            file_base_url = key_config.base_url
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("FASTRACT_API_KEY")
        if not api_key:
            raise LLMConfigurationError(
                "LLM is required. Set PSKA_LLM_API_KEY, OPENAI_API_KEY, FASTRACT_API_KEY, "
                "or PSKA_LLM_API_KEY_FILE."
            )
        return cls(
            api_key=api_key,
            model=config.model or file_model or "gpt-4.1-mini",
            base_url=(config.base_url or file_base_url or "https://api.openai.com/v1").rstrip("/"),
            timeout_seconds=int(config.timeout_seconds or 60),
        )

    def complete_json(self, *, system: str, prompt: str, temperature: float = 0.0) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMResponseError(f"LLM HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - surface provider failures as explicit LLM errors.
            raise LLMResponseError(f"LLM request failed: {type(exc).__name__}: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception as exc:  # noqa: BLE001
            record_recovery_event("llm_json_repair", {"reason": type(exc).__name__, "model": self.model})
            parsed = self._repair_json_with_llm(system=system, invalid_content=str(body), temperature=temperature)
        if not isinstance(parsed, dict):
            raise LLMResponseError("LLM JSON response must be an object")
        return parsed

    def _repair_json_with_llm(self, *, system: str, invalid_content: str, temperature: float) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system}\nYou are now correcting an invalid JSON response. "
                        "Return only strict RFC 8259 JSON. No markdown, no comments, no trailing commas."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "The previous response was not valid JSON. Convert it into valid JSON without adding facts.\n\n"
                        f"Invalid response:\n{invalid_content[:20000]}"
                    ),
                },
            ],
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            return json.loads(content)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMResponseError(f"LLM JSON repair HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMResponseError("LLM returned invalid JSON and JSON correction failed") from exc
