from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from typing import Any, Protocol

from pska_core.fastreact_client import FastreactConfig, FastreactError, HttpFastreactClient
from pska_core.models import User


class AgenticServiceError(RuntimeError):
    """Raised when the configured external agentic service cannot satisfy a request."""


class AgenticServiceClient(Protocol):
    def ready(self) -> dict[str, Any]: ...

    def search(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgenticServiceConfig:
    provider: str = "fastreact"
    url: str = "http://127.0.0.1:8000"
    service_token: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "AgenticServiceConfig":
        provider = os.getenv("PSKA_AGENTIC_SERVICE_PROVIDER") or os.getenv("PSKA_AGENTIC_PROVIDER") or "fastreact"
        return cls(
            provider=provider,
            url=(os.getenv("PSKA_AGENTIC_SERVICE_URL") or os.getenv("PSKA_FASTREACT_URL") or "http://127.0.0.1:8000").rstrip("/"),
            service_token=os.getenv("PSKA_AGENTIC_SERVICE_TOKEN") or os.getenv("PSKA_FASTREACT_SERVICE_TOKEN") or None,
            timeout_seconds=float(os.getenv("PSKA_AGENTIC_SERVICE_TIMEOUT_SECONDS") or os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS") or "30"),
        )


def build_agentic_service_client(config: AgenticServiceConfig | None = None) -> AgenticServiceClient:
    config = config or AgenticServiceConfig.from_env()
    if config.provider != "fastreact":
        return UnsupportedAgenticService(config)
    return FastreactAgenticServiceAdapter(config)


@dataclass(slots=True)
class UnsupportedAgenticService:
    config: AgenticServiceConfig

    def ready(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.config.provider,
            "url": self.config.url,
            "error": f"Unsupported agentic service provider: {self.config.provider}",
        }

    def search(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
    ) -> dict[str, Any]:
        raise AgenticServiceError(f"Unsupported agentic service provider: {self.config.provider}")


@dataclass(slots=True)
class FastreactAgenticServiceAdapter:
    """Adapter from PSKA's generic agentic-service boundary to FastReAct."""

    config: AgenticServiceConfig = field(default_factory=AgenticServiceConfig.from_env)
    client: HttpFastreactClient | None = None

    def ready(self) -> dict[str, Any]:
        try:
            ready = self._client().ready()
        except FastreactError as exc:
            return {
                "ok": False,
                "provider": self.config.provider,
                "adapter": "fastreact",
                "url": self.config.url,
                "error": str(exc),
            }
        return {
            **ready,
            "provider": self.config.provider,
            "adapter": "fastreact",
            "url": self.config.url,
        }

    def search(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
    ) -> dict[str, Any]:
        messages = _agentic_messages(query)
        scope = {
            "query": query,
            "represented_user_id": represented_user_id,
            "max_iterations": max_iterations,
            "agentic_boundary": "external_service",
        }
        try:
            response = self._run_agentic_search(messages=messages, user_id=user.user_id, scope=scope)
        except FastreactError as exc:
            raise AgenticServiceError(str(exc)) from exc
        return _normalize_agentic_response(
            response,
            provider=self.config.provider,
            adapter="fastreact",
            url=self.config.url,
        )

    def _run_agentic_search(self, *, messages: list[dict[str, str]], user_id: str, scope: dict[str, Any]) -> dict[str, Any]:
        client = self._client()
        try:
            created = client.create_run(
                messages=messages,
                user_id=user_id,
                purpose="agentic_search",
                scope=scope,
            )
            run_id = str(created.get("run_id") or "")
            if not run_id:
                raise FastreactError("Fastreact /v1/runs response missing run_id")
            snapshot = client.wait_for_run(run_id)
            events_payload = client.run_events(run_id)
            events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
            return {
                "type": "run",
                "run_id": run_id,
                "session_id": snapshot.get("session_id") or created.get("session_id"),
                "status": snapshot.get("status"),
                "content": _final_answer_from_events(events),
                "events": events,
                "tool_calls": _tool_calls_from_events(events),
                "metadata": {
                    **(snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}),
                    "event_count": events_payload.get("total_event_count") or events_payload.get("event_count") or len(events),
                    "run_protocol": "background_runs",
                },
            }
        except (AttributeError, FastreactError) as exc:
            if isinstance(exc, AttributeError):
                return client.chat_completion(
                    messages=messages,
                    user_id=user_id,
                    purpose="agentic_search",
                    stream=False,
                    scope=scope,
                )
            detail = str(exc)
            if "POST /v1/runs failed with HTTP 404" not in detail and "POST /v1/runs failed with HTTP 405" not in detail:
                raise
        return client.chat_completion(
            messages=messages,
            user_id=user_id,
            purpose="agentic_search",
            stream=False,
            scope=scope,
        )

    def _client(self) -> HttpFastreactClient:
        if self.client is not None:
            return self.client
        return HttpFastreactClient(
            FastreactConfig(
                url=self.config.url,
                service_token=self.config.service_token,
                timeout_seconds=self.config.timeout_seconds,
            )
        )


def _agentic_messages(query: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the configured external agentic service for PSKA. Handle the user's request "
                "with the tools available to FastReAct. Use PSKA tools when the request needs personal "
                "knowledge retrieval, respect ACL boundaries, cite evidence for PSKA knowledge answers, "
                "and otherwise use the appropriate non-PSKA tools. When the user explicitly asks to use "
                "bash or run a command, call the exec tool and answer from stdout/stderr."
            ),
        },
        {
            "role": "user",
            "content": (
                "Handle this user request. Return JSON when possible with keys answer, retrieval, "
                "trace, source_refs, and citations. If you use tools, include tool/event details in "
                "the service response when available.\n\n"
                f"User request: {query}"
            ),
        },
    ]


def _normalize_agentic_response(response: dict[str, Any], *, provider: str, adapter: str, url: str) -> dict[str, Any]:
    payload = _response_payload(response)
    retrieval = _dict_value(payload.get("retrieval")) or _dict_value(payload.get("direct_agentic_search", {}).get("retrieval")) or {}
    citations = _list_value(payload.get("citations")) or _list_value(payload.get("source_refs")) or _list_value(retrieval.get("citations"))
    if citations and not retrieval.get("citations"):
        retrieval = {**retrieval, "citations": citations}
    events_answer = _final_answer_from_events(_list_value(response.get("events")))
    return {
        "answer": events_answer or _response_text(payload) or _response_text(response),
        "retrieval": retrieval,
        "trace": _trace_summary(payload, response),
        "source_refs": citations,
        "raw_response": response,
        "agentic_service": {
            "provider": provider,
            "adapter": adapter,
            "url": url,
            "run_id": response.get("run_id"),
            "session_id": response.get("session_id"),
        },
    }


def _response_payload(response: dict[str, Any]) -> dict[str, Any]:
    for key in ["payload", "result", "agentic_search", "direct_agentic_search"]:
        value = response.get(key)
        if isinstance(value, dict):
            return value
    text = _response_text(response)
    if text:
        parsed = _json_object_from_text(text)
        if parsed is not None:
            return parsed
    return response


def _response_text(response: dict[str, Any]) -> str:
    for key in ["answer", "content", "text", "message", "final_answer"]:
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"].strip()
            if isinstance(first.get("text"), str):
                return first["text"].strip()
    return ""


def _final_answer_from_events(events: list[Any]) -> str:
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "session_end":
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            for key in ["final", "final_content", "answer"]:
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _tool_calls_from_events(events: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, dict) and event.get("type") == "tool_call":
            calls.append(
                {
                    "event_id": event.get("event_id"),
                    "tool_call_id": event.get("tool_call_id"),
                    "tool_name": event.get("tool_name"),
                    "tool_args": event.get("tool_args"),
                }
            )
    return calls


def _json_object_from_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    for candidate in _json_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    candidates.extend(match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


def _trace_summary(payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("trace")
    summary = trace if isinstance(trace, dict) else {}
    response_summary = {
        key: response[key]
        for key in ["run_id", "session_id", "model", "usage", "tool_calls", "events"]
        if key in response
    }
    metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
    if "event_count" in response:
        response_summary["event_count"] = response["event_count"]
    elif "event_count" in metadata:
        response_summary["event_count"] = metadata["event_count"]
    if metadata and "fastreact_metadata" not in response_summary:
        response_summary["fastreact_metadata"] = metadata
    return {**response_summary, **summary}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
