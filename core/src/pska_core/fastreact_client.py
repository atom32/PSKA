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
        tenant_id: str | None = None,
        purpose: str,
        stream: bool = False,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]: ...

    def create_run(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        tenant_id: str | None = None,
        purpose: str,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]: ...

    def wait_for_run(self, run_id: str) -> dict[str, Any]: ...

    def run_events(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FastreactConfig:
    url: str = "http://127.0.0.1:8000"
    service_token: str | None = None
    timeout_seconds: float = 30.0
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    authnode_url: str | None = None
    authnode_admin_token: str | None = None
    authnode_audience: str = "fastreact"
    authnode_token_ttl_seconds: int | None = None

    @classmethod
    def from_env(cls) -> "FastreactConfig":
        return cls(
            url=os.getenv("PSKA_FASTREACT_URL", "http://127.0.0.1:8000").rstrip("/"),
            service_token=os.getenv("PSKA_FASTREACT_SERVICE_TOKEN") or None,
            timeout_seconds=float(os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS", "30")),
            model=os.getenv("PSKA_FASTREACT_MODEL") or None,
            temperature=_optional_float_env("PSKA_FASTREACT_TEMPERATURE"),
            top_p=_optional_float_env("PSKA_FASTREACT_TOP_P"),
            max_tokens=_optional_int_env("PSKA_FASTREACT_MAX_TOKENS"),
            authnode_url=_optional_url_env("PSKA_FASTREACT_AUTHNODE_URL") or _optional_url_env("AUTHNODE_URL"),
            authnode_admin_token=os.getenv("PSKA_FASTREACT_AUTHNODE_ADMIN_TOKEN") or os.getenv("AUTHNODE_ADMIN_TOKEN") or None,
            authnode_audience=os.getenv("PSKA_FASTREACT_AUTHNODE_AUDIENCE", "fastreact"),
            authnode_token_ttl_seconds=_optional_int_env("PSKA_FASTREACT_AUTHNODE_TOKEN_TTL_SECONDS"),
        )


@dataclass(slots=True)
class HttpFastreactClient:
    config: FastreactConfig = field(default_factory=FastreactConfig.from_env)
    _run_authorizations: dict[str, str] = field(default_factory=dict, init=False, repr=False)

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
        tenant_id: str | None = None,
        purpose: str,
        stream: bool = False,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        metadata = _pska_metadata(user_id=user_id, tenant_id=tenant_id, purpose=purpose, job_id=job_id, scope=scope)
        payload = {
            "messages": messages,
            "stream": stream,
            "user_key": f"pska:{user_id}",
            "metadata": metadata,
        }
        payload.update(
            _generation_options_payload(
                self.config,
                model=model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
        )
        if session_id:
            payload["session_id"] = session_id
        if skills is not None:
            payload["skills"] = skills
        return self._request_json("POST", "/v1/chat/completions", payload)

    def create_run(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        tenant_id: str | None = None,
        purpose: str,
        job_id: str | None = None,
        scope: dict[str, Any] | None = None,
        session_id: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        metadata = _pska_metadata(user_id=user_id, tenant_id=tenant_id, purpose=purpose, job_id=job_id, scope=scope)
        payload = {
            "messages": messages,
            "stream": True,
            "user_key": f"pska:{user_id}",
            "metadata": metadata,
        }
        payload.update(
            _generation_options_payload(
                self.config,
                model=model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
            )
        )
        if session_id:
            payload["session_id"] = session_id
        if skills is not None:
            payload["skills"] = skills
        return self._request_json("POST", "/v1/runs", payload)

    def wait_for_run(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            snapshot = self._request_json("GET", f"/v1/runs/{run_id}", authorization=self._run_authorizations.get(run_id))
            if snapshot.get("status") in {"completed", "failed", "cancelled", "expired"}:
                return snapshot
            time.sleep(0.25)
        raise FastreactError(f"Fastreact /v1/runs/{run_id} timed out")

    def run_events(self, run_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/v1/runs/{run_id}/events?limit=500", authorization=self._run_authorizations.get(run_id))

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authorization: str | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers(payload is not None, payload=payload)
        if authorization and "Authorization" not in headers:
            headers["Authorization"] = authorization
        request = Request(
            f"{self.config.url}{path}",
            data=data,
            method=method,
            headers=headers,
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
        if method == "POST" and path == "/v1/runs":
            run_id = str(parsed.get("run_id") or "")
            bearer = headers.get("Authorization")
            if run_id and bearer:
                self._run_authorizations[run_id] = bearer
        return parsed

    def _headers(self, has_body: bool, *, payload: dict[str, Any] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"accept": "application/json"}
        if has_body:
            headers["content-type"] = "application/json; charset=utf-8"
        if self.config.service_token:
            headers["X-FastReAct-Service-Token"] = self.config.service_token
        authnode_token = self._authnode_token(payload)
        if authnode_token:
            headers["Authorization"] = f"Bearer {authnode_token}"
        if payload:
            user_key = payload.get("user_key")
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            tenant_key = metadata.get("tenant_key") or metadata.get("pska_tenant_id")
            if isinstance(user_key, str) and user_key.strip():
                headers["X-FastReAct-User-Key"] = user_key.strip()
            if isinstance(tenant_key, str) and tenant_key.strip():
                headers["X-FastReAct-Tenant-Key"] = tenant_key.strip()
            if user_key or tenant_key:
                headers["X-FastReAct-Auth-Provider"] = "pska"
        return headers

    def _authnode_token(self, payload: dict[str, Any] | None) -> str | None:
        authnode_url = (self.config.authnode_url or "").rstrip("/")
        if not authnode_url or not payload:
            return None
        user_key, tenant_key = _payload_identity(payload)
        if not user_key:
            raise FastreactError("Fastreact AuthNode token request requires payload.user_key")
        request_payload: dict[str, Any] = {
            "user_key": user_key,
            "audience": _authnode_audience_payload(self.config.authnode_audience),
        }
        if tenant_key:
            request_payload["tenant_id"] = tenant_key
        if self.config.authnode_token_ttl_seconds:
            request_payload["ttl_seconds"] = int(self.config.authnode_token_ttl_seconds)
        headers = {
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
        }
        if self.config.authnode_admin_token:
            headers["X-AuthNode-Admin-Token"] = self.config.authnode_admin_token
        request = Request(
            f"{authnode_url}/v1/token",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FastreactError(f"AuthNode token request failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise FastreactError(f"AuthNode token request unavailable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise FastreactError("AuthNode token request timed out") from exc
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise FastreactError("AuthNode token request returned invalid JSON") from exc
        token = parsed.get("access_token") if isinstance(parsed, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise FastreactError("AuthNode token request returned no access_token")
        return token.strip()


REQUIRED_PSKA_TOOLS = {"pska_search", "pska_index_status", "pska_job_context", "pska_write_candidates"}


def _pska_metadata(
    *,
    user_id: str,
    tenant_id: str | None,
    purpose: str,
    job_id: str | None,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = {
        "caller": "pska",
        "purpose": purpose,
        "pska_user_id": user_id,
        "pska_job_id": job_id,
        "scope": scope or {},
    }
    if tenant_id:
        metadata["tenant_key"] = tenant_id
        metadata["pska_tenant_id"] = tenant_id
    return metadata


def _payload_identity(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    user_key = payload.get("user_key")
    user = user_key.strip() if isinstance(user_key, str) and user_key.strip() else None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    tenant_key = metadata.get("tenant_key") or metadata.get("pska_tenant_id")
    tenant = tenant_key.strip() if isinstance(tenant_key, str) and tenant_key.strip() else None
    return user, tenant


def _authnode_audience_payload(value: str | None) -> str | list[str]:
    audience = str(value or "fastreact")
    parts = [item.strip() for item in audience.split(",") if item.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else "fastreact"
    return parts


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


def _optional_float_env(name: str) -> float | None:
    value = os.getenv(name)
    return float(value) if value not in {None, ""} else None


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value not in {None, ""} else None


def _optional_url_env(name: str) -> str | None:
    value = os.getenv(name)
    return value.rstrip("/") if value else None


def _generation_options_payload(
    config: FastreactConfig,
    *,
    model: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    effective_model = model if model is not None else config.model
    effective_temperature = temperature if temperature is not None else config.temperature
    effective_top_p = top_p if top_p is not None else config.top_p
    effective_max_tokens = max_tokens if max_tokens is not None else config.max_tokens
    if effective_model is not None:
        model_value = str(effective_model).strip()
        if not model_value:
            raise ValueError("Fastreact model must not be blank")
        payload["model"] = model_value
    if effective_temperature is not None:
        temperature_value = float(effective_temperature)
        if temperature_value < 0 or temperature_value > 2:
            raise ValueError("Fastreact temperature must be between 0 and 2")
        payload["temperature"] = temperature_value
    if effective_top_p is not None:
        top_p_value = float(effective_top_p)
        if top_p_value < 0 or top_p_value > 1:
            raise ValueError("Fastreact top_p must be between 0 and 1")
        payload["top_p"] = top_p_value
    if effective_max_tokens is not None:
        if isinstance(effective_max_tokens, bool):
            raise ValueError("Fastreact max_tokens must be greater than 0")
        max_tokens_value = int(effective_max_tokens)
        if max_tokens_value <= 0:
            raise ValueError("Fastreact max_tokens must be greater than 0")
        payload["max_tokens"] = max_tokens_value
    return payload
