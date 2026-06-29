from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pska_core.enums import Directionality, MemoryLayer, ReviewType, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.memory import MemoryService
from pska_core.models import DEFAULT_TENANT_ID, AuditEvent, DigestNote, Entity, KnowledgeClaim, ReviewItem, SourceRef, SourceItem
from pska_core.store import KnowledgeStore


class CandidateWriteError(ValueError):
    """Raised when a candidate batch cannot be grounded safely."""


SUPPORTED_CANDIDATE_SCHEMA_VERSION = "pska.candidates.v1"
LOW_CONFIDENCE_REVIEW_THRESHOLD = 0.6


class CandidateWriteService:
    """Applies Fastreact-generated candidates through PSKA's storage and review boundary."""

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.graph = HypergraphService(store)
        self.memory = MemoryService(store)

    def write_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        owner_user_id = str(payload.get("owner_user_id") or payload.get("represented_user_id") or "user_primary")
        producer = str(payload.get("producer") or "fastreact")
        job_id = payload.get("job_id")
        tenant_id = str(payload.get("tenant_id") or self._tenant_id_for_job(job_id) or DEFAULT_TENANT_ID)
        request_id = payload.get("request_id")
        schema_version = str(payload.get("schema_version") or SUPPORTED_CANDIDATE_SCHEMA_VERSION)
        warnings: list[str] = []
        if payload.get("schema_version") is None:
            warnings.append("schema_version missing; assumed pska.candidates.v1")
        if schema_version != SUPPORTED_CANDIDATE_SCHEMA_VERSION:
            raise CandidateWriteError(f"unsupported candidate schema_version: {schema_version}")
        source_refs = _source_refs(payload.get("source_refs"))
        source_items = self._source_items_for_refs(source_refs, tenant_id=tenant_id)
        if not source_refs:
            raise CandidateWriteError("candidate batch requires source_refs")
        if not source_items:
            raise CandidateWriteError("candidate batch source_refs must reference known source_items")
        self._assert_owner(owner_user_id, source_items)
        self._assert_tenant(tenant_id, source_items)
        defaults = _ContextDefaults(tenant_id=tenant_id, owner_user_id=owner_user_id, source_refs=source_refs, source_items=source_items)
        digest_note_specs = [
            self._digest_note_from_spec(spec, defaults, producer=producer, job_id=job_id, request_id=request_id)
            for spec in _list_of_dicts(payload.get("digest_notes"))
        ]

        summary = {
            "entities": [],
            "hyperedges": [],
            "knowledge_claims": [],
            "digest_notes": [],
            "review_items": [],
            "agent_memories": [],
            "profile_cards": [],
            "saved_candidates": 0,
            "review_candidates": 0,
            "ignored_low_value_items": [],
            "schema_version": schema_version,
            "warnings": warnings,
        }
        entity_lookup: dict[str, Entity] = {}
        for spec in _list_of_dicts(payload.get("entities")):
            entity = self._write_entity(spec, defaults, producer=producer, job_id=job_id, request_id=request_id)
            entity_lookup[_entity_key(entity.entity_type, entity.label)] = entity
            summary["entities"].append(entity.entity_id)

        for spec in _list_of_dicts(payload.get("hyperedges")):
            edge = self._write_hyperedge(
                spec,
                defaults,
                entity_lookup=entity_lookup,
                summary=summary,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
            if edge is not None:
                summary["hyperedges"].append(edge.hyperedge_id)

        for spec in _list_of_dicts(payload.get("knowledge_claims")):
            result = self._write_knowledge_claim(
                spec,
                defaults,
                entity_lookup=entity_lookup,
                summary=summary,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
            if isinstance(result, ReviewItem):
                summary["review_items"].append(result.review_item_id)
            else:
                summary["knowledge_claims"].append(result.knowledge_claim_id)

        for note in digest_note_specs:
            note = self.store.add_digest_note(note)
            summary["digest_notes"].append(note.digest_note_id)

        for spec in _list_of_dicts(payload.get("review_items")):
            review_item = self._write_review_item(spec, defaults, producer=producer, job_id=job_id, request_id=request_id)
            summary["review_items"].append(review_item.review_item_id)

        for spec in _list_of_dicts(payload.get("memory_candidates")):
            result = self._write_memory_candidate(spec, defaults, producer=producer, job_id=job_id, request_id=request_id)
            if isinstance(result, ReviewItem):
                summary["review_items"].append(result.review_item_id)
            elif result.__class__.__name__ == "UserProfileCard":
                summary["profile_cards"].append(result.profile_card_id)
            else:
                summary["agent_memories"].append(result.agent_memory_id)

        summary["review_candidates"] = len(summary["review_items"])
        summary["saved_candidates"] = (
            len(summary["entities"])
            + len(summary["hyperedges"])
            + len(summary["knowledge_claims"])
            + len(summary["digest_notes"])
            + len(summary["agent_memories"])
            + len(summary["profile_cards"])
        )
        self._audit(
            actor_user_id=str(payload.get("created_by_user_id") or "agent_service"),
            action="candidates.write",
            target_type="candidate_batch",
            target_id=str(request_id or job_id or uuid4().hex),
            decision="accepted",
            metadata={
                "tenant_id": tenant_id,
                "producer": producer,
                "schema_version": schema_version,
                "job_id": job_id,
                "request_id": request_id,
                "summary": summary,
                "source_refs": [asdict(ref) for ref in source_refs],
            },
        )
        return summary

    def _write_entity(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any) -> Entity:
        entity_type = str(spec["entity_type"])
        label = str(spec["label"])
        first = defaults.source_items[0]
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        entity = Entity(
            entity_id=str(spec.get("entity_id") or _tenant_stable_id("ent", defaults.tenant_id, defaults.owner_user_id, entity_type, label)),
            entity_type=entity_type,
            label=label,
            owner_user_id=defaults.owner_user_id,
            space_id=str(spec.get("space_id") or first.space_id),
            visibility=_visibility(spec.get("visibility"), first.visibility),
            visible_team_ids=list(spec.get("visible_team_ids") or first.visible_team_ids),
            tenant_id=defaults.tenant_id,
            metadata={
                **_dict_or_empty(spec.get("metadata"), "entity.metadata"),
                "producer": producer,
                "job_id": job_id,
                "request_id": request_id,
                "source_refs": [asdict(ref) for ref in source_refs],
                "confidence": float(spec.get("confidence", 0.8)),
            },
        )
        self.store.add_entity(entity)
        return entity

    def _write_hyperedge(
        self,
        spec: dict[str, Any],
        defaults: "_ContextDefaults",
        *,
        entity_lookup: dict[str, Entity],
        summary: dict[str, list],
        producer: str,
        job_id: Any,
        request_id: Any,
    ):
        relation_type = str(spec["relation_type"])
        evidence_text = str(spec.get("evidence_text") or "")
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        confidence = float(spec.get("confidence", 0.75))
        if confidence < LOW_CONFIDENCE_REVIEW_THRESHOLD:
            plain = str(spec.get("plain_text_summary") or spec.get("why_it_matters") or evidence_text or f"候选关系 {relation_type} 置信度较低，需要人工确认。")
            review_item = self._write_review_item(
                {
                    "review_type": ReviewType.RELATIONSHIP_CANDIDATE.value,
                    "title": str(spec.get("title") or f"Review relationship candidate: {relation_type}"),
                    "proposal": {
                        **spec,
                        "source_refs": [asdict(ref) for ref in source_refs],
                        "confidence": confidence,
                        "reason": "low_confidence_relationship_candidate",
                        "plain_text_summary": plain,
                    },
                },
                defaults,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
            summary["review_items"].append(review_item.review_item_id)
            return None
        if not evidence_text and not str(spec.get("why_it_matters") or "").strip():
            raise CandidateWriteError("hyperedge candidate requires evidence_text or why_it_matters")
        members = []
        for member in _list_of_dicts(spec.get("members")):
            role = str(member.get("role") or "related")
            entity_id = member.get("entity_id")
            if not entity_id:
                entity_type = str(member["entity_type"])
                label = str(member["label"])
                entity = entity_lookup.get(_entity_key(entity_type, label))
                if entity is None:
                    entity = self._write_entity(
                        {"entity_type": entity_type, "label": label, "source_refs": [asdict(ref) for ref in source_refs]},
                        defaults,
                        producer=producer,
                        job_id=job_id,
                        request_id=request_id,
                    )
                    entity_lookup[_entity_key(entity_type, label)] = entity
                    summary["entities"].append(entity.entity_id)
                entity_id = entity.entity_id
            members.append((str(entity_id), role))
        if len(members) < 2:
            raise CandidateWriteError("hyperedge candidate requires at least two members")
        first = defaults.source_items[0]
        return self.graph.create_hyperedge(
            relation_type=relation_type,
            owner_user_id=defaults.owner_user_id,
            space_id=str(spec.get("space_id") or first.space_id),
            visibility=_visibility(spec.get("visibility"), first.visibility),
            visible_team_ids=list(spec.get("visible_team_ids") or first.visible_team_ids),
            directionality=Directionality(str(spec.get("directionality") or Directionality.AMBIGUOUS.value)),
            members=members,
            evidence_text=evidence_text,
            source_refs=source_refs,
            confidence=confidence,
            tenant_id=defaults.tenant_id,
        )

    def _write_review_item(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any) -> ReviewItem:
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        proposal = _dict_or_empty(spec.get("proposal"), "review_item.proposal")
        proposal.setdefault("source_refs", [asdict(ref) for ref in source_refs])
        proposal.setdefault("producer", producer)
        proposal.setdefault("job_id", job_id)
        proposal.setdefault("request_id", request_id)
        proposal.setdefault("plain_text_summary", _plain_text_summary(spec, proposal))
        review_item = ReviewItem(
            review_item_id=str(
                spec.get("review_item_id")
                or _tenant_stable_id(
                    "rev",
                    defaults.tenant_id,
                    defaults.owner_user_id,
                    str(spec["review_type"]),
                    str(spec["title"]),
                    str(job_id or request_id or ""),
                )
            ),
            owner_user_id=defaults.owner_user_id,
            review_type=ReviewType(str(spec["review_type"])),
            title=str(spec["title"]),
            proposal=proposal,
            tenant_id=defaults.tenant_id,
        )
        self.store.add_review_item(review_item)
        return review_item

    def _write_knowledge_claim(
        self,
        spec: dict[str, Any],
        defaults: "_ContextDefaults",
        *,
        entity_lookup: dict[str, Entity],
        summary: dict[str, list],
        producer: str,
        job_id: Any,
        request_id: Any,
    ) -> KnowledgeClaim | ReviewItem:
        statement = str(spec.get("statement") or "").strip()
        evidence_text = str(spec.get("evidence_text") or "").strip()
        if not statement:
            raise CandidateWriteError("knowledge_claim requires statement")
        if not evidence_text:
            raise CandidateWriteError("knowledge_claim requires evidence_text")
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        if not source_refs:
            raise CandidateWriteError("knowledge_claim requires source_refs")
        confidence = float(spec.get("confidence", 0.75))
        if confidence < LOW_CONFIDENCE_REVIEW_THRESHOLD:
            return self._write_review_item(
                {
                    "review_type": ReviewType.LOW_CONFIDENCE.value,
                    "title": str(spec.get("title") or "Review low-confidence knowledge claim"),
                    "proposal": {
                        "candidate": spec,
                        "statement": statement,
                        "evidence_text": evidence_text,
                        "source_refs": [asdict(ref) for ref in source_refs],
                        "confidence": confidence,
                        "reason": "low_confidence_knowledge_claim",
                        "plain_text_summary": statement,
                    },
                },
                defaults,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
        claim = KnowledgeClaim(
            knowledge_claim_id=str(
                spec.get("knowledge_claim_id")
                or _candidate_content_id("claim", defaults.tenant_id, defaults.owner_user_id, [statement], source_refs, spec)
            ),
            owner_user_id=defaults.owner_user_id,
            claim_type=str(spec.get("claim_type") or "fact"),
            statement=statement,
            subject=str(spec.get("subject")) if spec.get("subject") is not None else None,
            predicate=str(spec.get("predicate")) if spec.get("predicate") is not None else None,
            object=str(spec.get("object")) if spec.get("object") is not None else None,
            qualifiers=_dict_or_empty(spec.get("qualifiers"), "knowledge_claim.qualifiers"),
            evidence_text=evidence_text,
            source_refs=source_refs,
            confidence=confidence,
            producer=producer,
            job_id=str(job_id) if job_id else None,
            request_id=str(request_id) if request_id else None,
            metadata={
                **_dict_or_empty(spec.get("metadata"), "knowledge_claim.metadata"),
                **_dedupe_metadata(spec),
                "plain_text_summary": str(spec.get("plain_text_summary") or statement),
            },
            tenant_id=defaults.tenant_id,
        )
        stored = self.store.add_knowledge_claim(claim)
        self._derive_hyperedge_from_claim(stored, defaults, entity_lookup=entity_lookup, summary=summary, producer=producer, job_id=job_id, request_id=request_id)
        return stored

    def _derive_hyperedge_from_claim(
        self,
        claim: KnowledgeClaim,
        defaults: "_ContextDefaults",
        *,
        entity_lookup: dict[str, Entity],
        summary: dict[str, list],
        producer: str,
        job_id: Any,
        request_id: Any,
    ) -> None:
        if not (claim.subject and claim.predicate and claim.object):
            return
        edge = self._write_hyperedge(
            {
                "relation_type": claim.predicate,
                "members": [
                    {"entity_type": "claim_subject", "label": claim.subject, "role": "subject"},
                    {"entity_type": "claim_object", "label": claim.object, "role": "object"},
                ],
                "evidence_text": claim.evidence_text,
                "confidence": claim.confidence,
                "source_refs": [asdict(ref) for ref in claim.source_refs],
                "metadata": {"derived_from_knowledge_claim_id": claim.knowledge_claim_id},
            },
            defaults,
            entity_lookup=entity_lookup,
            summary=summary,
            producer=producer,
            job_id=job_id,
            request_id=request_id,
        )
        if edge is not None:
            summary["hyperedges"].append(edge.hyperedge_id)

    def _write_digest_note(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any) -> DigestNote:
        return self.store.add_digest_note(self._digest_note_from_spec(spec, defaults, producer=producer, job_id=job_id, request_id=request_id))

    def _digest_note_from_spec(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any) -> DigestNote:
        title = _digest_note_title(spec)
        synopsis = _digest_note_synopsis(spec, fallback_title=title)
        if not title:
            raise CandidateWriteError("digest_note requires title")
        if not synopsis:
            raise CandidateWriteError("digest_note requires synopsis")
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        if not source_refs:
            raise CandidateWriteError("digest_note requires source_refs")
        note = DigestNote(
            digest_note_id=str(
                spec.get("digest_note_id")
                or _candidate_content_id("dig", defaults.tenant_id, defaults.owner_user_id, [title, synopsis], source_refs, spec)
            ),
            owner_user_id=defaults.owner_user_id,
            title=title,
            synopsis=synopsis,
            key_points=_list_of_dicts(spec.get("key_points")),
            actions=_list_of_dicts(spec.get("actions")),
            open_questions=_list_of_dicts(spec.get("open_questions") or spec.get("questions")),
            risks=_list_of_dicts(spec.get("risks")),
            memory_suggestions=_list_of_dicts(spec.get("memory_suggestions")),
            relationship_suggestions=_list_of_dicts(spec.get("relationship_suggestions")),
            source_refs=source_refs,
            confidence=float(spec.get("confidence", 0.75)),
            producer=producer,
            job_id=str(job_id) if job_id else None,
            request_id=str(request_id) if request_id else None,
            metadata={**_dict_or_empty(spec.get("metadata"), "digest_note.metadata"), **_dedupe_metadata(spec)},
            tenant_id=defaults.tenant_id,
        )
        _assert_digest_note_items_are_grounded(note)
        return self.store.add_digest_note(note)

    def _write_memory_candidate(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any):
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        kind = str(spec.get("kind") or spec.get("memory_type") or "agent_memory")
        confidence = float(spec.get("confidence", 0.75))
        sensitivity = str(spec.get("sensitivity") or "normal")
        if kind in {"profile", "profile_card", "profile_update"}:
            profile_delta = spec.get("profile_delta") if spec.get("profile_delta") is not None else spec.get("profile")
            if not isinstance(profile_delta, dict) or not profile_delta:
                raise CandidateWriteError("profile memory candidate requires profile_delta")
            return self.memory.propose_profile_update(
                owner_user_id=defaults.owner_user_id,
                profile_delta={
                    **profile_delta,
                    "_producer": producer,
                    "_job_id": job_id,
                    "_request_id": request_id,
                },
                source_refs=source_refs,
                sensitivity=sensitivity,
                confidence=confidence,
                tenant_id=defaults.tenant_id,
            )
        text = str(spec.get("text") or "")
        if not text:
            raise CandidateWriteError("agent memory candidate requires text")
        if sensitivity in {"high", "sensitive"}:
            return self._write_review_item(
                {
                    "review_type": ReviewType.PROFILE_UPDATE.value,
                    "title": str(spec.get("title") or "Sensitive memory candidate requires review"),
                    "proposal": {
                        "profile_delta": {"memory_candidate": text, "layer": str(spec.get("layer") or MemoryLayer.SEMANTIC.value)},
                        "source_refs": [asdict(ref) for ref in source_refs],
                        "confidence": confidence,
                    },
                },
                defaults,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
        if confidence < LOW_CONFIDENCE_REVIEW_THRESHOLD:
            return self._write_review_item(
                {
                    "review_type": ReviewType.LOW_CONFIDENCE.value,
                    "title": str(spec.get("title") or "Review low-confidence memory candidate"),
                    "proposal": {
                        "memory_candidate": text,
                        "layer": str(spec.get("layer") or MemoryLayer.SEMANTIC.value),
                        "source_refs": [asdict(ref) for ref in source_refs],
                        "confidence": confidence,
                        "reason": "low_confidence_memory_candidate",
                    },
                },
                defaults,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
        return self.memory.write_agent_memory(
            owner_user_id=defaults.owner_user_id,
            layer=MemoryLayer(str(spec.get("layer") or MemoryLayer.SEMANTIC.value)),
            text=text,
            confidence=confidence,
            source_refs=source_refs,
            created_by_user_id=str(spec.get("created_by_user_id") or "agent_service"),
            decay_policy=str(spec.get("decay_policy") or "manual"),
            tenant_id=defaults.tenant_id,
        )

    def _source_items_for_refs(self, source_refs: list[SourceRef], *, tenant_id: str) -> list[SourceItem]:
        requested = {ref.source_item_id for ref in source_refs if ref.source_item_id}
        return [item for item in self.store.list_source_items(tenant_id=tenant_id) if item.source_item_id in requested]

    def _assert_owner(self, owner_user_id: str, source_items: list[SourceItem]) -> None:
        mismatches = [item.source_item_id for item in source_items if item.owner_user_id != owner_user_id]
        if mismatches:
            raise CandidateWriteError(f"source_refs do not belong to owner_user_id {owner_user_id}: {mismatches}")

    def _assert_tenant(self, tenant_id: str, source_items: list[SourceItem]) -> None:
        mismatches = [item.source_item_id for item in source_items if item.tenant_id != tenant_id]
        if mismatches:
            raise CandidateWriteError(f"source_refs do not belong to tenant_id {tenant_id}: {mismatches}")

    def _tenant_id_for_job(self, job_id: Any) -> str | None:
        if not job_id:
            return None
        try:
            return self.store.get_job(str(job_id)).tenant_id
        except Exception:
            return None

    def _audit(self, *, actor_user_id: str, action: str, target_type: str, target_id: str, decision: str, metadata: dict[str, Any]) -> None:
        self.store.add_audit_event(
            AuditEvent(
                audit_event_id=f"aud_{uuid4().hex}",
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                decision=decision,
                metadata=metadata,
                tenant_id=str(metadata.get("tenant_id") or DEFAULT_TENANT_ID),
            )
        )


class _ContextDefaults:
    def __init__(self, *, tenant_id: str, owner_user_id: str, source_refs: list[SourceRef], source_items: list[SourceItem]) -> None:
        self.tenant_id = tenant_id
        self.owner_user_id = owner_user_id
        self.source_refs = source_refs
        self.source_items = source_items


def _source_refs(value: Any) -> list[SourceRef]:
    if not isinstance(value, list):
        return []
    allowed = set(SourceRef.__dataclass_fields__)
    return [SourceRef(**{key: item for key, item in ref.items() if key in allowed}) for ref in value if isinstance(ref, dict)]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_or_empty(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise CandidateWriteError(f"{field_name} must be an object")


def _plain_text_summary(spec: dict[str, Any], proposal: dict[str, Any]) -> str:
    for value in (
        proposal.get("plain_text_summary"),
        spec.get("plain_text_summary"),
        proposal.get("statement"),
        proposal.get("summary"),
        proposal.get("memory_candidate"),
        proposal.get("text"),
        proposal.get("evidence_text"),
        spec.get("title"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "候选结果需要人工确认。"


def _digest_note_title(spec: dict[str, Any]) -> str:
    for key in ("title", "heading", "name", "label"):
        text = str(spec.get(key) or "").strip()
        if text:
            return text
    synopsis = str(spec.get("synopsis") or spec.get("summary") or "").strip()
    if synopsis:
        return synopsis[:120]
    return _first_digest_item_text(spec)[:120]


def _digest_note_synopsis(spec: dict[str, Any], *, fallback_title: str) -> str:
    for key in ("synopsis", "summary", "description", "text", "body"):
        text = str(spec.get(key) or "").strip()
        if text:
            return text
    item_text = _first_digest_item_text(spec)
    if item_text:
        return item_text
    return fallback_title


def _first_digest_item_text(spec: dict[str, Any]) -> str:
    for group_name in ("key_points", "actions", "open_questions", "questions", "risks", "memory_suggestions", "relationship_suggestions"):
        for item in _list_of_dicts(spec.get(group_name)):
            text = _digest_item_readable_text(item)
            if text:
                return text
    return ""


def _assert_digest_note_items_are_grounded(note: DigestNote) -> None:
    groups = {
        "key_points": note.key_points,
        "actions": note.actions,
        "open_questions": note.open_questions,
        "risks": note.risks,
        "memory_suggestions": note.memory_suggestions,
        "relationship_suggestions": note.relationship_suggestions,
    }
    for group_name, items in groups.items():
        for item in items:
            refs = _source_refs(item.get("source_refs")) or note.source_refs
            if not refs:
                raise CandidateWriteError(f"digest_note.{group_name} items require source_refs")
            item.setdefault("source_refs", [asdict(ref) for ref in refs])
            readable = _digest_item_readable_text(item)
            if not readable:
                raise CandidateWriteError(f"digest_note.{group_name} items require readable text")
            item.setdefault("summary", readable)


def _digest_item_readable_text(item: dict[str, Any]) -> str:
    for key in (
        "summary",
        "statement",
        "title",
        "text",
        "point",
        "question",
        "action",
        "risk",
        "issue",
        "description",
        "why_it_matters",
    ):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    return ""


def _visibility(value: Any, default: Visibility) -> Visibility:
    if isinstance(value, Visibility):
        return value
    if isinstance(value, str):
        return Visibility(value)
    return default


def _entity_key(entity_type: str, label: str) -> str:
    return f"{entity_type.strip().lower()}::{label.strip().lower()}"


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{uuid5(NAMESPACE_URL, '|'.join(parts)).hex}"


def _tenant_identity_parts(tenant_id: str, *parts: str) -> tuple[str, ...]:
    if tenant_id == DEFAULT_TENANT_ID:
        return tuple(parts)
    return (tenant_id, *parts)


def _tenant_stable_id(prefix: str, tenant_id: str, *parts: str) -> str:
    return _stable_id(prefix, *_tenant_identity_parts(tenant_id, *parts))


def _content_stable_id(prefix: str, tenant_id: str, owner_user_id: str, text_parts: list[str], source_refs: list[SourceRef]) -> str:
    return _tenant_stable_id(prefix, tenant_id, owner_user_id, *[_normalized_identity_text(part) for part in text_parts], *_source_ref_identity_parts(source_refs))


def _candidate_content_id(prefix: str, tenant_id: str, owner_user_id: str, text_parts: list[str], source_refs: list[SourceRef], spec: dict[str, Any]) -> str:
    dedupe_key = _candidate_dedupe_key(spec)
    if dedupe_key:
        return _content_stable_id(prefix, tenant_id, owner_user_id, [f"dedupe:{dedupe_key}"], source_refs)
    return _content_stable_id(prefix, tenant_id, owner_user_id, text_parts, source_refs)


def _candidate_dedupe_key(spec: dict[str, Any]) -> str:
    metadata = spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}
    for key in ("dedupe_key", "identity_key", "semantic_key", "canonical_key"):
        value = spec.get(key)
        if value is None:
            value = metadata.get(key)
        text = _normalized_identity_text(str(value or ""))
        if text:
            return text
    return ""


def _dedupe_metadata(spec: dict[str, Any]) -> dict[str, str]:
    dedupe_key = _candidate_dedupe_key(spec)
    return {"dedupe_key": dedupe_key} if dedupe_key else {}


def _normalized_identity_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _source_ref_identity_parts(source_refs: list[SourceRef]) -> list[str]:
    parts: set[str] = set()
    for ref in source_refs:
        if ref.source_item_id:
            parts.add(f"source:{ref.source_item_id}")
        elif ref.document_id:
            parts.add(f"document:{ref.document_id}")
        elif ref.passage_window_id:
            parts.add(f"passage:{ref.passage_window_id}")
        elif ref.chunk_id:
            parts.add(f"chunk:{ref.chunk_id}")
        elif ref.url:
            parts.add(f"url:{ref.url}")
        elif ref.path:
            parts.add(f"path:{ref.path}")
        elif ref.message_id:
            parts.add(f"message:{ref.message_id}")
    return sorted(parts) or ["source:unknown"]
