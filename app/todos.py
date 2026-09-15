import re
from datetime import date
from typing import Any, Dict, List

ITEM_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s*(.*)$")
META_RE = re.compile(r"\s+(due|resolved):(\d{4}-\d{2}-\d{2})\s*$", re.IGNORECASE)


def normalize_due(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.group(1).split("-"))
    try:
        date(year, month, day)
    except ValueError:
        return None
    return match.group(1)


def split_item_meta(raw_text: str) -> tuple[str, Dict[str, str | None]]:
    text = (raw_text or "").rstrip()
    meta: Dict[str, str | None] = {"due": None, "resolved": None}
    while True:
        match = META_RE.search(text)
        if not match:
            break
        meta[match.group(1).lower()] = normalize_due(match.group(2))
        text = text[: match.start()].rstrip()
    return text, meta


def format_item_line(item: Dict[str, Any]) -> str:
    mark = "x" if item.get("done") else " "
    text = str(item.get("text") or "").rstrip()
    due = normalize_due(item.get("due"))
    resolved = normalize_due(item.get("resolved"))
    if due:
        text = f"{text} due:{due}".strip()
    if resolved:
        text = f"{text} resolved:{resolved}".strip()
    return f"- [{mark}] {text}".rstrip()


def parse_todos(markdown: str) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    preamble_lines: List[str] = []
    trailer_lines: List[str] = []
    seen_item = False
    for line in (markdown or "").splitlines():
        match = ITEM_RE.match(line)
        if match:
            seen_item = True
            body, meta = split_item_meta(match.group(2).rstrip())
            items.append({
                "text": body,
                "done": match.group(1).lower() == "x",
                "due": meta["due"],
                "resolved": meta["resolved"],
            })
        elif not seen_item:
            preamble_lines.append(line)
        else:
            trailer_lines.append(line)
    return {
        "items": items,
        "preamble": "\n".join(preamble_lines),
        "trailer": "\n".join(trailer_lines),
    }


def serialize_todos(
    items: List[Dict[str, Any]],
    preamble: str = "",
    trailer: str = "",
) -> str:
    lines: List[str] = []
    if preamble.strip():
        lines.append(preamble.rstrip("\n"))
    for item in items:
        lines.append(format_item_line(item))
    if trailer.strip():
        lines.append(trailer.rstrip("\n"))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


TODO_INTENT_RE = re.compile(r"(?i)(?:^|\s)(/todo\b|guppy\b|to-?dos?\b)")


def strip_todo_command(prompt: str) -> str:
    text = (prompt or "").strip()
    lowered = text.lower()
    if lowered.startswith("/todo"):
        rest = text[5:].lstrip()
        if rest.startswith(":"):
            rest = rest[1:].lstrip()
        return rest or text
    return text


def is_todo_channel(prompt: str, tab: str = "", channel: str = "") -> bool:
    ch = (channel or "").strip().lower()
    if ch in {"todos", "todo", "guppy"}:
        return True
    if ch in {"writing", "write"}:
        return False
    if (tab or "").strip().lower() == "todos":
        return True
    return bool(TODO_INTENT_RE.search(prompt or ""))


def find_item_index(items: List[Dict[str, Any]], action: Dict[str, Any]) -> int | None:
    raw_index = action.get("index")
    if raw_index is not None and raw_index != "":
        try:
            idx = int(raw_index)
        except (TypeError, ValueError):
            idx = None
        if idx is not None:
            if 1 <= idx <= len(items):
                return idx - 1
            if 0 <= idx < len(items):
                return idx
    needle = str(action.get("match") or "").strip().lower()
    if not needle:
        needle = str(action.get("text") or "").strip().lower() if action.get("op") not in {"add"} else ""
    if not needle:
        return None
    exact = [i for i, item in enumerate(items) if str(item.get("text") or "").strip().lower() == needle]
    if exact:
        return exact[0]
    partial = [i for i, item in enumerate(items) if needle in str(item.get("text") or "").lower()]
    return partial[0] if partial else None


def allowed_due(value: Any, today_iso: str) -> str | None:
    due = normalize_due(value)
    if not due or due < today_iso:
        return None
    return due


def apply_todo_actions(
    items: List[Dict[str, Any]],
    actions: List[Dict[str, Any]] | None,
    today: str | None = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    today_iso = normalize_due(today) or date.today().isoformat()
    updated = [{
        "text": str(item.get("text") or ""),
        "done": bool(item.get("done")),
        "due": normalize_due(item.get("due")),
        "resolved": normalize_due(item.get("resolved")),
    } for item in items]
    applied: List[Dict[str, Any]] = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        op = str(action.get("op") or action.get("action") or "").strip().lower()
        if op == "add":
            text = str(action.get("text") or "").strip()
            if text:
                done = bool(action.get("done", False))
                due = allowed_due(action.get("due"), today_iso)
                resolved = today_iso if done else None
                updated.append({"text": text, "done": done, "due": due, "resolved": resolved})
                applied.append({"op": "add", "text": text, "due": due})
        elif op in {"delete", "remove", "downgrade"}:
            idx = find_item_index(updated, action)
            if idx is None:
                continue
            item = updated.pop(idx)
            updated.append(item)
            applied.append({"op": "downgrade", "text": item["text"], "to": len(updated)})
        elif op in {"complete", "check", "done"}:
            idx = find_item_index(updated, action)
            if idx is not None:
                updated[idx]["done"] = True
                updated[idx]["resolved"] = today_iso
                applied.append({"op": "complete", "text": updated[idx]["text"], "resolved": today_iso})
        elif op in {"uncomplete", "uncheck", "undone", "reopen"}:
            idx = find_item_index(updated, action)
            if idx is not None:
                updated[idx]["done"] = False
                updated[idx]["resolved"] = None
                applied.append({"op": "uncomplete", "text": updated[idx]["text"]})
        elif op in {"update", "edit"}:
            idx = find_item_index(updated, {
                "index": action.get("index"),
                "match": action.get("match"),
                "text": action.get("match"),
                "op": "update",
            })
            new_text = str(action.get("new_text") or "").strip()
            if not new_text and action.get("match"):
                new_text = str(action.get("text") or "").strip()
            if idx is not None and new_text:
                updated[idx]["text"] = new_text
                applied.append({"op": "update", "text": new_text})
        elif op == "move":
            idx = find_item_index(updated, action)
            if idx is None:
                continue
            item = updated.pop(idx)
            position = str(action.get("position") or "").strip().lower()
            dest = len(updated)
            if position == "top":
                dest = 0
            elif position == "bottom":
                dest = len(updated)
            elif action.get("to") is not None and action.get("to") != "":
                try:
                    dest = int(action["to"])
                    if dest >= 1:
                        dest -= 1
                except (TypeError, ValueError):
                    dest = idx
            dest = max(0, min(len(updated), dest))
            updated.insert(dest, item)
            applied.append({"op": "move", "text": item["text"], "to": dest + 1})
        elif op in {"set_due", "due"}:
            idx = find_item_index(updated, action)
            if idx is None:
                continue
            if action.get("due") in (None, ""):
                updated[idx]["due"] = None
                applied.append({"op": "set_due", "text": updated[idx]["text"], "due": None})
                continue
            due = allowed_due(action.get("due"), today_iso)
            if not due:
                continue
            updated[idx]["due"] = due
            applied.append({"op": "set_due", "text": updated[idx]["text"], "due": due})
    return updated, applied
