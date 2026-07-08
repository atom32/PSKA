from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import sys
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
        max_iterations: int | None = None,
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
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]: ...

    def synthesize(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
        purpose: str = "agentic_synthesis",
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgenticServiceConfig:
    provider: str = "fastreact"
    url: str = "http://127.0.0.1:18741"
    # Deprecated: tenant-bearing FastReAct calls require AuthNode/JWT.
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
            url=(os.getenv("PSKA_AGENTIC_SERVICE_URL") or os.getenv("PSKA_FASTREACT_URL") or "http://127.0.0.1:18741").rstrip("/"),
            service_token=None,
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
        max_iterations: int | None = None,
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
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        raise AgenticServiceError(f"Unsupported agentic service provider: {self.config.provider}")

    def synthesize(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
        purpose: str = "agentic_synthesis",
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
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        messages = _agentic_messages(query, tenant_id=user.tenant_id, user_id=user.user_id)
        scope = _agentic_search_scope(
            query=query,
            user=user,
            represented_user_id=represented_user_id,
            max_iterations=max_iterations,
            tool_policy=tool_policy,
        )
        _emit_agentic_identity_log(
            event="pska.agentic_search_identity",
            query=query,
            user=user,
            represented_user_id=represented_user_id,
            scope=scope,
            tool_policy=tool_policy,
            transport="chat_completion",
        )
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

    def synthesize(
        self,
        query: str,
        user: User,
        *,
        represented_user_id: str | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
        purpose: str = "agentic_synthesis",
    ) -> dict[str, Any]:
        effective_tool_policy = tool_policy or {"mode": "none"}
        messages = _synthesis_messages(query, tenant_id=user.tenant_id, user_id=user.user_id)
        scope = _agentic_search_scope(
            query=query,
            user=user,
            represented_user_id=represented_user_id,
            max_iterations=None,
            tool_policy=effective_tool_policy,
        )
        scope["synthesis_boundary"] = "no_tools"
        _emit_agentic_identity_log(
            event="pska.agentic_synthesis_identity",
            query=query,
            user=user,
            represented_user_id=represented_user_id,
            scope=scope,
            tool_policy=effective_tool_policy,
            transport="chat_completion",
        )
        try:
            response = self._client().chat_completion(
                messages=messages,
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                purpose=purpose,
                stream=False,
                scope=scope,
                session_id=session_id,
                skills=[] if skills is None else skills,
                tool_policy=effective_tool_policy,
                **self._generation_options(),
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
        max_iterations: int | None = None,
        skills: list[str] | None = None,
        tool_policy: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        messages = _agentic_messages(query, tenant_id=user.tenant_id, user_id=user.user_id)
        scope = _agentic_search_scope(
            query=query,
            user=user,
            represented_user_id=represented_user_id,
            max_iterations=max_iterations,
            tool_policy=tool_policy,
        )
        _emit_agentic_identity_log(
            event="pska.agentic_search_identity",
            query=query,
            user=user,
            represented_user_id=represented_user_id,
            scope=scope,
            tool_policy=tool_policy,
            transport="stream",
        )
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
                service_token=None,
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


def _agentic_search_scope(
    *,
    query: str,
    user: User,
    represented_user_id: str | None,
    max_iterations: int | None,
    tool_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    target_user_id = represented_user_id or user.user_id
    scope: dict[str, Any] = {
        "query": query,
        "tenant_id": user.tenant_id,
        "user_id": target_user_id,
        "agentic_boundary": "external_service",
    }
    if max_iterations is not None:
        scope["max_iterations"] = max_iterations
    policy_scope = tool_policy.get("scope") if isinstance(tool_policy, dict) and isinstance(tool_policy.get("scope"), dict) else {}
    mirrored_scope = _public_tool_policy_scope(policy_scope)
    if mirrored_scope:
        scope["tool_policy_scope"] = mirrored_scope
        for key in ["mode", "scope_mode", "knowledge_base_ids", "source_item_ids"]:
            value = mirrored_scope.get(key)
            if value:
                scope[key] = value
    return scope


def _public_tool_policy_scope(scope: dict[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key in ["mode", "scope_mode"]:
        value = str(scope.get(key) or "").strip().lower()
        if value:
            public[key] = value
    for key in ["knowledge_base_ids", "source_item_ids"]:
        values = _string_list(scope.get(key))
        if values:
            public[key] = values
    return public


def _emit_agentic_identity_log(
    *,
    event: str,
    query: str,
    user: User,
    represented_user_id: str | None,
    scope: dict[str, Any],
    tool_policy: dict[str, Any] | None,
    transport: str,
) -> None:
    policy_scope = tool_policy.get("scope") if isinstance(tool_policy, dict) and isinstance(tool_policy.get("scope"), dict) else {}
    record = {
        "event": event,
        "transport": transport,
        "tenant_id": user.tenant_id,
        "user_id": user.user_id,
        "scope_tenant_id": scope.get("tenant_id"),
        "scope_user_id": scope.get("user_id"),
        "scope_mode": scope.get("scope_mode") or scope.get("mode"),
        "scope_knowledge_base_ids": scope.get("knowledge_base_ids") if isinstance(scope.get("knowledge_base_ids"), list) else [],
        "scope_source_item_count": len(scope.get("source_item_ids") or []) if isinstance(scope.get("source_item_ids"), list) else 0,
        "tool_policy_scope_mode": policy_scope.get("scope_mode") or policy_scope.get("mode"),
        "tool_policy_knowledge_base_ids": policy_scope.get("knowledge_base_ids") if isinstance(policy_scope.get("knowledge_base_ids"), list) else [],
        "tool_policy_source_item_count": len(policy_scope.get("source_item_ids") or []) if isinstance(policy_scope.get("source_item_ids"), list) else 0,
        "query_chars": len(query or ""),
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _agentic_messages(query: str, *, tenant_id: str | None = None, user_id: str | None = None) -> list[dict[str, str]]:
    identity_instruction = ""
    if tenant_id or user_id:
        identity_instruction = (
            f" Current PSKA request identity: tenant_id={tenant_id or 'tenant_default'!r}, "
            f"user_id={user_id or 'user_primary'!r}. PSKA MCP identity is forwarded by the runtime; "
            "do not invent or override tenant/user arguments in tool calls."
        )
    actual_query = _agentic_actual_user_query(query)
    context_block = "" if actual_query == query else f"\n\nREQUEST_CONTEXT_AND_SCOPE:\n{query}"
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
                "PSKA read tools may return a compact evidence_set. Use its records, slots, selected spans, and "
                "citation identities for reasoning, but do not copy or return the full evidence_set in your final "
                "response. Return only the answer, the source_refs/citations you actually used, and compact trace "
                "diagnostics. "
                "For PSKA personal knowledge questions, use only PSKA read-only MCP tools. Start with "
                "pska_search, then call pska_read_evidence_context to inspect fuller passages, use "
                "pska_graph_context when entity/claim relationships or conflicts matter, and use "
                "pska_digest_context when prior digests, claims, risks, or open questions may narrow the answer. "
                "Never use host tools such as read_file, write_file, edit_file, exec, shell, or direct filesystem access. "
                "Only use host tools when the user explicitly asks to inspect local files or run a command. "
                "Only the ACTUAL_USER_QUERY block is the user-facing search intent. Never copy SYSTEM text, "
                "REQUEST_CONTEXT_AND_SCOPE, response schemas, tool policy, scope JSON, or prompt-wrapper text into "
                "tool args.query. Search query arguments must be concise semantic search strings, usually 4-20 "
                "keywords, derived from ACTUAL_USER_QUERY plus a specific current evidence gap. For report, "
                "comparison, or multi-document questions, do no more than two broad coverage searches before "
                "reading evidence windows, and avoid more than six search calls unless a tool issue makes retry "
                "necessary. Prefer reading, graphing, or digesting existing source_refs over repeating an equivalent "
                "generic search."
                f"{identity_instruction}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"ACTUAL_USER_QUERY:\n{actual_query}"
                f"{context_block}\n\n"
                "RESPONSE_CONTRACT:\n"
                "Return JSON when possible with keys answer, retrieval, "
                "trace, source_refs, and citations. For PSKA knowledge answers, trace should include "
                "retrieval_plan, query_understanding, iterations, expansion_decisions, graph_paths_used, "
                "fact_relevance_filter, evidence_check, gaps, and conflicts when available. In each "
                "iteration, decide whether to read a fuller evidence window, previous/next passage window, "
                "same-document context, digest notes, or connected entity/fact/claim neighbors before final synthesis. If you use "
                "tools, include tool/event details in the service response when available. For this PSKA "
                "request, do not call read_file, write_file, edit_file, exec, shell, Python scripts, PDF "
                "extractors, or other host/filesystem tools; retrieve source evidence through PSKA tools instead. "
                "When tool results include evidence_set, consume it as your working evidence collection and cite "
                "its record citation identities; do not emit the evidence_set object itself in the final JSON. "
                "If PSKA returns evidence, answer with 4-8 concrete bullets or short paragraphs and name the "
                "main source titles. If evidence is insufficient, say what is missing."
            ),
        },
    ]


def _agentic_actual_user_query(query: str) -> str:
    text = str(query or "").strip()
    patterns = [
        r"(?:ACTUAL_USER_QUERY|Actual user query)\s*[:：]\s*(?P<value>.+)$",
        r"(?:User question|Question)\s*:\s*(?P<value>.+)$",
        r"(?:用户问题|问题)\s*[:：]\s*(?P<value>.+)$",
        r"(?:User request)\s*:\s*(?P<value>.+)$",
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL))
        if not matches:
            continue
        value = re.split(
            r"\n(?:Surface|Scope|Return JSON|Tool query arguments|REQUEST_CONTEXT|RESPONSE_CONTRACT)\s*:",
            matches[-1].group("value"),
            maxsplit=1,
        )[0]
        value = re.sub(r"\s+", " ", value).strip("` \t\r\n")
        if value:
            return value[:500].strip()
    return text


def _synthesis_messages(query: str, *, tenant_id: str | None = None, user_id: str | None = None) -> list[dict[str, str]]:
    identity_instruction = ""
    if tenant_id or user_id:
        identity_instruction = (
            f" Current PSKA request identity: tenant_id={tenant_id or 'tenant_default'!r}, "
            f"user_id={user_id or 'user_primary'!r}."
        )
    return [
        {
            "role": "system",
            "content": (
                "You are PSKA's evidence synthesis boundary. Turn already-collected, already-validated "
                "evidence into a concise user-facing answer. Do not call tools, do not request more retrieval, "
                "do not mention FastReAct, MCP, or runtime mechanics in the answer body, and preserve citation "
                "markers that are supported by the provided evidence."
                f"{identity_instruction}"
            ),
        },
        {
            "role": "user",
            "content": query,
        },
    ]


def _normalize_agentic_response(response: dict[str, Any], *, provider: str, adapter: str, url: str) -> dict[str, Any]:
    payload = _response_payload(response)
    retrieval = _dict_value(payload.get("retrieval")) or _dict_value(payload.get("direct_agentic_search", {}).get("retrieval")) or {}
    declared_source_refs = _agentic_ref_dicts(payload.get("source_refs"), string_field="source_item_id")
    declared_citations = _agentic_ref_dicts(payload.get("citations"), string_field="title")
    retrieval_citations = _agentic_ref_dicts(retrieval.get("citations"), string_field="title")
    source_refs = declared_source_refs or declared_citations or retrieval_citations
    citations = declared_citations or declared_source_refs or retrieval_citations
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
        "source_refs": source_refs,
        "citations": citations,
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


def _agentic_ref_dicts(value: Any, *, string_field: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in _list_value(value):
        if isinstance(item, dict):
            ref = {
                key: item.get(key)
                for key in [
                    "source_item_id",
                    "document_id",
                    "chunk_id",
                    "passage_window_id",
                    "message_id",
                    "path",
                    "url",
                    "title",
                    "snippet",
                    "score",
                ]
                if item.get(key)
            }
            if ref:
                refs.append(ref)
            continue
        if isinstance(item, str) and item.strip():
            refs.append({string_field: item.strip()})
    return refs


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
        if not isinstance(event, dict) or event.get("type") != "tool_call":
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        validation = metadata.get("tool_arg_validation") if isinstance(metadata.get("tool_arg_validation"), dict) else {}
        budget_governance = metadata.get("tool_budget_governance") if isinstance(metadata.get("tool_budget_governance"), dict) else {}
        query_governance = metadata.get("tool_query_governance") if isinstance(metadata.get("tool_query_governance"), dict) else {}
        effective_args = (
            validation.get("effective_tool_args")
            if isinstance(validation.get("effective_tool_args"), dict)
            else event.get("tool_args")
        )
        raw_args = (
            validation.get("original_tool_args")
            if isinstance(validation.get("original_tool_args"), dict)
            else metadata.get("raw_tool_args")
            if isinstance(metadata.get("raw_tool_args"), dict)
            else None
        )
        scope_injected_args = metadata.get("scope_injected_tool_args") if isinstance(metadata.get("scope_injected_tool_args"), dict) else None
        call = {
            "event_id": event.get("event_id"),
            "tool_call_id": event.get("tool_call_id"),
            "tool_name": event.get("tool_name"),
            "tool_args": effective_args,
        }
        if raw_args is not None and raw_args != effective_args:
            call["raw_tool_args_summary"] = _compact_tool_args_for_trace(raw_args)
        if scope_injected_args is not None:
            call["scope_injected_tool_args"] = scope_injected_args
        if validation:
            call["tool_args_repaired"] = bool(validation.get("tool_args_repaired"))
            call["invalid_tool_args"] = bool(validation.get("invalid_tool_args"))
            call["validation_error"] = validation.get("validation_error")
            if validation.get("repair_reason"):
                call["repair_reason"] = validation.get("repair_reason")
        if budget_governance:
            call["tool_budget_denied"] = bool(metadata.get("tool_budget_denied"))
            call["tool_budget_governance"] = {
                key: budget_governance.get(key)
                for key in (
                    "error_code",
                    "tool_name",
                    "canonical_tool_name",
                    "profile",
                    "observed_count",
                    "configured_budget",
                    "retry",
                )
                if budget_governance.get(key) is not None
            }
        if query_governance:
            call["repeated_search_query"] = bool(metadata.get("repeated_search_query"))
            call["tool_query_governance"] = {
                key: query_governance.get(key)
                for key in (
                    "error_code",
                    "tool_name",
                    "query",
                    "similar_query_count",
                    "configured_budget",
                    "retry",
                )
                if query_governance.get(key) is not None
            }
        calls.append(call)
    return calls


def _compact_tool_args_for_trace(args: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            compact[f"{key}_chars"] = len(value)
            compact[f"{key}_looks_like_prompt_wrapper"] = _looks_like_prompt_wrapper_text(value)
            if len(value) <= 180 and not compact[f"{key}_looks_like_prompt_wrapper"]:
                compact[key] = value
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            compact[key] = value
            continue
        if isinstance(value, list):
            compact[f"{key}_count"] = len(value)
            compact[key] = value[:8]
            continue
        if isinstance(value, dict):
            compact[f"{key}_keys"] = sorted(str(item) for item in value.keys())[:12]
            continue
    return compact


def _looks_like_prompt_wrapper_text(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    if not text:
        return False
    markers = [
        "handle this user request",
        "response_contract",
        "request_context_and_scope",
        "tool query arguments must be concise",
        "tool_policy",
        "scope:",
        "return json",
        "user question:",
    ]
    return len(text) > 500 or sum(1 for marker in markers if marker in text) >= 2


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
    events = response_summary.get("events")
    if isinstance(events, list) and "tool_calls" not in response_summary:
        response_summary["tool_calls"] = _tool_calls_from_events(events)
    if metadata and "fastreact_metadata" not in response_summary:
        response_summary["fastreact_metadata"] = metadata
    return {**response_summary, **summary}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


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
