from __future__ import annotations

import re
from typing import List, Tuple

Span = Tuple[int, int]


def _compile_query(
    query: str,
    *,
    regex: bool = False,
    whole_word: bool = False,
    case_sensitive: bool = False,
) -> tuple[re.Pattern[str] | None, str | None]:
    if not query:
        return None, None
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        source = rf"\b(?:{query})\b" if whole_word else query
        try:
            return re.compile(source, flags), None
        except re.error as exc:
            return None, f"Invalid regex: {exc}"
    source = re.escape(query)
    if whole_word:
        source = rf"\b{source}\b"
    return re.compile(source, flags), None


def find_spans(
    text: str,
    query: str,
    *,
    regex: bool = False,
    whole_word: bool = False,
    case_sensitive: bool = False,
) -> tuple[List[Span], str | None]:
    text = text or ""
    pattern, error = _compile_query(
        query,
        regex=regex,
        whole_word=whole_word,
        case_sensitive=case_sensitive,
    )
    if error:
        return [], error
    if pattern is None:
        return [], None
    spans: List[Span] = []
    for match in pattern.finditer(text):
        if match.end() > match.start():
            spans.append((match.start(), match.end()))
    return spans, None


def replace_spans(text: str, spans: List[Span], replacement: str) -> str:
    out = text or ""
    repl = replacement if replacement is not None else ""
    for start, end in reversed(spans):
        out = out[:start] + repl + out[end:]
    return out


def replace_all(
    text: str,
    query: str,
    replacement: str,
    *,
    regex: bool = False,
    whole_word: bool = False,
    case_sensitive: bool = False,
) -> tuple[str, int, str | None]:
    spans, error = find_spans(
        text,
        query,
        regex=regex,
        whole_word=whole_word,
        case_sensitive=case_sensitive,
    )
    if error:
        return text or "", 0, error
    return replace_spans(text, spans, replacement), len(spans), None


def replace_at(
    text: str,
    query: str,
    replacement: str,
    index: int,
    *,
    regex: bool = False,
    whole_word: bool = False,
    case_sensitive: bool = False,
) -> tuple[str, int, str | None]:
    spans, error = find_spans(
        text,
        query,
        regex=regex,
        whole_word=whole_word,
        case_sensitive=case_sensitive,
    )
    if error:
        return text or "", index, error
    if not spans:
        return text or "", 0, None
    idx = index % len(spans)
    updated = replace_spans(text, [spans[idx]], replacement)
    return updated, idx, None
