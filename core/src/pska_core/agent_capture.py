from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pska_core.adapters.conversation import conversation_to_payload
from pska_core.enums import ReviewType, Visibility
from pska_core.ingest import IngestService
from pska_core.models import DEFAULT_TENANT_ID, ReviewItem, SourceItem, SourceRef
from pska_core.serde import to_jsonable
from pska_core.store import KnowledgeStore


@dataclass(frozen=True, slots=True)
class AgentCaptureResult:
    action: str
    explanation: str
    source_item: SourceItem | None = None
    review_item: ReviewItem | None = None
    policy: dict[str, Any] | None = None

    @property
    def source_item_id(self) -> str | None:
        return self.source_item.source_item_id if self.source_item else None

    @property
    def review_item_id(self) -> str | None:
        return self.review_item.review_item_id if self.review_item else None

    def __getattr__(self, name: str) -> Any:
        if self.source_item is not None:
            return getattr(self.source_item, name)
        raise AttributeError(name)


def capture_agent_conversation(
    store: KnowledgeStore,
    *,
    owner_user_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    purpose: str,
    prompt: str,
    answer: str,
    source_refs: list[dict[str, Any]] | None = None,
    trace_summary: dict[str, Any] | None = None,
    represented_user_id: str | None = None,
    title: str | None = None,
    source_channel: str = "pska_agent",
    conversation_id: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    sensitivity: str = "normal",
    retention_days: int | None = 90,
    require_source_refs: bool = True,
    review_on_policy_violation: bool = True,
) -> AgentCaptureResult:
    refs = _normalize_source_refs(source_refs or citations or [])
    represented = represented_user_id or owner_user_id
    dedupe_key = _capture_dedupe_key(
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        represented_user_id=represented,
        purpose=purpose,
        prompt=prompt,
        answer=answer,
        source_refs=refs,
    )
    existing = _existing_capture(store, dedupe_key=dedupe_key, owner_user_id=owner_user_id, tenant_id=tenant_id, source_channel=source_channel)
    policy = {
        "schema_version": "pska.agent_capture_policy.v1",
        "dedupe_key": dedupe_key,
        "retention_days": retention_days,
        "sensitivity": sensitivity,
        "require_source_refs": require_source_refs,
        "review_on_policy_violation": review_on_policy_violation,
    }
    if existing:
        return AgentCaptureResult(
            action="existing",
            explanation="duplicate capture skipped; existing source item returned",
            source_item=existing,
            policy={**policy, "decision": "dedupe_existing"},
        )
    violation = _policy_violation(refs=refs, sensitivity=sensitivity, require_source_refs=require_source_refs)
    if violation:
        if not review_on_policy_violation:
            return AgentCaptureResult(
                action="rejected",
                explanation=violation,
                policy={**policy, "decision": "rejected", "violation": violation},
            )
        review = _write_capture_review(
            store,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            represented_user_id=represented,
            purpose=purpose,
            prompt=prompt,
            answer=answer,
            refs=refs,
            trace_summary=trace_summary or {},
            sensitivity=sensitivity,
            violation=violation,
            dedupe_key=dedupe_key,
        )
        return AgentCaptureResult(
            action="review_required",
            explanation=violation,
            review_item=review,
            policy={**policy, "decision": "review_required", "violation": violation},
        )

    now = datetime.now(timezone.utc).isoformat()
    payload = conversation_to_payload(
        {
            "id": conversation_id or f"pska_agent_{purpose}_{uuid4().hex}",
            "source_channel": source_channel,
            "title": title or f"PSKA agent capture: {purpose}",
            "captured_at": now,
            "participants": [
                {"participant_id": represented_user_id or owner_user_id, "name": represented_user_id or owner_user_id},
                {"participant_id": "pska_agent", "name": "PSKA Agent"},
            ],
            "messages": [
                {
                    "id": "msg_user_prompt",
                    "role": "user",
                    "participant_id": represented_user_id or owner_user_id,
                    "content": prompt,
                    "created_at": now,
                },
                {
                    "id": "msg_agent_answer",
                    "role": "assistant",
                    "participant_id": "pska_agent",
                    "content": answer,
                    "created_at": now,
                    "citations": refs,
                },
            ],
            "citations": refs,
            "tool_calls": _safe_tool_calls(tool_calls or []),
            "trace_summary": trace_summary or {},
            "extra": {
                "purpose": purpose,
                "represented_user_id": represented,
                "source_refs": refs,
                "trace_summary": trace_summary or {},
                "capture_policy": policy,
                "capture_dedupe_key": dedupe_key,
                "retention": {
                    "retention_days": retention_days,
                    "captured_at": now,
                    "expires_at": _expires_at(now, retention_days),
                },
            },
        },
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        space_id="private_primary",
        visibility=Visibility.PRIVATE,
    )
    source = IngestService(store).ingest_channel_payload(payload)
    return AgentCaptureResult(
        action="saved",
        explanation="capture saved as source item",
        source_item=source,
        policy={**policy, "decision": "saved"},
    )


def _capture_dedupe_key(
    *,
    owner_user_id: str,
    tenant_id: str,
    represented_user_id: str,
    purpose: str,
    prompt: str,
    answer: str,
    source_refs: list[dict[str, Any]],
) -> str:
    payload = {
        "owner_user_id": owner_user_id,
        "tenant_id": tenant_id,
        "represented_user_id": represented_user_id,
        "purpose": purpose,
        "prompt": prompt.strip(),
        "answer": answer.strip(),
        "source_refs": source_refs,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _existing_capture(store: KnowledgeStore, *, dedupe_key: str, owner_user_id: str, tenant_id: str, source_channel: str) -> SourceItem | None:
    try:
        items = store.list_source_items(tenant_id=tenant_id)
    except Exception:
        return None
    for item in items:
        extra = item.metadata.get("extra") if isinstance(item.metadata, dict) else {}
        if (
            item.owner_user_id == owner_user_id
            and item.source_channel == source_channel
            and isinstance(extra, dict)
            and extra.get("capture_dedupe_key") == dedupe_key
        ):
            return item
    return None


def _policy_violation(*, refs: list[dict[str, Any]], sensitivity: str, require_source_refs: bool) -> str | None:
    normalized_sensitivity = sensitivity.strip().lower()
    if normalized_sensitivity in {"high", "sensitive", "secret"}:
        return "sensitive capture requires review before saving"
    if require_source_refs and not refs:
        return "capture requires source_refs before saving"
    return None


def _write_capture_review(
    store: KnowledgeStore,
    *,
    owner_user_id: str,
    tenant_id: str,
    represented_user_id: str,
    purpose: str,
    prompt: str,
    answer: str,
    refs: list[dict[str, Any]],
    trace_summary: dict[str, Any],
    sensitivity: str,
    violation: str,
    dedupe_key: str,
) -> ReviewItem:
    existing = _existing_capture_review(store, dedupe_key=dedupe_key, owner_user_id=owner_user_id, tenant_id=tenant_id)
    if existing:
        return existing
    review_type = ReviewType.SENSITIVE_CONTENT if sensitivity.strip().lower() in {"high", "sensitive", "secret"} else ReviewType.LOW_CONFIDENCE
    review = ReviewItem(
        review_item_id=f"rev_capture_{uuid5(NAMESPACE_URL, dedupe_key).hex}",
        owner_user_id=owner_user_id,
        review_type=review_type,
        title=f"Review agent capture: {purpose}",
        tenant_id=tenant_id,
        proposal={
            "candidate_type": "agent_capture",
            "purpose": purpose,
            "represented_user_id": represented_user_id,
            "prompt": prompt,
            "answer": answer,
            "source_refs": refs,
            "trace_summary": trace_summary,
            "sensitivity": sensitivity,
            "violation": violation,
            "capture_dedupe_key": dedupe_key,
            "recommended_action": "approve_capture_after_redaction_or_add_source_refs",
            "plain_text_summary": answer[:240] or prompt[:240] or f"Review captured agent conversation for {purpose}.",
        },
    )
    store.add_review_item(review)
    return review


def _existing_capture_review(store: KnowledgeStore, *, dedupe_key: str, owner_user_id: str, tenant_id: str) -> ReviewItem | None:
    try:
        items = store.list_review_items(tenant_id=tenant_id)
    except Exception:
        return None
    for item in items:
        proposal = item.proposal if isinstance(item.proposal, dict) else {}
        if item.owner_user_id == owner_user_id and proposal.get("capture_dedupe_key") == dedupe_key:
            return item
    return None


def _safe_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        safe.append(
            {
                key: value
                for key, value in to_jsonable(call).items()
                if key in {"tool", "name", "status", "source_refs", "started_at", "finished_at"}
            }
        )
    return safe


def _expires_at(captured_at: str, retention_days: int | None) -> str | None:
    if retention_days is None or retention_days <= 0:
        return None
    captured = datetime.fromisoformat(captured_at)
    return (captured + timedelta(days=retention_days)).isoformat()


def _normalize_source_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = set(SourceRef.__dataclass_fields__)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for ref in refs:
        data = to_jsonable(ref)
        if not isinstance(data, dict):
            continue
        value = {key: item for key, item in data.items() if key in allowed and item}
        marker = tuple(sorted(value.items()))
        if value and marker not in seen:
            seen.add(marker)
            normalized.append(value)
    return normalized
