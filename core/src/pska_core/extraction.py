from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import uuid5, NAMESPACE_URL

from pska_core.enums import Directionality, ReviewType, Visibility
from pska_core.hypergraph import HypergraphService
from pska_core.llm import LLMClient, LLMResponseError, OpenAILLMClient, record_recovery_event
from pska_core.models import Entity, ReviewItem, SourceItem, SourceRef
from pska_core.store import KnowledgeStore


@dataclass(slots=True)
class ExtractionReport:
    source_item_id: str
    entities_created: list[str] = field(default_factory=list)
    hyperedges_created: list[str] = field(default_factory=list)
    review_items_created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ExtractionService:
    """LLM-required extractor for entities, hyperedges, and review items."""

    def __init__(self, store: KnowledgeStore, llm: LLMClient | None = None) -> None:
        self.store = store
        self.graph = HypergraphService(store)
        self.llm = llm

    def extract_source_item(self, item: SourceItem) -> ExtractionReport:
        report = ExtractionReport(source_item_id=item.source_item_id)
        text = item.content_text
        source_ref = SourceRef(
            source_item_id=item.source_item_id,
            path=(item.metadata.get("raw_paths") or {}).get("markdown"),
            url=item.url,
        )

        extraction = self._extract_with_llm(item)

        for entity_spec in extraction["entities"]:
            entity_type = str(entity_spec["entity_type"])
            label = str(entity_spec["label"])
            entity = self._create_entity(item, entity_type, label)
            report.entities_created.append(entity.entity_id)

        for edge_spec in extraction["hyperedges"]:
            member_ids = []
            for entity_type, label, role in edge_spec["members"]:
                entity = self._create_entity(item, entity_type, label)
                if entity.entity_id not in report.entities_created:
                    report.entities_created.append(entity.entity_id)
                member_ids.append((entity.entity_id, role))
            edge = self.graph.create_hyperedge(
                relation_type=edge_spec["relation_type"],
                owner_user_id=item.owner_user_id,
                space_id=item.space_id,
                visibility=item.visibility,
                visible_team_ids=item.visible_team_ids,
                directionality=edge_spec.get("directionality", Directionality.AMBIGUOUS),
                members=member_ids,
                evidence_text=edge_spec["evidence_text"],
                source_refs=[source_ref],
                confidence=edge_spec.get("confidence", 0.75),
            )
            report.hyperedges_created.append(edge.hyperedge_id)

        for review_spec in extraction["review_items"]:
            review = ReviewItem(
                review_item_id=self._id("review", item.source_item_id, str(review_spec["review_type"]), str(review_spec["title"])),
                owner_user_id=item.owner_user_id,
                review_type=ReviewType(str(review_spec["review_type"])),
                title=str(review_spec["title"]),
                proposal=self._proposal_with_source_refs(item, review_spec, source_ref),
            )
            self.store.add_review_item(review)
            report.review_items_created.append(review.review_item_id)

        if not report.entities_created:
            report.warnings.append("no_entities_extracted")
        return report

    def extract_all_visible(self, owner_user_id: str | None = None) -> list[ExtractionReport]:
        reports = []
        for item in self.store.list_source_items():
            if owner_user_id and item.owner_user_id != owner_user_id:
                continue
            reports.append(self.extract_source_item(item))
        return reports

    def _extract_with_llm(self, item: SourceItem) -> dict:
        system = (
            "You are PSKA's knowledge extraction agent. Extract only facts grounded in the document. "
            "Return strict JSON. Do not invent entities, relations, evidence, or directionality. "
            "Use anonymous labels exactly as found when a real person is represented by an alias. "
            "Keep JSON keys and enum values exactly as specified, but write user-facing natural-language values "
            "such as title, evidence_text, and proposal text in Chinese by default unless the source text requires another language."
        )
        prompt = f"""
Return a JSON object with exactly these keys:
- entities: array of objects with entity_type, label
- hyperedges: array of objects with relation_type, directionality, evidence_text, confidence, members
- review_items: array of objects with review_type, title, proposal

Hyperedge member objects must contain entity_type, label, role.
directionality must be one of: directed, undirected, ambiguous.
review_type must be one of: share_proposal, sensitive_content, profile_update, entity_merge, conflict, memory_candidate, relationship_candidate, action_candidate, low_confidence.
Only create review items for sharing, sensitive personal memory/profile updates, entity merges, conflicting facts, action candidates, or low-confidence memory/relationship candidates.
If no grounded item exists, return an empty array for that key.
Keep schema keys and enum values in English. Prefer Chinese for natural-language values that a user will read.

Source metadata:
source_item_id: {item.source_item_id}
source_channel: {item.source_channel}
record_type: {item.record_type}
title: {item.title}

For conversation records, preserve provenance by including proposal.message_ids
for any review item grounded in specific messages.

Document text:
{item.content_text[:12000]}
"""
        llm = self.llm or OpenAILLMClient.from_env()
        raw = llm.complete_json(system=system, prompt=prompt, temperature=0.0)
        try:
            return self._validate_extraction(raw)
        except LLMResponseError as exc:
            record_recovery_event("llm_extraction_schema_repair", {"source_item_id": item.source_item_id, "error": str(exc)})
            repaired = self._repair_extraction_schema(llm, raw, str(exc))
            return self._validate_extraction(repaired)

    def _repair_extraction_schema(self, llm: LLMClient, raw: dict, error: str) -> dict:
        system = (
            "You are PSKA's extraction schema correction agent. Return strict JSON only. "
            "Do not add facts; only reshape the previous extraction to match the required schema. "
            "Keep schema keys and enum values in English; preserve or convert user-facing natural-language values to Chinese when possible."
        )
        prompt = f"""
The previous extraction failed PSKA schema validation:
{error}

Convert this object to exactly:
{{
  "entities": [{{"entity_type": "string", "label": "string"}}],
  "hyperedges": [
    {{
      "relation_type": "string",
      "directionality": "directed|undirected|ambiguous",
      "evidence_text": "string",
      "confidence": 0.0,
      "members": [{{"entity_type": "string", "label": "string", "role": "string"}}]
    }}
  ],
  "review_items": [
    {{
      "review_type": "share_proposal|sensitive_content|profile_update|entity_merge|conflict|memory_candidate|relationship_candidate|action_candidate|low_confidence",
      "title": "string",
      "proposal": {{}}
    }}
  ]
}}

Previous extraction:
{raw}
"""
        return llm.complete_json(system=system, prompt=prompt, temperature=0.0)

    def _validate_extraction(self, raw: dict) -> dict:
        try:
            entities = list(raw["entities"])
            hyperedges = list(raw["hyperedges"])
            review_items = list(raw["review_items"])
        except Exception as exc:  # noqa: BLE001
            raise LLMResponseError("Extraction JSON must contain entities, hyperedges, and review_items arrays") from exc

        normalized_edges = []
        for edge in hyperedges:
            try:
                members = []
                for member in edge["members"]:
                    members.append((str(member["entity_type"]), str(member["label"]), str(member["role"])))
                if len(members) < 2:
                    raise ValueError("hyperedges must have at least two members")
                normalized_edges.append({
                    "relation_type": str(edge["relation_type"]),
                    "directionality": Directionality(str(edge.get("directionality") or Directionality.AMBIGUOUS)),
                    "evidence_text": str(edge["evidence_text"]),
                    "confidence": float(edge.get("confidence", 0.75)),
                    "members": members,
                })
            except Exception as exc:  # noqa: BLE001
                raise LLMResponseError(f"Invalid hyperedge schema: {edge}") from exc
        normalized_reviews = []
        for review in review_items:
            try:
                proposal = review.get("proposal")
                if not isinstance(proposal, dict):
                    raise TypeError("review_items[].proposal must be an object")
                normalized_reviews.append({
                    "review_type": str(review["review_type"]),
                    "title": str(review["title"]),
                    "proposal": proposal,
                })
            except Exception as exc:  # noqa: BLE001
                raise LLMResponseError(f"Invalid review item schema: {review}") from exc
        return {
            "entities": [{"entity_type": str(entity["entity_type"]), "label": str(entity["label"])} for entity in entities],
            "hyperedges": normalized_edges,
            "review_items": normalized_reviews,
        }

    def _proposal_with_source_refs(
        self,
        item: SourceItem,
        review_spec: dict,
        source_ref: SourceRef,
    ) -> dict:
        proposal = dict(review_spec.get("proposal") or {})
        source_refs = proposal.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            proposal["source_refs"] = [asdict(source_ref)]

        message_ids = proposal.get("message_ids")
        if item.record_type == "conversation" and isinstance(message_ids, list):
            refs = [ref for ref in proposal["source_refs"] if isinstance(ref, dict)]
            for message_id in message_ids:
                if not message_id:
                    continue
                refs.append(
                    asdict(
                        SourceRef(
                            source_item_id=item.source_item_id,
                            message_id=str(message_id),
                            url=item.url,
                        )
                    )
                )
            proposal["source_refs"] = refs
        return proposal

    def _create_entity(self, item: SourceItem, entity_type: str, label: str) -> Entity:
        entity = Entity(
            entity_id=self._id("ent", item.owner_user_id, entity_type, label),
            entity_type=entity_type,
            label=label,
            owner_user_id=item.owner_user_id,
            space_id=item.space_id,
            visibility=item.visibility,
            visible_team_ids=item.visible_team_ids,
        )
        self.store.add_entity(entity)
        return entity

    def _id(self, prefix: str, *parts: str) -> str:
        return f"{prefix}_{uuid5(NAMESPACE_URL, '|'.join(parts)).hex}"
