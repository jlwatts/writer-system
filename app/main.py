import os
import re
import json
import logging
import asyncio
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, date
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from google import genai
from google.genai import types
from app.todos import parse_todos, serialize_todos, is_todo_channel, strip_todo_command, apply_todo_actions
from app.markdown_preview import render_markdown

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("writer_app")

app = FastAPI(title="Writer Studio")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

VAULTS_DIR = Path(os.getenv("VAULTS_DIR", "./vaults")).resolve()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

client = genai.Client()

# In-memory session store (Persisted per vault in memory / JSON)
# Structure: { vault_name: [ { "id": str, "role": "user"|"model", "text": str, "status": "pending"|"processing"|"completed"|"failed", "timestamp": str } ] }
CHAT_HISTORIES: Dict[str, List[Dict[str, Any]]] = {}
TODO_CHAT_HISTORIES: Dict[str, List[Dict[str, Any]]] = {}
QUEUE_LOCKS: Dict[str, asyncio.Lock] = {}

def get_vault_lock(vault: str) -> asyncio.Lock:
    if vault not in QUEUE_LOCKS:
        QUEUE_LOCKS[vault] = asyncio.Lock()
    return QUEUE_LOCKS[vault]

def clean_json_response(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return json.loads(cleaned.strip())

class ChatEnqueueRequest(BaseModel):
    prompt: str
    vault: str = "default"
    file_path: Optional[str] = None
    file_content: Optional[str] = None
    channel: Optional[str] = None
    tab: Optional[str] = None

class ClearHistoryRequest(BaseModel):
    vault: str = "default"
    channel: str = "writing"

class FileSaveRequest(BaseModel):
    vault: str = "default"
    file_path: str
    content: str = ""

class FileCreateRequest(BaseModel):
    vault: str = "default"
    folder: str = ""
    name: Optional[str] = None
    content: str = ""
    conflict: Optional[Literal["replace", "keep"]] = None

class FilePathRequest(BaseModel):
    vault: str = "default"
    path: str

class FileRenameRequest(BaseModel):
    vault: str = "default"
    path: str
    new_name: str
    conflict: Optional[Literal["replace", "keep"]] = None

class FileRelocateRequest(BaseModel):
    vault: str = "default"
    path: str
    destination: str = ""
    conflict: Optional[Literal["replace", "keep"]] = None

class TodoItemModel(BaseModel):
    text: str = ""
    done: bool = False
    due: Optional[str] = None
    resolved: Optional[str] = None

class TodosUpdateRequest(BaseModel):
    vault: str = "default"
    items: List[TodoItemModel]


class PreviewRequest(BaseModel):
    content: str = ""


def sanitize_vault_name(vault: str) -> str:
    name = (vault or "default").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid vault name")
    return name


def ensure_vaults_dir() -> Path:
    VAULTS_DIR.mkdir(parents=True, exist_ok=True)
    return VAULTS_DIR.resolve()


def vault_root(vault: str) -> Path:
    name = sanitize_vault_name(vault)
    base = ensure_vaults_dir()
    root = (base / name).resolve()
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid vault path") from exc
    return root


def resolve_in_vault(vault: str, rel_path: Optional[str]) -> Path:
    if not rel_path or not str(rel_path).strip():
        raise HTTPException(status_code=400, detail="Missing file path")
    raw = str(rel_path).strip().lstrip("/")
    if Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise HTTPException(status_code=400, detail="Invalid file path")
    root = vault_root(vault)
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path escapes vault") from exc
    return candidate


def list_vault_names() -> List[str]:
    base = ensure_vaults_dir()
    vaults = sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    if not vaults:
        (base / "default").mkdir(parents=True, exist_ok=True)
        return ["default"]
    return vaults


def list_vault_tree(vault: str) -> Dict[str, Any]:
    root = vault_root(vault)
    root.mkdir(parents=True, exist_ok=True)
    files: List[str] = []
    folders = [""]
    mtimes: Dict[str, float] = {}
    try:
        mtimes[""] = root.stat().st_mtime
    except OSError:
        mtimes[""] = 0.0
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        key = rel.as_posix()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if path.is_dir():
            folders.append(key)
            mtimes[key] = max(mtimes.get(key, 0.0), mtime)
            continue
        if not path.is_file():
            continue
        files.append(key)
        mtimes[key] = mtime
        parent_parts = list(rel.parts[:-1])
        acc: List[str] = []
        for part in parent_parts:
            acc.append(part)
            folder = "/".join(acc)
            mtimes[folder] = max(mtimes.get(folder, 0.0), mtime)
        mtimes[""] = max(mtimes.get("", 0.0), mtime)
    files.sort()
    folders = sorted(set(folders), key=lambda value: value.lower())
    return {"files": files, "folders": folders, "mtimes": mtimes}


def list_vault_files(vault: str) -> List[str]:
    return list_vault_tree(vault)["files"]


def rel_posix(vault: str, path: Path) -> str:
    return path.relative_to(vault_root(vault)).as_posix()


def folder_path(vault: str, folder: str) -> Path:
    folder = (folder or "").strip().strip("/")
    if not folder:
        root = vault_root(vault)
        root.mkdir(parents=True, exist_ok=True)
        return root
    path = resolve_in_vault(vault, folder)
    if path.exists() and path.is_file():
        raise HTTPException(status_code=400, detail="Destination is a file")
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_vault_folders(vault: str) -> List[str]:
    return list_vault_tree(vault)["folders"]


def sanitize_filename(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid file name")
    return cleaned


def ensure_markdown_name(name: Optional[str]) -> str:
    cleaned = sanitize_filename(name or "untitled.md")
    if not Path(cleaned).suffix:
        cleaned += ".md"
    return cleaned


def next_available_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def resolve_conflict_destination(
    dest: Path,
    conflict: Optional[str],
    source: Optional[Path] = None,
) -> Path:
    if source and dest.resolve() == source.resolve():
        return dest
    if not dest.exists():
        return dest
    if conflict == "replace":
        if dest.is_file():
            dest.unlink()
        else:
            raise HTTPException(status_code=400, detail="Cannot replace a directory")
        return dest
    if conflict == "keep":
        return next_available_path(dest)
    suggested = next_available_path(dest)
    raise HTTPException(
        status_code=409,
        detail={
            "conflict": True,
            "existing": dest.name,
            "suggested": suggested.name,
        },
    )

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    Serves the primary UI interface.
    """
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )

async def process_chat_queue(vault: str, file_path: Optional[str], file_content: Optional[str]) -> Optional[Dict[str, Any]]:
    lock = get_vault_lock(vault)
    async with lock:
        history = CHAT_HISTORIES.get(vault, [])
        pending_items = [msg for msg in history if msg.get("status") == "pending"]
        
        if not pending_items:
            return None

        # Mark pending items as processing
        for item in pending_items:
            item["status"] = "processing"

        # Combine multiple un-sent prompts into a single payload if user spammed messages
        combined_prompts = "\n".join([f"- {msg['text']}" for msg in pending_items])
        
        # Build prior conversation turns for Gemini context
        formatted_history = []
        for msg in history:
            if msg.get("status") == "completed":
                formatted_history.append(f"{msg['role'].upper()}: {msg['text']}")

        doc_context = ""
        if file_content:
            doc_context = f"\n--- ACTIVE DOCUMENT ({file_path or 'Untitled'}) ---\n{file_content}\n--- END DOCUMENT ---\n"

        history_context = "\n".join(formatted_history[-10:]) # Keep last 10 turns
        
        full_prompt = f"""
        You are an intelligent writing co-pilot.
        {doc_context}
        
        --- RECENT CONVERSATION HISTORY ---
        {history_context}
        --- END HISTORY ---

        USER INSTRUCTION(S):
        {combined_prompts}

        Return a valid JSON object with two fields:
        1. "reply": Your conversational response answering the user's latest instruction(s).
        2. "updated_content": Complete revised text for the active document IF an edit was requested, else null.
        """

        try:
            # Run blocking Gemini SDK call in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=full_prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
            )

            data = clean_json_response(response.text)
            reply_text = data.get("reply", "Done.")

            # Update pending items status to completed
            for item in pending_items:
                item["status"] = "completed"

            # Append Assistant response turn
            history.append({
                "id": str(datetime.now().timestamp()),
                "role": "model",
                "text": reply_text,
                "status": "completed",
                "timestamp": datetime.now().isoformat()
            })

            # Save file updates if requested
            updated_content = data.get("updated_content")
            if updated_content and file_path:
                try:
                    file_full_path = resolve_in_vault(vault, file_path)
                    file_full_path.parent.mkdir(parents=True, exist_ok=True)
                    file_full_path.write_text(updated_content, encoding="utf-8")
                except HTTPException:
                    logger.warning("Skipped writing updated_content outside vault: %s/%s", vault, file_path)

            return {"reply": reply_text, "updated_content": updated_content}

        except Exception as e:
            logger.error(f"Queue Execution Error: {e}", exc_info=True)
            for item in pending_items:
                item["status"] = "failed"
            history.append({
                "id": str(datetime.now().timestamp()),
                "role": "model",
                "text": f"Error processing request: {str(e)}",
                "status": "completed",
                "timestamp": datetime.now().isoformat()
            })
            return {"reply": f"Error processing request: {str(e)}", "updated_content": None}


def chat_store(channel: str) -> Dict[str, List[Dict[str, Any]]]:
    return TODO_CHAT_HISTORIES if channel == "todos" else CHAT_HISTORIES


def resolve_chat_channel(req: ChatEnqueueRequest) -> tuple[str, str]:
    prompt = req.prompt or ""
    if is_todo_channel(prompt, tab=req.tab or "", channel=req.channel or ""):
        return "todos", strip_todo_command(prompt)
    return "writing", prompt


def read_todo_state(vault: str) -> tuple[List[Dict[str, Any]], str, str]:
    path = resolve_in_vault(vault, TODO_FILENAME)
    if not path.is_file():
        return [], "", ""
    parsed = parse_todos(path.read_text(encoding="utf-8"))
    return parsed["items"], parsed["preamble"], parsed["trailer"]


def write_todo_state(vault: str, items: List[Dict[str, Any]], preamble: str, trailer: str) -> None:
    path = resolve_in_vault(vault, TODO_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_todos(items, preamble, trailer), encoding="utf-8")


def format_todo_digest(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "(empty list)"
    lines = []
    for i, item in enumerate(items, start=1):
        mark = "x" if item.get("done") else " "
        due = f" due:{item['due']}" if item.get("due") else ""
        resolved = f" resolved:{item['resolved']}" if item.get("resolved") else ""
        lines.append(f"{i}. [{mark}] {item.get('text', '')}{due}{resolved}")
    return "\n".join(lines)


async def process_todo_queue(vault: str) -> Optional[Dict[str, Any]]:
    lock = get_vault_lock(f"{vault}:todos")
    async with lock:
        history = TODO_CHAT_HISTORIES.get(vault, [])
        pending_items = [msg for msg in history if msg.get("status") == "pending"]
        if not pending_items:
            return None
        for item in pending_items:
            item["status"] = "processing"

        combined_prompts = "\n".join([f"- {msg['text']}" for msg in pending_items])
        formatted_history = [
            f"{msg['role'].upper()}: {msg['text']}"
            for msg in history
            if msg.get("status") == "completed"
        ]
        items, preamble, trailer = read_todo_state(vault)
        digest = format_todo_digest(items)
        full_prompt = f"""
You are Guppy, a concise to-do list manager for a writer.
You manage ONLY the vault TODO.md list. Do not edit story manuscripts.
Today's date is {date.today().isoformat()}.

CURRENT TODO LIST (1-based numbers):
{digest}

--- RECENT TODO CONVERSATION ---
{chr(10).join(formatted_history[-10:])}
--- END HISTORY ---

USER INSTRUCTION(S):
{combined_prompts}

Return valid JSON with:
1. "reply": A helpful conversational answer. You may summarize, group items, suggest priorities, or confirm changes.
2. "actions": An array of mutations to apply, or [] if the user only asked a question.
   Each action is an object:
   - {{"op":"add","text":"..."}}
   - {{"op":"complete","index":2}} or {{"op":"complete","match":"..."}}
   - {{"op":"uncomplete","index":2}}
   - {{"op":"move","index":4,"to":1}} or {{"op":"move","match":"...","position":"top"|"bottom"}}
   - {{"op":"downgrade","index":3}} or {{"op":"downgrade","match":"partial text"}} (moves the item to the bottom)
   - {{"op":"update","match":"old","new_text":"new"}}
   - {{"op":"set_due","index":2,"due":"2026-09-20"}} or {{"op":"set_due","match":"...","due":null}} to clear
   Completing an item stamps resolved:YYYY-MM-DD as today automatically. Reopening clears resolved.
   Due dates MUST be ISO YYYY-MM-DD and must be today or later. Never set a past due date.
   When the user gives a relative date, convert it using today.
   NEVER delete items. If the user asks to delete or remove something, downgrade it to the bottom and say you demoted it instead.
Indexes are 1-based. Use match when the user describes an item instead of numbering it.
"""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=full_prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
            )
            data = clean_json_response(response.text)
            reply_text = data.get("reply", "Done.")
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                actions = []
            new_items, applied = apply_todo_actions(items, actions)
            if applied:
                write_todo_state(vault, new_items, preamble, trailer)
            else:
                new_items = items
            for item in pending_items:
                item["status"] = "completed"
            history.append({
                "id": str(datetime.now().timestamp()),
                "role": "model",
                "text": reply_text,
                "status": "completed",
                "timestamp": datetime.now().isoformat()
            })
            return {"reply": reply_text, "items": new_items, "actions": applied}
        except Exception as e:
            logger.error(f"Todo queue error: {e}", exc_info=True)
            for item in pending_items:
                item["status"] = "failed"
            history.append({
                "id": str(datetime.now().timestamp()),
                "role": "model",
                "text": f"Error processing request: {str(e)}",
                "status": "completed",
                "timestamp": datetime.now().isoformat()
            })
            return {"reply": f"Error processing request: {str(e)}", "items": items, "actions": []}


def _queue_user_message(req: ChatEnqueueRequest, channel: str = "writing") -> tuple[str, dict]:
    vault = req.vault or "default"
    store = chat_store(channel)
    if vault not in store:
        store[vault] = []

    user_msg = {
        "id": str(datetime.now().timestamp()),
        "role": "user",
        "text": req.prompt,
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    store[vault].append(user_msg)
    return vault, user_msg


@app.get("/api/vaults")
def get_vaults():
    return {"vaults": list_vault_names()}


@app.get("/api/files")
def get_files(vault: str = "default"):
    listing = list_vault_tree(vault)
    return {
        "vault": sanitize_vault_name(vault),
        "files": listing["files"],
        "folders": listing["folders"],
        "mtimes": listing["mtimes"],
    }


@app.get("/api/file")
def get_file(vault: str = "default", path: Optional[str] = None, filename: Optional[str] = None):
    rel = path or filename
    file_full_path = resolve_in_vault(vault, rel)
    if not file_full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return {
        "vault": sanitize_vault_name(vault),
        "filename": file_full_path.relative_to(vault_root(vault)).as_posix(),
        "content": file_full_path.read_text(encoding="utf-8"),
    }


@app.post("/api/file")
def save_file(req: FileSaveRequest):
    file_full_path = resolve_in_vault(req.vault, req.file_path)
    file_full_path.parent.mkdir(parents=True, exist_ok=True)
    file_full_path.write_text(req.content, encoding="utf-8")
    return {
        "status": "saved",
        "vault": sanitize_vault_name(req.vault),
        "filename": file_full_path.relative_to(vault_root(req.vault)).as_posix(),
    }


TODO_FILENAME = "TODO.md"


@app.post("/api/preview")
def preview_markdown(req: PreviewRequest):
    return {"html": render_markdown(req.content)}


@app.get("/api/todos")
def get_todos(vault: str = "default"):
    path = resolve_in_vault(vault, TODO_FILENAME)
    if not path.is_file():
        return {
            "vault": sanitize_vault_name(vault),
            "exists": False,
            "items": [],
        }
    parsed = parse_todos(path.read_text(encoding="utf-8"))
    return {
        "vault": sanitize_vault_name(vault),
        "exists": True,
        "items": parsed["items"],
    }


@app.put("/api/todos")
def put_todos(req: TodosUpdateRequest):
    path = resolve_in_vault(req.vault, TODO_FILENAME)
    preamble = ""
    trailer = ""
    if path.is_file():
        parsed = parse_todos(path.read_text(encoding="utf-8"))
        preamble = parsed["preamble"]
        trailer = parsed["trailer"]
    items = [item.model_dump() for item in req.items]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_todos(items, preamble, trailer), encoding="utf-8")
    return {
        "status": "saved",
        "vault": sanitize_vault_name(req.vault),
        "exists": True,
        "items": items,
    }


@app.post("/api/file/create")
def create_file(req: FileCreateRequest):
    dest_dir = folder_path(req.vault, req.folder)
    name = ensure_markdown_name(req.name)
    dest = dest_dir / name
    if dest.exists():
        if req.name:
            dest = resolve_conflict_destination(dest, req.conflict)
        else:
            dest = next_available_path(dest)
    dest = resolve_in_vault(req.vault, rel_posix(req.vault, dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(req.content or "", encoding="utf-8")
    return {
        "status": "created",
        "vault": sanitize_vault_name(req.vault),
        "filename": rel_posix(req.vault, dest),
        "content": req.content or "",
    }


@app.post("/api/file/rename")
def rename_file(req: FileRenameRequest):
    source = resolve_in_vault(req.vault, req.path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if source.is_dir():
        new_name = sanitize_filename(req.new_name)
        kind = "folder"
    else:
        if not source.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        new_name = ensure_markdown_name(req.new_name)
        kind = "file"
    dest = resolve_conflict_destination(source.parent / new_name, req.conflict, source)
    dest = resolve_in_vault(req.vault, rel_posix(req.vault, dest))
    if dest.resolve() != source.resolve():
        source.rename(dest)
    return {
        "status": "renamed",
        "vault": sanitize_vault_name(req.vault),
        "kind": kind,
        "filename": rel_posix(req.vault, dest),
    }


@app.post("/api/file/move")
def move_file(req: FileRelocateRequest):
    source = resolve_in_vault(req.vault, req.path)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    dest_dir = folder_path(req.vault, req.destination)
    dest = resolve_conflict_destination(dest_dir / source.name, req.conflict, source)
    dest = resolve_in_vault(req.vault, rel_posix(req.vault, dest))
    if dest.resolve() != source.resolve():
        shutil.move(str(source), str(dest))
    return {
        "status": "moved",
        "vault": sanitize_vault_name(req.vault),
        "filename": rel_posix(req.vault, dest),
    }


@app.post("/api/file/copy")
def copy_file(req: FileRelocateRequest):
    source = resolve_in_vault(req.vault, req.path)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    dest_dir = folder_path(req.vault, req.destination)
    dest = dest_dir / source.name
    if dest.resolve() == source.resolve():
        dest = next_available_path(dest)
    else:
        dest = resolve_conflict_destination(dest, req.conflict, source)
    dest = resolve_in_vault(req.vault, rel_posix(req.vault, dest))
    shutil.copy2(source, dest)
    return {
        "status": "copied",
        "vault": sanitize_vault_name(req.vault),
        "filename": rel_posix(req.vault, dest),
    }


@app.delete("/api/file")
def delete_file(vault: str = "default", path: Optional[str] = None):
    if not path:
        raise HTTPException(status_code=400, detail="Missing file path")
    target = resolve_in_vault(vault, path)
    if target.resolve() == vault_root(vault).resolve():
        raise HTTPException(status_code=400, detail="Cannot delete vault root")
    if target.is_dir():
        shutil.rmtree(target)
        kind = "folder"
    elif target.is_file():
        target.unlink()
        kind = "file"
    else:
        raise HTTPException(status_code=404, detail="Path not found")
    return {
        "status": "deleted",
        "vault": sanitize_vault_name(vault),
        "kind": kind,
        "filename": path,
    }


@app.post("/api/chat/enqueue")
async def enqueue_chat(req: ChatEnqueueRequest, background_tasks: BackgroundTasks):
    channel, prompt = resolve_chat_channel(req)
    req.prompt = prompt
    vault, user_msg = _queue_user_message(req, channel=channel)
    if channel == "todos":
        background_tasks.add_task(process_todo_queue, vault)
    else:
        background_tasks.add_task(process_chat_queue, vault, req.file_path, req.file_content)
    return {"status": "queued", "channel": channel, "message": user_msg}


@app.post("/api/chat")
async def chat(req: ChatEnqueueRequest):
    channel, prompt = resolve_chat_channel(req)
    req.prompt = prompt
    if channel == "todos":
        vault, _user_msg = _queue_user_message(req, channel="todos")
        result = await process_todo_queue(vault) or {}
        reply = result.get("reply", "Done.")
        return {
            "response": reply,
            "message": reply,
            "channel": "todos",
            "items": result.get("items"),
            "actions": result.get("actions") or [],
            "updated_content": None,
        }
    vault, _user_msg = _queue_user_message(req, channel="writing")
    result = await process_chat_queue(vault, req.file_path, req.file_content) or {}
    reply = result.get("reply", "Done.")
    return {
        "response": reply,
        "message": reply,
        "channel": "writing",
        "updated_content": result.get("updated_content"),
    }


@app.get("/api/chat/history")
def get_chat_history(vault: str = "default", channel: str = "writing"):
    store = chat_store("todos" if channel in {"todos", "todo", "guppy"} else "writing")
    return {"channel": "todos" if store is TODO_CHAT_HISTORIES else "writing", "history": store.get(vault, [])}


@app.post("/api/chat/clear")
def clear_chat_history(req: ClearHistoryRequest):
    vault = req.vault or "default"
    channel = "todos" if (req.channel or "writing") in {"todos", "todo", "guppy"} else "writing"
    chat_store(channel)[vault] = []
    return {"status": "cleared", "vault": vault, "channel": channel}
