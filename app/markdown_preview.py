from __future__ import annotations

import html
import re

FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
UL_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
OL_RE = re.compile(r"^(\s*)\d+\.\s+(.+)$")
HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
SAFE_URL_RE = re.compile(r"^(https?:|mailto:|#)", re.IGNORECASE)


def _safe_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw or not SAFE_URL_RE.match(raw):
        return None
    if raw.lower().startswith("javascript:"):
        return None
    return html.escape(raw, quote=True)


def _stash(slots: list[str], fragment: str) -> str:
    slots.append(fragment)
    return f"\x00S{len(slots) - 1}\x00"


def apply_inline(text: str) -> str:
    slots: list[str] = []
    working = text or ""

    def code_repl(match: re.Match[str]) -> str:
        return _stash(slots, f"<code>{html.escape(match.group(1))}</code>")

    working = re.sub(r"`([^`]+)`", code_repl, working)

    def img_repl(match: re.Match[str]) -> str:
        url = _safe_url(match.group(2))
        alt = html.escape(match.group(1), quote=True)
        if not url:
            return match.group(0)
        return _stash(slots, f'<img alt="{alt}" src="{url}">')

    working = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", img_repl, working)

    def link_repl(match: re.Match[str]) -> str:
        url = _safe_url(match.group(2))
        label = html.escape(match.group(1))
        if not url:
            return match.group(0)
        return _stash(slots, f'<a href="{url}" rel="noopener noreferrer">{label}</a>')

    working = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, working)
    working = html.escape(working)
    working = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", working)
    working = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", working)

    def restore(match: re.Match[str]) -> str:
        return slots[int(match.group(1))]

    return re.sub(r"\x00S(\d+)\x00", restore, working)


def render_markdown(source: str | None) -> str:
    text = (source or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    fences: list[str] = []

    def fence_repl(match: re.Match[str]) -> str:
        code = html.escape(match.group(2).rstrip("\n"))
        fences.append(f"<pre><code>{code}</code></pre>")
        return f"\x00F{len(fences) - 1}\x00"

    text = FENCE_RE.sub(fence_repl, text)
    lines = text.split("\n")
    parts: list[str] = []
    i = 0

    def fence_line(line: str) -> str | None:
        match = re.fullmatch(r"\x00F(\d+)\x00", line.strip())
        if not match:
            return None
        return fences[int(match.group(1))]

    while i < len(lines):
        line = lines[i]
        fenced = fence_line(line)
        if fenced is not None:
            parts.append(fenced)
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        if HR_RE.match(line):
            parts.append("<hr>")
            i += 1
            continue
        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            parts.append(f"<h{level}>{apply_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue
        if BLOCKQUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines):
                qm = BLOCKQUOTE_RE.match(lines[i])
                if not qm:
                    break
                quoted.append(qm.group(1))
                i += 1
            inner = apply_inline(" ".join(quoted).strip()) if quoted else ""
            parts.append(f"<blockquote>{inner}</blockquote>")
            continue
        if UL_RE.match(line):
            items: list[str] = []
            while i < len(lines):
                um = UL_RE.match(lines[i])
                if not um:
                    break
                items.append(f"<li>{apply_inline(um.group(2).strip())}</li>")
                i += 1
            parts.append(f"<ul>{''.join(items)}</ul>")
            continue
        if OL_RE.match(line):
            items = []
            while i < len(lines):
                om = OL_RE.match(lines[i])
                if not om:
                    break
                items.append(f"<li>{apply_inline(om.group(2).strip())}</li>")
                i += 1
            parts.append(f"<ol>{''.join(items)}</ol>")
            continue
        para: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if (
                not nxt.strip()
                or fence_line(nxt)
                or HR_RE.match(nxt)
                or HEADING_RE.match(nxt)
                or BLOCKQUOTE_RE.match(nxt)
                or UL_RE.match(nxt)
                or OL_RE.match(nxt)
            ):
                break
            para.append(nxt.strip())
            i += 1
        if para:
            parts.append(f"<p>{apply_inline(' '.join(para))}</p>")
    return "\n".join(parts)
