from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceCompositionContext:
    query: str
    query_terms: tuple[str, ...] = ()
    anchor_terms: tuple[str, ...] = ()
    max_records: int = 8


@dataclass(frozen=True, slots=True)
class EvidenceSlot:
    name: str
    slot_type: str
    value: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    record_id: str
    source_type: str
    citation: dict[str, Any]
    text: str
    selected_span: str
    rank: int
    score: float
    slots: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceCompositionValidation:
    name: str
    passed: bool
    reason: str = ""
    missing_slots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    evidence_set_id: str
    status: str
    records: list[dict[str, Any]]
    slots: list[dict[str, Any]]
    missing_slots: list[str]
    conflicts: list[str]
    audit: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EvidenceCompositionResult:
    evidence_set: EvidenceSet
    audit: dict[str, Any]


class EvidenceSlotExtractor:
    name = "base"

    def extract(self, context: EvidenceCompositionContext) -> list[EvidenceSlot]:
        raise NotImplementedError


class TemporalSlotExtractor(EvidenceSlotExtractor):
    name = "temporal_slots"

    def extract(self, context: EvidenceCompositionContext) -> list[EvidenceSlot]:
        slots: list[EvidenceSlot] = []
        for value in _ordered_unique(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", context.query)):
            slots.append(EvidenceSlot(name=f"year:{value}", slot_type="temporal", value=value))
        quarter_pattern = (
            r"(?:Q[1-4]|q[1-4]|"
            r"第[一二三四1234]季度|"
            r"[一二三四1234]季度|"
            r"first quarter|second quarter|third quarter|fourth quarter)"
        )
        for value in _ordered_unique(match.group(0) for match in re.finditer(quarter_pattern, context.query, re.IGNORECASE)):
            slots.append(EvidenceSlot(name=f"period:{_normalize_slot_value(value)}", slot_type="temporal", value=value))
        return slots


class VersionSlotExtractor(EvidenceSlotExtractor):
    name = "version_slots"

    def extract(self, context: EvidenceCompositionContext) -> list[EvidenceSlot]:
        slots: list[EvidenceSlot] = []
        version_pattern = r"\b(?:v|version|版本)\s*[\w.-]+\b"
        for value in _ordered_unique(match.group(0) for match in re.finditer(version_pattern, context.query, re.IGNORECASE)):
            slots.append(EvidenceSlot(name=f"version:{_normalize_slot_value(value)}", slot_type="version", value=value))
        if re.search(r"\b(latest|newest|most recent|current)\b|最新|最近|当前版本", context.query, re.IGNORECASE):
            slots.append(EvidenceSlot(name="recency:latest", slot_type="recency", value="latest", required=False))
        return slots


class EvidenceCompositionValidator:
    name = "base"

    def validate(
        self,
        records: list[EvidenceRecord],
        slots: list[EvidenceSlot],
        coverage: dict[str, list[str]],
        context: EvidenceCompositionContext,
    ) -> EvidenceCompositionValidation:
        raise NotImplementedError


class NonEmptyEvidenceSetValidator(EvidenceCompositionValidator):
    name = "non_empty_evidence_set"

    def validate(
        self,
        records: list[EvidenceRecord],
        slots: list[EvidenceSlot],
        coverage: dict[str, list[str]],
        context: EvidenceCompositionContext,
    ) -> EvidenceCompositionValidation:
        passed = bool(records)
        return EvidenceCompositionValidation(self.name, passed, "" if passed else "empty_evidence_set")


class RequiredSlotCoverageValidator(EvidenceCompositionValidator):
    name = "required_slot_coverage"

    def validate(
        self,
        records: list[EvidenceRecord],
        slots: list[EvidenceSlot],
        coverage: dict[str, list[str]],
        context: EvidenceCompositionContext,
    ) -> EvidenceCompositionValidation:
        missing = tuple(slot.name for slot in slots if slot.required and not coverage.get(slot.name))
        return EvidenceCompositionValidation(
            self.name,
            not missing,
            "" if not missing else "missing_required_slots",
            missing_slots=missing,
        )


class CitationCoverageValidator(EvidenceCompositionValidator):
    name = "citation_coverage"

    def validate(
        self,
        records: list[EvidenceRecord],
        slots: list[EvidenceSlot],
        coverage: dict[str, list[str]],
        context: EvidenceCompositionContext,
    ) -> EvidenceCompositionValidation:
        missing = tuple(record.record_id for record in records if not _record_has_citation_identity(record.citation))
        return EvidenceCompositionValidation(
            self.name,
            not missing,
            "" if not missing else "missing_citation_identity",
            missing_slots=missing,
        )


class EvidenceCompositionPipeline:
    name = "evidence_composition"

    def __init__(
        self,
        *,
        slot_extractors: list[EvidenceSlotExtractor] | None = None,
        validators: list[EvidenceCompositionValidator] | None = None,
    ) -> None:
        self.slot_extractors = slot_extractors or [TemporalSlotExtractor(), VersionSlotExtractor()]
        self.validators = validators or [
            NonEmptyEvidenceSetValidator(),
            RequiredSlotCoverageValidator(),
            CitationCoverageValidator(),
        ]

    def compose(
        self,
        citations: list[dict[str, Any]],
        context: EvidenceCompositionContext,
        *,
        graph_paths: list[dict[str, Any]] | None = None,
    ) -> EvidenceCompositionResult:
        slots = self._extract_slots(context)
        records = self._records(citations, context)
        graph_records = self._graph_records(graph_paths or [], start_rank=len(records) + 1, max_records=context.max_records)
        records = [*records, *graph_records][: max(1, int(context.max_records or 1))]
        coverage = self._coverage(records, slots)
        validations = [validator.validate(records, slots, coverage, context) for validator in self.validators]
        missing_slots = _ordered_unique(
            slot
            for validation in validations
            for slot in validation.missing_slots
            if validation.reason == "missing_required_slots"
        )
        status = "composed"
        if any(validation.reason == "empty_evidence_set" for validation in validations if not validation.passed):
            status = "empty"
        elif missing_slots:
            status = "incomplete"
        elif any(not validation.passed for validation in validations):
            status = "needs_review"
        audit = {
            "schema": "pska.evidence_composition.v1",
            "pipeline": self.name,
            "status": status,
            "record_count": len(records),
            "slot_count": len(slots),
            "missing_slots": missing_slots,
            "source_type_counts": _source_type_counts(records),
            "coverage": {slot.name: coverage.get(slot.name, []) for slot in slots},
            "validations": [
                {
                    "name": validation.name,
                    "passed": validation.passed,
                    "reason": validation.reason,
                    "missing_slots": list(validation.missing_slots),
                }
                for validation in validations
            ],
        }
        evidence_set = EvidenceSet(
            evidence_set_id=_evidence_set_id(context.query, records),
            status=status,
            records=[_record_payload(record) for record in records],
            slots=[_slot_payload(slot, coverage.get(slot.name, [])) for slot in slots],
            missing_slots=missing_slots,
            conflicts=[],
            audit=audit,
        )
        return EvidenceCompositionResult(evidence_set=evidence_set, audit=audit)

    def _extract_slots(self, context: EvidenceCompositionContext) -> list[EvidenceSlot]:
        slots: list[EvidenceSlot] = []
        seen: set[str] = set()
        for extractor in self.slot_extractors:
            for slot in extractor.extract(context):
                if slot.name in seen:
                    continue
                seen.add(slot.name)
                slots.append(slot)
        return slots

    def _records(self, citations: list[dict[str, Any]], context: EvidenceCompositionContext) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for index, citation in enumerate(citations[: max(1, int(context.max_records or 1))], start=1):
            selection = citation.get("citation_selection") if isinstance(citation.get("citation_selection"), dict) else {}
            selected_span = str(selection.get("selected_span") or "").strip()
            text = selected_span or _citation_text(citation)
            record = EvidenceRecord(
                record_id=_record_id(citation, index=index),
                source_type=_citation_source_type(citation),
                citation=citation,
                text=text,
                selected_span=selected_span,
                rank=_int_value(selection.get("rank"), fallback=index),
                score=_float_value(selection.get("score"), fallback=0.0),
                metadata={
                    "title": citation.get("title"),
                    "source_item_id": citation.get("source_item_id"),
                    "document_id": citation.get("document_id"),
                    "chunk_id": citation.get("chunk_id"),
                    "passage_window_id": citation.get("passage_window_id"),
                    "features": selection.get("features") if isinstance(selection.get("features"), list) else [],
                },
            )
            records.append(record)
        return records

    def _graph_records(self, graph_paths: list[dict[str, Any]], *, start_rank: int, max_records: int) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        remaining = max(0, int(max_records or 0) - start_rank + 1)
        if remaining <= 0:
            return records
        for index, path in enumerate(graph_paths[:remaining], start=start_rank):
            text = str(path.get("explanation") or " -> ".join(str(item) for item in path.get("entities") or [])).strip()
            if not text:
                continue
            citation = {
                "schema": "pska.graph_evidence.v1",
                "source_type": "graph",
                "title": "Graph evidence",
                "snippet": text,
                "graph_path": path,
            }
            records.append(
                EvidenceRecord(
                    record_id=f"graph:{_stable_digest(text)[:16]}",
                    source_type="graph",
                    citation=citation,
                    text=text,
                    selected_span=text[:320],
                    rank=index,
                    score=_float_value(path.get("score"), fallback=0.0),
                    metadata={
                        "edge_count": path.get("edge_count"),
                        "grounded_edges": path.get("grounded_edges"),
                    },
                )
            )
        return records

    def _coverage(self, records: list[EvidenceRecord], slots: list[EvidenceSlot]) -> dict[str, list[str]]:
        coverage: dict[str, list[str]] = {}
        for slot in slots:
            for record in records:
                if _slot_supported_by_record(slot, record):
                    coverage.setdefault(slot.name, []).append(record.record_id)
        return coverage


def evidence_set_to_dict(evidence_set: EvidenceSet) -> dict[str, Any]:
    return {
        "schema": "pska.evidence_set.v1",
        "evidence_set_id": evidence_set.evidence_set_id,
        "status": evidence_set.status,
        "records": evidence_set.records,
        "slots": evidence_set.slots,
        "missing_slots": evidence_set.missing_slots,
        "conflicts": evidence_set.conflicts,
        "audit": evidence_set.audit,
    }


def _record_payload(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "source_type": record.source_type,
        "rank": record.rank,
        "score": round(record.score, 6),
        "selected_span": record.selected_span or record.text[:320],
        "text": record.text[:1200],
        "citation": record.citation,
        "metadata": record.metadata,
    }


def _slot_payload(slot: EvidenceSlot, record_ids: list[str]) -> dict[str, Any]:
    return {
        "name": slot.name,
        "type": slot.slot_type,
        "value": slot.value,
        "required": slot.required,
        "record_ids": list(record_ids),
        "covered": bool(record_ids),
    }


def _citation_text(citation: dict[str, Any]) -> str:
    source_window = citation.get("source_window") if isinstance(citation.get("source_window"), dict) else {}
    parts = [
        str(citation.get("title") or ""),
        str(citation.get("snippet") or ""),
        str(source_window.get("text") or ""),
    ]
    return "\n".join(part for part in parts if part).strip()


def _citation_source_type(citation: dict[str, Any]) -> str:
    explicit = str(citation.get("source_type") or citation.get("evidence_type") or "").strip().lower()
    if explicit:
        return explicit
    text = _citation_text(citation)
    if "|" in text or "\t" in text or re.search(r"\S\s{2,}\S", text):
        return "table"
    return "document"


def _slot_supported_by_record(slot: EvidenceSlot, record: EvidenceRecord) -> bool:
    if slot.slot_type == "recency":
        return True
    haystack = "\n".join(
        str(part or "")
        for part in (
            record.text,
            record.selected_span,
            record.metadata.get("title"),
            record.metadata.get("document_id"),
            record.citation.get("title"),
        )
    ).casefold()
    return _normalize_slot_value(slot.value) in _normalize_slot_value(haystack)


def _record_has_citation_identity(citation: dict[str, Any]) -> bool:
    if str(citation.get("source_type") or "").lower() == "graph":
        return bool(citation.get("graph_path"))
    return bool(str(citation.get("source_item_id") or "").strip())


def _record_id(citation: dict[str, Any], *, index: int) -> str:
    parts = [
        str(citation.get("source_item_id") or ""),
        str(citation.get("document_id") or ""),
        str(citation.get("chunk_id") or citation.get("passage_window_id") or ""),
        str(index),
    ]
    return f"evr:{_stable_digest(':'.join(parts))[:16]}"


def _evidence_set_id(query: str, records: list[EvidenceRecord]) -> str:
    payload = "|".join([query, *[record.record_id for record in records]])
    return f"evs:{_stable_digest(payload)[:20]}"


def _stable_digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _source_type_counts(records: list[EvidenceRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.source_type] = counts.get(record.source_type, 0) + 1
    return counts


def _normalize_slot_value(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _ordered_unique(values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def _int_value(value: Any, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float_value(value: Any, *, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback
