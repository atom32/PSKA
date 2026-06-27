from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from typing import Any, Iterator, Protocol

from pska_core.fastreact_client import FastreactConfig, FastreactError, HttpFastreactClient
from pska_core.models import User


PSKA_QA_SKILL = "pska_answer_with_citations"


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
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...

    def search_event_stream(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class AgenticServiceConfig:
    provider: str = "fastreact"
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
    def from_env(cls) -> "AgenticServiceConfig":
        provider = os.getenv("PSKA_AGENTIC_SERVICE_PROVIDER") or os.getenv("PSKA_AGENTIC_PROVIDER") or "fastreact"
        return cls(
            provider=provider,
            url=(os.getenv("PSKA_AGENTIC_SERVICE_URL") or os.getenv("PSKA_FASTREACT_URL") or "http://127.0.0.1:8000").rstrip("/"),
            service_token=os.getenv("PSKA_AGENTIC_SERVICE_TOKEN") or os.getenv("PSKA_FASTREACT_SERVICE_TOKEN") or None,
            timeout_seconds=float(os.getenv("PSKA_AGENTIC_SERVICE_TIMEOUT_SECONDS") or os.getenv("PSKA_FASTREACT_TIMEOUT_SECONDS") or "30"),
            model=os.getenv("PSKA_AGENTIC_SERVICE_MODEL") or os.getenv("PSKA_FASTREACT_MODEL") or None,
            temperature=_optional_float_env("PSKA_AGENTIC_SERVICE_TEMPERATURE", fallback_name="PSKA_FASTREACT_TEMPERATURE"),
            top_p=_optional_float_env("PSKA_AGENTIC_SERVICE_TOP_P", fallback_name="PSKA_FASTREACT_TOP_P"),
            max_tokens=_optional_int_env("PSKA_AGENTIC_SERVICE_MAX_TOKENS", fallback_name="PSKA_FASTREACT_MAX_TOKENS"),
            authnode_url=(
                os.getenv("PSKA_AGENTIC_SERVICE_AUTHNODE_URL")
                or os.getenv("PSKA_FASTREACT_AUTHNODE_URL")
                or os.getenv("AUTHNODE_URL")
                or None
            ),
            authnode_admin_token=(
                os.getenv("PSKA_AGENTIC_SERVICE_AUTHNODE_ADMIN_TOKEN")
                or os.getenv("PSKA_FASTREACT_AUTHNODE_ADMIN_TOKEN")
                or os.getenv("AUTHNODE_ADMIN_TOKEN")
                or None
            ),
            authnode_audience=os.getenv("PSKA_AGENTIC_SERVICE_AUTHNODE_AUDIENCE")
            or os.getenv("PSKA_FASTREACT_AUTHNODE_AUDIENCE")
            or "fastreact",
            authnode_token_ttl_seconds=_optional_int_env(
                "PSKA_AGENTIC_SERVICE_AUTHNODE_TOKEN_TTL_SECONDS",
                fallback_name="PSKA_FASTREACT_AUTHNODE_TOKEN_TTL_SECONDS",
            ),
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
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        raise AgenticServiceError(f"Unsupported agentic service provider: {self.config.provider}")

    def search_event_stream(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
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
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        messages = _agentic_messages(query, tenant_id=user.tenant_id, user_id=user.user_id)
        scope = {
            "query": query,
            "tenant_id": user.tenant_id,
            "represented_user_id": represented_user_id,
            "max_iterations": max_iterations,
            "agentic_boundary": "external_service",
        }
        run_skills = [PSKA_QA_SKILL] if skills is None else skills
        try:
            response = self._run_agentic_search(
                messages=messages,
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                scope=scope,
                skills=run_skills,
                tool_policy=tool_policy,
                session_id=session_id,
            )
        except FastreactError as exc:
            raise AgenticServiceError(str(exc)) from exc
        return _normalize_agentic_response(
            response,
            provider=self.config.provider,
            adapter="fastreact",
            url=self.config.url,
        )

    def search_event_stream(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        max_iterations: int = 3,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        messages = _agentic_messages(query, tenant_id=user.tenant_id, user_id=user.user_id)
        scope = {
            "query": query,
            "tenant_id": user.tenant_id,
            "represented_user_id": represented_user_id,
            "max_iterations": max_iterations,
            "agentic_boundary": "external_service",
        }
        run_skills = [PSKA_QA_SKILL] if skills is None else skills
        try:
            yield from self._client().chat_completion_stream(
                messages=messages,
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                purpose="agentic_search",
                scope=scope,
                session_id=session_id,
                skills=run_skills,
                tool_policy=tool_policy,
                **self._generation_options(),
            )
        except FastreactError as exc:
            raise AgenticServiceError(str(exc)) from exc

    def _run_agentic_search(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str,
        tenant_id: str,
        scope: dict[str, Any],
        skills: list[str],
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        client = self._client()
        try:
            created = client.create_run(
                messages=messages,
                user_id=user_id,
                tenant_id=tenant_id,
                purpose="agentic_search",
                scope=scope,
                session_id=session_id,
                skills=skills,
                tool_policy=tool_policy,
                **self._generation_options(),
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
                    tenant_id=tenant_id,
                    purpose="agentic_search",
                    stream=False,
                    scope=scope,
                    session_id=session_id,
                    skills=skills,
                    tool_policy=tool_policy,
                    **self._generation_options(),
                )
            detail = str(exc)
            if "POST /v1/runs failed with HTTP 404" not in detail and "POST /v1/runs failed with HTTP 405" not in detail:
                raise
        return client.chat_completion(
            messages=messages,
            user_id=user_id,
            tenant_id=tenant_id,
            purpose="agentic_search",
            stream=False,
            scope=scope,
            session_id=session_id,
            skills=skills,
            tool_policy=tool_policy,
            **self._generation_options(),
        )

    def _client(self) -> HttpFastreactClient:
        if self.client is not None:
            return self.client
        return HttpFastreactClient(
            FastreactConfig(
                url=self.config.url,
                service_token=self.config.service_token,
                timeout_seconds=self.config.timeout_seconds,
                model=self.config.model,
                temperature=self.config.temperature,
                top_p=self.config.top_p,
                max_tokens=self.config.max_tokens,
                authnode_url=self.config.authnode_url.rstrip("/") if self.config.authnode_url else None,
                authnode_admin_token=self.config.authnode_admin_token,
                authnode_audience=self.config.authnode_audience,
                authnode_token_ttl_seconds=self.config.authnode_token_ttl_seconds,
            )
        )

    def _generation_options(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "model": self.config.model,
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
                "max_tokens": self.config.max_tokens,
            }.items()
            if value is not None
        }


def _agentic_messages(query: str, *, tenant_id: str | None = None, user_id: str | None = None) -> list[dict[str, str]]:
    identity_instruction = ""
    if tenant_id or user_id:
        identity_instruction = (
            f" Current PSKA request identity: tenant_id={tenant_id or 'tenant_default'!r}, "
            f"user_id={user_id or 'user_primary'!r}. Every PSKA MCP tool call must include exactly "
            "these tenant_id and user_id arguments; never use PSKA tool defaults or a different tenant/user."
        )
    return [
        {
            "role": "system",
            "content": (
                "You are the configured external agentic service for PSKA. Handle the user's request "
                "with the tools available to FastReAct. Use PSKA tools when the request needs personal "
                "knowledge retrieval, respect ACL boundaries, cite evidence for PSKA knowledge answers, "
                "and otherwise use the appropriate non-PSKA tools. For PSKA GraphRAG questions, follow a "
                "HippoRAG-style loop: understand the query, retrieve lexical/vector passage seeds, inspect "
                "entity/fact/claim graph paths, judge whether adjacent passages or graph neighbors are needed, "
                "optionally issue follow-up PSKA searches, filter irrelevant facts, then synthesize a cited answer. "
                "When PSKA returns relevant evidence, provide a specific answer in the user's language with "
                "key facts, relationships, caveats, and source titles; do not collapse grounded answers into a "
                "single generic sentence. "
                "If the user request includes a JSON payload with deterministic_seeds, treat that payload's query "
                "field as the only user question and answer from the provided supporting passages, facts, graph "
                "paths, citations, and source refs first. Do not replace it with an unrelated query. Do not call "
                "PSKA tools for broad discovery when those seeds already answer the question; use tools only for "
                "a specific missing citation or evidence gap. If a PSKA tool fails or times out, still answer from "
                "the provided deterministic seeds and record the tool issue in trace.gaps. "
                "For PSKA personal knowledge questions, use only PSKA MCP tools such as pska_search and never "
                "use host tools such as read_file, write_file, edit_file, exec, shell, or direct filesystem access. "
                "Only use host tools when the user explicitly asks to inspect local files or run a command."
                f"{identity_instruction}"
            ),
        },
        {
            "role": "user",
            "content": (
                "Handle this user request. Return JSON when possible with keys answer, retrieval, "
                "trace, source_refs, and citations. For PSKA knowledge answers, trace should include "
                "retrieval_plan, query_understanding, iterations, expansion_decisions, graph_paths_used, "
                "fact_relevance_filter, evidence_check, gaps, and conflicts when available. In each "
                "iteration, decide whether to search the previous/next passage window, same-document "
                "neighbors, or connected entity/fact/claim neighbors before final synthesis. If you use "
                "tools, include tool/event details in the service response when available. For this PSKA "
                "request, do not call read_file, write_file, edit_file, exec, shell, Python scripts, PDF "
                "extractors, or other host/filesystem tools; retrieve source evidence through PSKA tools instead. "
                "If PSKA returns evidence, answer with 4-8 concrete bullets or short paragraphs and name the "
                "main source titles. If evidence is insufficient, say what is missing.\n\n"
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
    raw_answer = events_answer or _response_text(response)
    payload_answer = _answer_from_jsonish_text(raw_answer)
    if not payload_answer and events_answer:
        payload_answer = events_answer
    if not payload_answer and payload is not response:
        payload_answer = _response_text(payload)
    if not payload_answer:
        payload_answer = _response_text(payload)
    return {
        "answer": payload_answer or raw_answer,
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


def normalize_agentic_event_response(
    events: list[dict[str, Any]],
    *,
    provider: str = "fastreact",
    adapter: str = "fastreact",
    url: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    first_event = next((event for event in events if isinstance(event, dict)), {})
    response = {
        "type": "chat.completion",
        "run_id": first_event.get("run_id"),
        "session_id": first_event.get("session_id"),
        "content": _final_answer_from_events(events),
        "events": events,
        "tool_calls": _tool_calls_from_events(events),
        "metadata": {
            **(metadata or {}),
            "event_count": len(events),
            "run_protocol": "chat_completion_stream",
        },
    }
    return _normalize_agentic_response(response, provider=provider, adapter=adapter, url=url)


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
    for key in ["answer", "content", "final_content", "text", "message", "final_answer"]:
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    trace = response.get("trace")
    if isinstance(trace, dict):
        for key in ["final_content", "content", "answer"]:
            value = trace.get(key)
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
            for key in ["content", "final_content", "answer"]:
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
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


def _answer_from_jsonish_text(text: str) -> str:
    if not text:
        return ""
    for candidate in _json_candidates(text):
        answer = _jsonish_string_field(candidate, "answer")
        if answer:
            return answer
    return ""


def _jsonish_string_field(text: str, field: str) -> str:
    pattern = rf'"{re.escape(field)}"\s*:\s*"(?P<value>.*?)"\s*,\s*"(?:retrieval|trace|source_refs|citations|service_response)"\s*:'
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        match = re.search(rf'"{re.escape(field)}"\s*:\s*"(?P<value>.*?)"\s*[}}\n]', text, flags=re.DOTALL)
    if not match:
        return ""
    value = match.group("value").strip()
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\n", "\n").replace('\\"', '"').strip()


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


def _optional_float_env(name: str, *, fallback_name: str | None = None) -> float | None:
    value = os.getenv(name)
    if value in {None, ""} and fallback_name:
        value = os.getenv(fallback_name)
    return float(value) if value not in {None, ""} else None


def _optional_int_env(name: str, *, fallback_name: str | None = None) -> int | None:
    value = os.getenv(name)
    if value in {None, ""} and fallback_name:
        value = os.getenv(fallback_name)
    return int(value) if value not in {None, ""} else None
