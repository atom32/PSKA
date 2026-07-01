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
    elif strategy == "heuristic":
        spans = _heuristic_chunks(source, chunking)
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
    parent_windows = _parent_windows(source, spans, chunking)
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
                "parent_window_ordinal": _parent_window_ordinal(parent_windows, span),
            }
            for span in spans
        ],
        "parent_windows": parent_windows,
    }


def _select_strategy(text: str, config: Mapping[str, Any]) -> str:
    requested = str(config.get("strategy") or "auto")
    if requested not in {"auto", "adaptive"}:
        return requested
    profile = _profile(text)
    if profile["markdown_headings"] >= 2:
        return "heading"
    if profile["has_markdown_table"] or profile["has_code_fence"] or profile["cjk_chars"] > 400:
        return "heuristic"
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


def _heuristic_chunks(text: str, config: Mapping[str, Any]) -> list[ChunkSpan]:
    blocks = _structural_blocks(text)
    if not blocks:
        return _window_chunks(text, config, strategy="recursive")
    chunk_size = int(config["chunk_size"])
    spans: list[ChunkSpan] = []
    current_start: int | None = None
    current_end: int | None = None
    for start, end, block_type in blocks:
        if end <= start:
            continue
        if end - start > chunk_size:
            if current_start is not None and current_end is not None and current_end > current_start:
                spans.extend(_window_chunks_for_range(text, current_start, current_end, config, strategy="heuristic"))
                current_start = None
                current_end = None
            spans.extend(_window_chunks_for_range(text, start, end, config, strategy="heuristic", context_header=block_type if block_type in {"code", "table"} else None))
            continue
        if current_start is None:
            current_start = start
            current_end = end
            continue
        proposed_end = end
        if proposed_end - current_start <= chunk_size:
            current_end = proposed_end
            continue
        spans.extend(_window_chunks_for_range(text, current_start, current_end or start, config, strategy="heuristic"))
        current_start = start
        current_end = end
    if current_start is not None and current_end is not None and current_end > current_start:
        spans.extend(_window_chunks_for_range(text, current_start, current_end, config, strategy="heuristic"))
    return spans or _window_chunks(text, config, strategy="recursive")


def _structural_blocks(text: str) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    line_start = 0
    lines: list[tuple[int, int, str]] = []
    for raw_line in text.splitlines(keepends=True):
        line_end = line_start + len(raw_line)
        lines.append((line_start, line_end, raw_line))
        line_start = line_end
    if not lines:
        return []
    cursor = 0
    in_code = False
    while cursor < len(lines):
        start, end, line = lines[cursor]
        stripped = line.strip()
        if stripped.startswith("```"):
            block_start = start
            cursor += 1
            in_code = not in_code
            while cursor < len(lines):
                _, block_end, next_line = lines[cursor]
                cursor += 1
                if next_line.strip().startswith("```"):
                    in_code = False
                    break
            else:
                block_end = len(text)
            blocks.append((block_start, block_end, "code"))
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            block_start = start
            block_end = end
            cursor += 1
            while cursor < len(lines):
                _, next_end, next_line = lines[cursor]
                next_stripped = next_line.strip()
                if not (next_stripped.startswith("|") and next_stripped.endswith("|")):
                    break
                block_end = next_end
                cursor += 1
            blocks.append((block_start, block_end, "table"))
            continue
        if not stripped:
            cursor += 1
            continue
        block_start = start
        block_end = end
        cursor += 1
        while cursor < len(lines):
            _, next_end, next_line = lines[cursor]
            next_stripped = next_line.strip()
            if not next_stripped or next_stripped.startswith("```") or (next_stripped.startswith("|") and next_stripped.endswith("|")):
                break
            block_end = next_end
            cursor += 1
        blocks.append((block_start, block_end, "paragraph"))
    if in_code:
        return _paragraph_blocks(text)
    return blocks


def _paragraph_blocks(text: str) -> list[tuple[int, int, str]]:
    blocks = []
    for match in re.finditer(r"\S(?:.*?(?:\n\s*\n|$))", text, flags=re.DOTALL):
        blocks.append((match.start(), match.end(), "paragraph"))
    return blocks


def _parent_windows(text: str, spans: list[ChunkSpan], config: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not spans:
        return []
    parent_size = int(config.get("parent_chunk_size") or config.get("parent_window_size") or max(int(config["chunk_size"]) * 3, int(config["chunk_size"])))
    parent_size = max(parent_size, int(config["chunk_size"]))
    windows: list[dict[str, Any]] = []
    current_start = spans[0].start
    current_end = spans[0].end
    child_ordinals: list[int] = []
    for span in spans:
        if child_ordinals and span.end - current_start > parent_size:
            windows.append(_parent_window_payload(text, current_start, current_end, len(windows), child_ordinals))
            current_start = span.start
            child_ordinals = []
        current_end = max(current_end, span.end)
        child_ordinals.append(span.ordinal)
    if child_ordinals:
        windows.append(_parent_window_payload(text, current_start, current_end, len(windows), child_ordinals))
    return windows


def _parent_window_payload(text: str, start: int, end: int, ordinal: int, child_ordinals: list[int]) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "start": start,
        "end": end,
        "chars": max(0, end - start),
        "text": text[start:end].strip(),
        "child_ordinals": list(child_ordinals),
        "window_policy": "parent_child_preview",
    }


def _parent_window_ordinal(parent_windows: list[dict[str, Any]], span: ChunkSpan) -> int | None:
    for window in parent_windows:
        if span.ordinal in set(window.get("child_ordinals") or []):
            return int(window.get("ordinal") or 0)
    return None


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
    if requested in {"auto", "adaptive"}:
        if selected == "heading":
            reason = "markdown_headings_detected"
        elif selected == "heuristic":
            reason = "tables_code_or_long_cjk_detected"
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
