from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class AnswerPipelineContext:
    query: str
    ask_intent: str
    evidence_status: str
    required_values: tuple[str, ...] = ()
    support_terms: tuple[str, ...] = ()
    reject_raw_evidence_listing: bool = False


@dataclass(frozen=True, slots=True)
class AnswerCandidate:
    answer: str
    answer_type: str
    owner: str
    priority: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnswerValidation:
    name: str
    passed: bool
    reason: str = ""
    missing_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerDecision:
    answer: str
    answer_type: str
    owner: str
    audit: dict[str, Any]


class AnswerValidator:
    name = "base"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        raise NotImplementedError


class NonEmptyAnswerValidator(AnswerValidator):
    name = "non_empty_answer"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        passed = bool(str(candidate.answer or "").strip())
        return AnswerValidation(self.name, passed, "" if passed else "empty_answer")


class RequiredValueCoverageValidator(AnswerValidator):
    name = "required_value_coverage"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        required = tuple(value for value in context.required_values if str(value or "").strip())
        if not required:
            return AnswerValidation(self.name, True)
        answer_text = str(candidate.answer or "").casefold()
        missing = tuple(value for value in required if value.casefold() not in answer_text)
        return AnswerValidation(
            self.name,
            not missing,
            "" if not missing else "missing_required_values",
            missing_values=missing,
        )


class SupportTermCoverageValidator(AnswerValidator):
    name = "support_term_coverage"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        if str(candidate.answer_type or "") == "no_answer" or str(context.evidence_status or "") != "supported":
            return AnswerValidation(self.name, True)
        if not candidate.metadata.get("validate_support_term_coverage", False):
            return AnswerValidation(self.name, True)
        support_terms = tuple(
            term
            for term in [*context.support_terms, *tuple(candidate.metadata.get("support_terms") or ())]
            if str(term or "").strip()
        )
        if not support_terms:
            return AnswerValidation(self.name, True)
        answer_text = str(candidate.answer or "").casefold()
        hits = tuple(term for term in support_terms if _term_supported_in_answer(str(term), answer_text))
        return AnswerValidation(
            self.name,
            bool(hits),
            "" if hits else "missing_support_terms",
            missing_values=tuple(dict.fromkeys(support_terms))[:8],
        )


class RawEvidenceListingValidator(AnswerValidator):
    name = "raw_evidence_listing"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        if not context.reject_raw_evidence_listing:
            return AnswerValidation(self.name, True)
        if not candidate.metadata.get("validate_raw_evidence_listing", True):
            return AnswerValidation(self.name, True)
        passed = not _answer_looks_like_raw_evidence_listing(candidate.answer)
        return AnswerValidation(self.name, passed, "" if passed else "raw_evidence_listing")


class RuntimeControlSignalValidator(AnswerValidator):
    name = "runtime_control_signal"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        passed = not _answer_looks_like_runtime_control_signal(candidate.answer)
        return AnswerValidation(self.name, passed, "" if passed else "runtime_control_signal")


class RuntimeArtifactValidator(AnswerValidator):
    name = "runtime_artifact"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        passed = not _answer_looks_like_runtime_artifact(candidate.answer)
        return AnswerValidation(self.name, passed, "" if passed else "runtime_artifact")


class TaskMismatchValidator(AnswerValidator):
    name = "task_mismatch"

    def validate(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> AnswerValidation:
        if str(candidate.answer_type or "") == "no_answer" or str(context.evidence_status or "") != "supported":
            return AnswerValidation(self.name, True)
        passed = not _answer_looks_like_task_mismatch(candidate.answer)
        return AnswerValidation(self.name, passed, "" if passed else "task_mismatch")


class AnswerPipeline:
    name = "deterministic_answer_pipeline"

    def __init__(self, validators: list[AnswerValidator] | None = None) -> None:
        self.validators = validators or [
            NonEmptyAnswerValidator(),
            RuntimeControlSignalValidator(),
            RuntimeArtifactValidator(),
            TaskMismatchValidator(),
            RequiredValueCoverageValidator(),
            SupportTermCoverageValidator(),
            RawEvidenceListingValidator(),
        ]

    def decide(
        self,
        candidates: list[AnswerCandidate],
        context: AnswerPipelineContext,
    ) -> AnswerDecision:
        ordered = sorted(candidates, key=lambda candidate: candidate.priority)
        records = [self._record(candidate, context) for candidate in ordered]
        selected_record = next((record for record in records if record["status"] == "passed"), None)
        if selected_record is None and records:
            selected_record = records[-1]
            selected_record["status"] = "selected_with_validation_warnings"
        if selected_record is None:
            selected_record = {
                "owner": "answer_pipeline",
                "answer": "",
                "answer_type": "no_answer",
                "status": "failed",
                "validations": [],
            }
        audit = {
            "schema": "pska.answer_pipeline.v1",
            "pipeline": self.name,
            "evidence_status": context.evidence_status,
            "selected_owner": selected_record.get("owner"),
            "selected_status": selected_record.get("status"),
            "candidate_count": len(records),
            "required_values": list(context.required_values),
            "candidates": [_public_record(record) for record in records],
        }
        return AnswerDecision(
            answer=str(selected_record.get("answer") or ""),
            answer_type=str(selected_record.get("answer_type") or "kb_answer"),
            owner=str(selected_record.get("owner") or "answer_pipeline"),
            audit=audit,
        )

    def _record(self, candidate: AnswerCandidate, context: AnswerPipelineContext) -> dict[str, Any]:
        validations = [validator.validate(candidate, context) for validator in self.validators]
        blocking = [validation for validation in validations if not validation.passed]
        return {
            "owner": candidate.owner,
            "answer": candidate.answer,
            "answer_type": candidate.answer_type,
            "priority": candidate.priority,
            "status": "rejected" if blocking else "passed",
            "metadata": candidate.metadata,
            "validations": [
                {
                    "name": validation.name,
                    "passed": validation.passed,
                    "reason": validation.reason,
                    "missing_values": list(validation.missing_values),
                }
                for validation in validations
            ],
        }


def required_answer_values(query: str, deterministic_answer: str, *, ask_intent: str) -> tuple[str, ...]:
    if not str(deterministic_answer or "").strip():
        return ()
    if ask_intent == "writing" and _query_requests_preserved_numbers(query):
        return tuple(_numeric_values(deterministic_answer))
    if _query_requests_multiple_values(query):
        return tuple(_numeric_values(deterministic_answer))
    return ()


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    answer = str(record.get("answer") or "")
    return {
        "owner": record.get("owner"),
        "answer_type": record.get("answer_type"),
        "priority": record.get("priority"),
        "status": record.get("status"),
        "answer_length": len(answer),
        "metadata": record.get("metadata") or {},
        "validations": record.get("validations") or [],
    }


def _query_requests_preserved_numbers(query: str) -> bool:
    text = str(query or "").casefold()
    markers = (
        "保留数字",
        "保留数值",
        "保留具体数字",
        "preserve numbers",
        "keep numbers",
        "include numbers",
    )
    return any(marker in text for marker in markers)


def _query_requests_multiple_values(query: str) -> bool:
    text = str(query or "")
    if any(marker in text for marker in ["、", "/", "以及", "分别"]):
        return True
    return bool(re.search(r"\b(?:and|plus)\b", text, flags=re.IGNORECASE)) or "和" in text


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


def _term_supported_in_answer(term: str, answer_text: str) -> bool:
    normalized = str(term or "").strip().casefold()
    if len(normalized) < 2:
        return False
    if re.search(r"[a-z0-9]", normalized):
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", answer_text) is not None
    return normalized in answer_text


def _answer_looks_like_raw_evidence_listing(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    if "当前资料支持以下结论" in text:
        return True
    if text.count(" / ") >= 3:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 4 or not lines[0].startswith(("关键结论", "结论")):
        return False
    short_fact_lines = sum(
        1
        for line in lines[1:]
        if len(line) <= 180 and re.search(r"\d|=| is |为|：|:", line, flags=re.IGNORECASE)
    )
    return short_fact_lines >= 3


def _answer_looks_like_runtime_control_signal(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    first_line = text.splitlines()[0].strip().upper()
    return first_line.startswith(("[STOPPED]", "[ERROR]", "[CANCELLED]", "[CANCELED]", "[INJECTED]"))


def _answer_looks_like_runtime_artifact(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    procedural_markers = (
        "i'll search",
        "i will search",
        "let me search",
        "searching the",
        "checking index status",
        "proceeding with search",
        "proceeding with retrieval",
        "我要搜索",
        "我将搜索",
        "正在搜索",
        "开始检索",
        "继续检索",
    )
    if any(marker in lowered for marker in procedural_markers):
        return True
    if re.fullmatch(r"<[A-Za-z][A-Za-z0-9_.:-]*\b[^>]*>.*</[A-Za-z][A-Za-z0-9_.:-]*>", text, flags=re.DOTALL):
        return True
    first_line = text.splitlines()[0].strip()
    return bool(re.match(r"^<[/]?(?:tool|mcp|function|call|[A-Za-z0-9_]*_[A-Za-z0-9_]+)\b", first_line, flags=re.IGNORECASE))


def _answer_looks_like_task_mismatch(answer: str) -> bool:
    text = str(answer or "").strip().casefold()
    if not text:
        return False
    markers = (
        "request appears truncated",
        "query appears truncated",
        "question appears truncated",
        "cannot determine the specific query",
        "cannot determine the query",
        "cannot determine the task",
        "cannot identify the question",
        "unable to determine the query",
        "unable to identify the question",
        "no specific query",
        "no user query",
        "没有明确的问题",
        "无法判断具体问题",
        "无法确定具体问题",
        "无法识别问题",
        "请求似乎被截断",
    )
    return any(marker in text for marker in markers)
