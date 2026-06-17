from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pska_core.enums import Directionality, MemoryLayer, ReviewType, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.memory import MemoryService
from pska_core.models import AuditEvent, Entity, ReviewItem, SourceRef, SourceItem
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
        request_id = payload.get("request_id")
        schema_version = str(payload.get("schema_version") or SUPPORTED_CANDIDATE_SCHEMA_VERSION)
        warnings: list[str] = []
        if payload.get("schema_version") is None:
            warnings.append("schema_version missing; assumed pska.candidates.v1")
        if schema_version != SUPPORTED_CANDIDATE_SCHEMA_VERSION:
            raise CandidateWriteError(f"unsupported candidate schema_version: {schema_version}")
        source_refs = _source_refs(payload.get("source_refs"))
        source_items = self._source_items_for_refs(source_refs)
        if not source_refs:
            raise CandidateWriteError("candidate batch requires source_refs")
        if not source_items:
            raise CandidateWriteError("candidate batch source_refs must reference known source_items")
        self._assert_owner(owner_user_id, source_items)
        defaults = _ContextDefaults(owner_user_id=owner_user_id, source_refs=source_refs, source_items=source_items)

        summary = {
            "entities": [],
            "hyperedges": [],
            "review_items": [],
            "agent_memories": [],
            "profile_cards": [],
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

        self._audit(
            actor_user_id=str(payload.get("created_by_user_id") or "agent_service"),
            action="candidates.write",
            target_type="candidate_batch",
            target_id=str(request_id or job_id or uuid4().hex),
            decision="accepted",
            metadata={
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
            entity_id=str(spec.get("entity_id") or _stable_id("ent", defaults.owner_user_id, entity_type, label)),
            entity_type=entity_type,
            label=label,
            owner_user_id=defaults.owner_user_id,
            space_id=str(spec.get("space_id") or first.space_id),
            visibility=_visibility(spec.get("visibility"), first.visibility),
            visible_team_ids=list(spec.get("visible_team_ids") or first.visible_team_ids),
            metadata={
                **dict(spec.get("metadata") or {}),
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
            review_item = self._write_review_item(
                {
                    "review_type": ReviewType.RELATIONSHIP_CANDIDATE.value,
                    "title": str(spec.get("title") or f"Review relationship candidate: {relation_type}"),
                    "proposal": {
                        **spec,
                        "source_refs": [asdict(ref) for ref in source_refs],
                        "confidence": confidence,
                        "reason": "low_confidence_relationship_candidate",
                    },
                },
                defaults,
                producer=producer,
                job_id=job_id,
                request_id=request_id,
            )
            summary["review_items"].append(review_item.review_item_id)
            return None
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
        )

    def _write_review_item(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any) -> ReviewItem:
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        proposal = dict(spec.get("proposal") or {})
        proposal.setdefault("source_refs", [asdict(ref) for ref in source_refs])
        proposal.setdefault("producer", producer)
        proposal.setdefault("job_id", job_id)
        proposal.setdefault("request_id", request_id)
        review_item = ReviewItem(
            review_item_id=str(spec.get("review_item_id") or _stable_id("rev", defaults.owner_user_id, str(spec["review_type"]), str(spec["title"]), str(job_id or request_id or ""))),
            owner_user_id=defaults.owner_user_id,
            review_type=ReviewType(str(spec["review_type"])),
            title=str(spec["title"]),
            proposal=proposal,
        )
        self.store.add_review_item(review_item)
        return review_item

    def _write_memory_candidate(self, spec: dict[str, Any], defaults: "_ContextDefaults", *, producer: str, job_id: Any, request_id: Any):
        source_refs = _source_refs(spec.get("source_refs")) or defaults.source_refs
        kind = str(spec.get("kind") or spec.get("memory_type") or "agent_memory")
        confidence = float(spec.get("confidence", 0.75))
        sensitivity = str(spec.get("sensitivity") or "normal")
        if kind in {"profile", "profile_card", "profile_update"}:
            profile_delta = spec.get("profile_delta") or spec.get("profile")
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
        )

    def _source_items_for_refs(self, source_refs: list[SourceRef]) -> list[SourceItem]:
        requested = {ref.source_item_id for ref in source_refs if ref.source_item_id}
        return [item for item in self.store.list_source_items() if item.source_item_id in requested]

    def _assert_owner(self, owner_user_id: str, source_items: list[SourceItem]) -> None:
        mismatches = [item.source_item_id for item in source_items if item.owner_user_id != owner_user_id]
        if mismatches:
            raise CandidateWriteError(f"source_refs do not belong to owner_user_id {owner_user_id}: {mismatches}")

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
            )
        )


class _ContextDefaults:
    def __init__(self, *, owner_user_id: str, source_refs: list[SourceRef], source_items: list[SourceItem]) -> None:
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
