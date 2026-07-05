from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class CitationSelectionContext:
    query: str
    query_terms: tuple[str, ...] = ()
    anchor_terms: tuple[str, ...] = ()
    max_citations: int = 6


@dataclass(frozen=True, slots=True)
class CitationCandidate:
    citation: dict[str, Any]
    text: str
    original_rank: int


@dataclass(frozen=True, slots=True)
class CitationFeature:
    name: str
    value: float
    weight: float

    @property
    def contribution(self) -> float:
        return self.value * self.weight


@dataclass(frozen=True, slots=True)
class CitationScore:
    score: float
    features: list[CitationFeature] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CitationSelectionResult:
    selected: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    audit: dict[str, Any]


class CitationScorer:
    name = "base"
    weight = 0.0

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        raise NotImplementedError


class SupportHitScorer(CitationScorer):
    name = "support_hits"
    weight = 0.16

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        hits = _string_list(candidate.citation.get("support_hits"))
        if not hits:
            return None
        denominator = max(len(context.anchor_terms) or len(context.query_terms), 1)
        value = min(len(set(hit.casefold() for hit in hits)) / denominator, 1.0)
        return CitationFeature(self.name, value, self.weight)


class AnchorCoverageCitationScorer(CitationScorer):
    name = "anchor_coverage"
    weight = 0.14

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        coverage = _term_coverage(context.anchor_terms, candidate.text)
        if coverage <= 0:
            return None
        return CitationFeature(self.name, coverage, self.weight)


class QueryTermCoverageCitationScorer(CitationScorer):
    name = "query_term_coverage"
    weight = 0.08

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        terms = context.query_terms or tuple(_tokenize_query(context.query))
        coverage = _term_coverage(terms, candidate.text)
        if coverage <= 0:
            return None
        return CitationFeature(self.name, coverage, self.weight)


class NumericAlignmentCitationScorer(CitationScorer):
    name = "numeric_alignment"
    weight = 0.06

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        numbers = _numeric_values(context.query)
        if not numbers:
            return None
        coverage = _term_coverage(tuple(numbers), candidate.text)
        if coverage <= 0:
            return None
        return CitationFeature(self.name, coverage, self.weight)


class EvidenceTextAvailabilityScorer(CitationScorer):
    name = "evidence_text_available"
    weight = 0.03

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        if not candidate.text.strip():
            return None
        value = min(len(candidate.text) / 400.0, 1.0)
        return CitationFeature(self.name, value, self.weight)


class RetrievalOrderCitationScorer(CitationScorer):
    name = "retrieval_order"
    weight = 0.03

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationFeature | None:
        value = 1.0 / max(candidate.original_rank, 1)
        return CitationFeature(self.name, value, self.weight)


class CitationSelectionPipeline:
    name = "deterministic_citation_selection"

    def __init__(self, scorers: list[CitationScorer] | None = None) -> None:
        self.scorers = scorers or [
            SupportHitScorer(),
            AnchorCoverageCitationScorer(),
            QueryTermCoverageCitationScorer(),
            NumericAlignmentCitationScorer(),
            EvidenceTextAvailabilityScorer(),
            RetrievalOrderCitationScorer(),
        ]

    def select(
        self,
        citations: list[dict[str, Any]],
        context: CitationSelectionContext,
    ) -> CitationSelectionResult:
        candidates = [
            CitationCandidate(citation=citation, text=_citation_text(citation), original_rank=index)
            for index, citation in enumerate(citations, start=1)
        ]
        records = [
            (candidate, self.score(candidate, context))
            for candidate in candidates
        ]
        records.sort(key=lambda item: (item[1].score, -item[0].original_rank), reverse=True)
        limit = max(1, int(context.max_citations or 1))
        selected_records = records[:limit]
        dropped_records = records[limit:]
        selected = [
            self._annotate(candidate, score, rank=rank, context=context)
            for rank, (candidate, score) in enumerate(selected_records, start=1)
        ]
        dropped = [
            {
                **self._annotate(candidate, score, rank=rank + limit, context=context),
                "drop_reason": "citation_selection_overflow",
            }
            for rank, (candidate, score) in enumerate(dropped_records, start=1)
        ]
        audit = {
            "schema": "pska.citation_selection.v1",
            "pipeline": self.name,
            "candidate_count": len(citations),
            "selected_count": len(selected),
            "dropped_count": len(dropped),
            "max_citations": limit,
            "selected": [_citation_summary(citation) for citation in selected],
            "dropped": [_citation_summary(citation) for citation in dropped],
        }
        return CitationSelectionResult(selected=selected, dropped=dropped, audit=audit)

    def score(self, candidate: CitationCandidate, context: CitationSelectionContext) -> CitationScore:
        features = [
            feature
            for scorer in self.scorers
            if (feature := scorer.score(candidate, context)) is not None
        ]
        return CitationScore(score=sum(feature.contribution for feature in features), features=features)

    def _annotate(
        self,
        candidate: CitationCandidate,
        score: CitationScore,
        *,
        rank: int,
        context: CitationSelectionContext,
    ) -> dict[str, Any]:
        annotation = {
            "schema": "pska.citation_selection_record.v1",
            "pipeline": self.name,
            "rank": rank,
            "original_rank": candidate.original_rank,
            "score": round(score.score, 6),
            "features": [
                {
                    "name": feature.name,
                    "value": round(feature.value, 6),
                    "weight": round(feature.weight, 6),
                    "contribution": round(feature.contribution, 6),
                }
                for feature in score.features
            ],
            "selected_span": _best_span(candidate.text, context),
        }
        return {**candidate.citation, "citation_selection": annotation}


def _citation_text(citation: dict[str, Any]) -> str:
    source_window = citation.get("source_window") if isinstance(citation.get("source_window"), dict) else {}
    parts = [
        str(citation.get("title") or ""),
        str(citation.get("snippet") or ""),
        str(source_window.get("text") or ""),
    ]
    return "\n".join(part for part in parts if part).strip()


def _citation_summary(citation: dict[str, Any]) -> dict[str, Any]:
    selection = citation.get("citation_selection") if isinstance(citation.get("citation_selection"), dict) else {}
    return {
        "source_item_id": citation.get("source_item_id"),
        "document_id": citation.get("document_id"),
        "chunk_id": citation.get("chunk_id"),
        "passage_window_id": citation.get("passage_window_id"),
        "title": citation.get("title"),
        "drop_reason": citation.get("drop_reason"),
        "rank": selection.get("rank"),
        "original_rank": selection.get("original_rank"),
        "score": selection.get("score"),
        "features": selection.get("features") or [],
        "selected_span": selection.get("selected_span"),
    }


def _best_span(text: str, context: CitationSelectionContext, *, max_chars: int = 320) -> str:
    candidates = _span_candidates(text)
    if not candidates:
        return ""
    terms = tuple(dict.fromkeys([*context.anchor_terms, *context.query_terms, *_numeric_values(context.query)]))

    def span_score(span: str) -> tuple[float, int]:
        coverage = _term_coverage(terms, span) if terms else 0.0
        numeric_hits = _term_coverage(tuple(_numeric_values(context.query)), span)
        return (coverage + numeric_hits, min(len(span), max_chars))

    best = max(candidates, key=span_score)
    return _compact_text(best, max_chars=max_chars)


def _span_candidates(text: str) -> list[str]:
    spans: list[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            spans.append(line)
            continue
        spans.extend(part.strip() for part in re.split(r"(?<=[。！？.!?])\s+|[；;]\s+", line) if part.strip())
    if not spans and str(text or "").strip():
        spans.append(str(text).strip())
    return spans[:80]


def _term_coverage(terms: tuple[str, ...], text: str) -> float:
    normalized_terms = [term.casefold() for term in terms if str(term or "").strip()]
    if not normalized_terms:
        return 0.0
    haystack = str(text or "").casefold()
    hits = sum(1 for term in dict.fromkeys(normalized_terms) if term in haystack)
    return hits / len(set(normalized_terms))


def _tokenize_query(query: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", str(query or "").casefold()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens[:24]


def _numeric_values(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    numeric_pattern = (
        r"(?<![A-Za-z0-9_-])"
        r"\d+(?:[.,]\d+)*"
        r"(?:\s*(?:ms|s|usd|rmb|元|万元|亿元|%))?"
    )
    for match in re.finditer(numeric_pattern, str(text or ""), flags=re.IGNORECASE):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _compact_text(text: str, *, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."
