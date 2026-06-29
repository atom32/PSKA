from __future__ import annotations

from dataclasses import dataclass
import re
from statistics import mean
from typing import Any, Mapping

from pska_core.processing import normalize_chunking_config


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ChunkSpan:
    text: str
    start: int
    end: int
    ordinal: int
    strategy: str
    context_header: str | None = None

    def embedding_text(self) -> str:
        if self.context_header and not self.text.lstrip().startswith(self.context_header):
            return f"{self.context_header}\n\n{self.text}"
        return self.text


def chunk_text(text: str, config: Mapping[str, Any] | None = None) -> list[ChunkSpan]:
    chunking = normalize_chunking_config(config)
    source = str(text or "")
    if not source:
        return [ChunkSpan(text="", start=0, end=0, ordinal=0, strategy=chunking["strategy"])]

    strategy = _select_strategy(source, chunking)
    if strategy == "heading":
        spans = _heading_chunks(source, chunking)
    elif strategy == "recursive":
        spans = _window_chunks(source, chunking, strategy="recursive")
    else:
        spans = _fixed_chunks(source, chunking)
    return _renumber([span for span in spans if span.text.strip() or len(spans) == 1])


def preview_chunking(text: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    chunking = normalize_chunking_config(config)
    source = str(text or "")
    selected = _select_strategy(source, chunking)
    spans = chunk_text(source, chunking)
    lengths = [len(span.text) for span in spans]
    return {
        "ok": True,
        "strategy": selected,
        "requested_strategy": chunking["strategy"],
        "strategy_diagnostics": _strategy_diagnostics(source, chunking, selected),
        "config": chunking,
        "profile": _profile(source),
        "stats": {
            "count": len(spans),
            "min_chars": min(lengths) if lengths else 0,
            "max_chars": max(lengths) if lengths else 0,
            "avg_chars": round(mean(lengths), 2) if lengths else 0,
            "total_chars": len(source),
        },
        "chunks": [
            {
                "ordinal": span.ordinal,
                "text": span.text,
                "start": span.start,
                "end": span.end,
                "chars": len(span.text),
                "strategy": span.strategy,
                "context_header": span.context_header,
            }
            for span in spans
        ],
    }


def _select_strategy(text: str, config: Mapping[str, Any]) -> str:
    requested = str(config.get("strategy") or "auto")
    if requested != "auto":
        return requested
    profile = _profile(text)
    if profile["markdown_headings"] >= 2:
        return "heading"
    if profile["lines"] > 3 or any(separator in text for separator in config.get("separators", [])):
        return "recursive"
    return "fixed"


def _heading_chunks(text: str, config: Mapping[str, Any]) -> list[ChunkSpan]:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return _window_chunks(text, config, strategy="recursive")
    spans: list[ChunkSpan] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        if not section:
            continue
        header = match.group(2).strip()
        section_spans = _window_chunks_for_range(
            text,
            start,
            end,
            config,
            strategy="heading",
            context_header=header,
        )
        spans.extend(section_spans)
    prefix = text[: matches[0].start()].strip()
    if prefix:
        spans = _window_chunks_for_range(text, 0, matches[0].start(), config, strategy="heading") + spans
    return spans or _window_chunks(text, config, strategy="recursive")


def _window_chunks(text: str, config: Mapping[str, Any], *, strategy: str) -> list[ChunkSpan]:
    return _window_chunks_for_range(text, 0, len(text), config, strategy=strategy)


def _fixed_chunks(text: str, config: Mapping[str, Any]) -> list[ChunkSpan]:
    chunk_size = int(config["chunk_size"])
    overlap = int(config["chunk_overlap"])
    step = max(1, chunk_size - overlap)
    spans = []
    for start in range(0, len(text), step):
        end = min(len(text), start + chunk_size)
        spans.append(ChunkSpan(text=text[start:end].strip(), start=start, end=end, ordinal=len(spans), strategy="fixed"))
        if end >= len(text):
            break
    return spans


def _window_chunks_for_range(
    full_text: str,
    range_start: int,
    range_end: int,
    config: Mapping[str, Any],
    *,
    strategy: str,
    context_header: str | None = None,
) -> list[ChunkSpan]:
    chunk_size = int(config["chunk_size"])
    overlap = int(config["chunk_overlap"])
    separators = list(config.get("separators") or [])
    spans: list[ChunkSpan] = []
    cursor = range_start
    while cursor < range_end:
        hard_end = min(range_end, cursor + chunk_size)
        end = _best_break(full_text, cursor, hard_end, range_end, separators)
        raw = full_text[cursor:end]
        stripped = raw.strip()
        if stripped:
            leading = len(raw) - len(raw.lstrip())
            trailing = len(raw.rstrip())
            spans.append(
                ChunkSpan(
                    text=stripped,
                    start=cursor + leading,
                    end=cursor + trailing,
                    ordinal=len(spans),
                    strategy=strategy,
                    context_header=context_header,
                )
            )
        if end >= range_end:
            break
        cursor = max(end - overlap, cursor + 1)
    return spans


def _best_break(text: str, start: int, hard_end: int, range_end: int, separators: list[str]) -> int:
    if hard_end >= range_end:
        return range_end
    minimum = start + max(1, int((hard_end - start) * 0.45))
    best = -1
    for separator in separators:
        index = text.rfind(separator, minimum, hard_end)
        if index > best:
            best = index + len(separator)
    return best if best > start else hard_end


def _renumber(spans: list[ChunkSpan]) -> list[ChunkSpan]:
    return [
        ChunkSpan(
            text=span.text,
            start=span.start,
            end=span.end,
            ordinal=index,
            strategy=span.strategy,
            context_header=span.context_header,
        )
        for index, span in enumerate(spans)
    ]


def _profile(text: str) -> dict[str, Any]:
    return {
        "chars": len(text),
        "lines": len(text.splitlines()) if text else 0,
        "markdown_headings": len(HEADING_RE.findall(text)),
        "has_markdown_table": bool(re.search(r"^\s*\|.+\|\s*$", text, re.MULTILINE)),
        "has_code_fence": "```" in text,
        "cjk_chars": len(re.findall(r"[\u4e00-\u9fff]", text)),
    }


def _strategy_diagnostics(text: str, config: Mapping[str, Any], selected: str) -> dict[str, Any]:
    requested = str(config.get("strategy") or "auto")
    profile = _profile(text)
    reason = "requested_explicit_strategy"
    if requested == "auto":
        if selected == "heading":
            reason = "markdown_headings_detected"
        elif selected == "recursive":
            reason = "paragraph_or_separator_boundaries_detected"
        else:
            reason = "plain_text_fallback"
    return {
        "reason": reason,
        "requested": requested,
        "selected": selected,
        "profile": profile,
    }
