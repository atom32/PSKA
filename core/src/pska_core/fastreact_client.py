from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FastreactError(RuntimeError):
    """Raised when the Fastreact service cannot satisfy a PSKA request."""


class FastreactClient(Protocol):
    def ready(self) -> dict[str, Any]: ...

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        purpose: str,
        stream: bool = False,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...

    def create_run(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        purpose: str,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...

    def wait_for_run(self, run_id: str) -> dict[str, Any]: ...

    def run_events(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FastreactConfig:
    url: str = "http://127.0.0.1:8000"
    service_token: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "FastreactConfig":
        return cls(
            url=os.getenv("PSKA_FASTREACT_URL", "http://127.0.0.1:8000").rstrip("/"),
            service_token=os.getenv("PSKA_FASTREACT_SERVICE_TOKEN") or None,
            timeout_seconds=float(os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS", "30")),
        )


@dataclass(slots=True)
class HttpFastreactClient:
    config: FastreactConfig = field(default_factory=FastreactConfig.from_env)

    def ready(self) -> dict[str, Any]:
        health = self._request_json("GET", "/health")
        ready = self._request_json("GET", "/ready")
        tools: dict[str, Any]
        try:
            tools = self._request_json("GET", "/v1/tools")
        except FastreactError as exc:
            tools = {"ok": False, "error": str(exc)}
        tool_names = _tool_names(tools)
        normalized_tool_names = _normalized_pska_tool_names(tool_names)
        missing_pska_tools = sorted(REQUIRED_PSKA_TOOLS.difference(normalized_tool_names))
        return {
            "ok": True,
            "url": self.config.url,
            "health": health,
            "ready": ready,
            "tools": tools,
            "tool_names": sorted(tool_names),
            "normalized_pska_tool_names": sorted(normalized_tool_names),
            "required_pska_tools": sorted(REQUIRED_PSKA_TOOLS),
            "missing_pska_tools": missing_pska_tools,
            "pska_tools_loaded": not missing_pska_tools,
        }

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        purpose: str,
        stream: bool = False,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "messages": messages,
            "stream": stream,
            "user_key": f"pska:{user_id}",
            "metadata": {
                "caller": "pska",
                "purpose": purpose,
                "pska_user_id": user_id,
                "pska_job_id": job_id,
                "scope": scope or {},
            },
        }
        if session_id:
            payload["session_id"] = session_id
        return self._request_json("POST", "/v1/chat/completions", payload)

    def create_run(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        purpose: str,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "messages": messages,
            "stream": True,
            "user_key": f"pska:{user_id}",
            "metadata": {
                "caller": "pska",
                "purpose": purpose,
                "pska_user_id": user_id,
                "pska_job_id": job_id,
                "scope": scope or {},
            },
        }
        if session_id:
            payload["session_id"] = session_id
        return self._request_json("POST", "/v1/runs", payload)

    def wait_for_run(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            snapshot = self._request_json("GET", f"/v1/runs/{run_id}")
            if snapshot.get("status") in {"completed", "failed", "cancelled", "expired"}:
                return snapshot
            time.sleep(0.25)
        raise FastreactError(f"Fastreact /v1/runs/{run_id} timed out")

    def run_events(self, run_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/v1/runs/{run_id}/events?limit=500")

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.config.url}{path}",
            data=data,
            method=method,
            headers=self._headers(payload is not None),
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FastreactError(f"Fastreact {method} {path} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise FastreactError(f"Fastreact {method} {path} unavailable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise FastreactError(f"Fastreact {method} {path} timed out") from exc
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise FastreactError(f"Fastreact {method} {path} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise FastreactError(f"Fastreact {method} {path} returned non-object JSON")
        return parsed

    def _headers(self, has_body: bool) -> dict[str, str]:
        headers: dict[str, str] = {"accept": "application/json"}
        if has_body:
            headers["content-type"] = "application/json; charset=utf-8"
        if self.config.service_token:
            headers["X-FastReAct-Service-Token"] = self.config.service_token
        return headers


REQUIRED_PSKA_TOOLS = {"pska_search", "pska_index_status", "pska_job_context", "pska_write_candidates"}


def _pska_tools_loaded(tools_payload: dict[str, Any]) -> bool:
    return REQUIRED_PSKA_TOOLS.issubset(_normalized_pska_tool_names(_tool_names(tools_payload)))


def _tool_names(tools_payload: dict[str, Any]) -> set[str]:
    raw_tools = tools_payload.get("tools") if isinstance(tools_payload, dict) else None
    if not isinstance(raw_tools, list):
        return set()
    names = set()
    for tool in raw_tools:
        if isinstance(tool, str):
            names.add(tool)
        elif isinstance(tool, dict) and tool.get("name"):
            names.add(str(tool["name"]))
    return names


def _normalized_pska_tool_names(tool_names: set[str]) -> set[str]:
    normalized: set[str] = set()
    for name in tool_names:
        if name.startswith("pska_"):
            normalized.add(name)
        if name.startswith("pska_pska_"):
            normalized.add(name.removeprefix("pska_"))
    return normalized
