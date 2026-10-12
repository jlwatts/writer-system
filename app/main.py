import os
import re
import json
import time
import logging
import asyncio
import shutil
import hashlib
import secrets
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, date
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from google import genai
from google.genai import types
from app.todos import parse_todos, serialize_todos, is_todo_channel, strip_todo_command, apply_todo_actions
from app.markdown_preview import render_markdown
from app.auth import (
    apply_session_cookie,
    change_password,
    check_password,
    clear_session_cookie,
    install as install_auth,
    password_is_set,
    session_ok,
    set_password,
    COOKIE_NAME,
)
from app.history import (
    file_history,
    read_file_at,
    restore_file,
    set_file_label,
    snapshot_all,
    snapshot_interval_seconds,
    vault_lock,
    purge_vault_history,
)
from app.encryption import (
    PREFIX as ENCRYPTED_PREFIX,
    atomic_write_text,
    clear_conversion_pending,
    envelope_file_id,
    file_revision,
    is_encrypted_path,
    is_encrypted_text,
    mark_conversion_pending,
    parse_envelope,
    pending_conversions,
    register_verifier,
    remove_identity_if_unused,
    report_unlock_failure,
    report_unlock_success,
    throttle_status,
    verify_deletion,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("writer_app")


async def _snapshot_loop() -> None:
    if os.getenv("SNAPSHOTS_ENABLED", "1") == "0":
        return
    while True:
        try:
            await asyncio.to_thread(snapshot_all)
        except Exception:
            logger.exception("Vault snapshot failed")
        await asyncio.sleep(snapshot_interval_seconds())


def chat_store_path() -> Path:
    from app import auth as auth_mod

    return Path(auth_mod.AUTH_DIR) / "chat.json"


def load_chat_histories() -> None:
    path = chat_store_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read chat history")
        return
    if not isinstance(data, dict):
        return
    writing = data.get("writing")
    todos = data.get("todos")
    CHAT_HISTORIES.clear()
    TODO_CHAT_HISTORIES.clear()
    if isinstance(writing, dict):
        CHAT_HISTORIES.update({str(key): value for key, value in writing.items() if isinstance(value, list)})
    if isinstance(todos, dict):
        TODO_CHAT_HISTORIES.update({str(key): value for key, value in todos.items() if isinstance(value, list)})


def save_chat_histories() -> None:
    path = chat_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"writing": CHAT_HISTORIES, "todos": TODO_CHAT_HISTORIES})
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def prompt_files_store_path() -> Path:
    from app import auth as auth_mod

    return Path(auth_mod.AUTH_DIR) / "prompt_files.json"


def load_prompt_files() -> None:
    PROMPT_FILE_SELECTIONS.clear()
    path = prompt_files_store_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read prompt files")
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            PROMPT_FILE_SELECTIONS[str(key)] = list(value)


def save_prompt_files() -> None:
    path = prompt_files_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(PROMPT_FILE_SELECTIONS)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    load_chat_histories()
    load_prompt_files()
    recover_pending_conversions()
    task = asyncio.create_task(_snapshot_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Writer Studio", lifespan=_lifespan)


class NoCacheStaticFiles(StaticFiles):
    """Static files that browsers must re-fetch, so UI edits land immediately."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


STATIC_DIR = Path("app/static")
SERVER_START = str(int(time.time()))


def asset_version() -> str:
    """Newest static-file mtime, so edited CSS/JS always gets a fresh URL."""
    try:
        return str(int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file())))
    except ValueError:
        return SERVER_START


app.mount("/static", NoCacheStaticFiles(directory="app/static"), name="static")
install_auth(app)
templates = Jinja2Templates(directory="app/templates")

VAULTS_DIR = Path(os.getenv("VAULTS_DIR", "./vaults")).resolve()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

client = genai.Client()


def gemini_json_config():
    """JSON answers only. Automatic function calling is not used and must stay off."""
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # `store` is already supported by the REST API but is not yet a typed
        # field in every google-genai 2.x model. Keep the opt-out explicit.
        http_options=types.HttpOptions(extra_body={"store": False}),
    )

# In-memory session store (Persisted per vault in memory / JSON)
# Structure: { vault_name: [ { "id": str, "role": "user"|"model", "text": str, "status": "pending"|"processing"|"completed"|"failed", "timestamp": str } ] }
CHAT_HISTORIES: Dict[str, List[Dict[str, Any]]] = {}
TODO_CHAT_HISTORIES: Dict[str, List[Dict[str, Any]]] = {}
# Vaults absent from this map still use SKILL.md. An empty list is an explicit clear.
PROMPT_FILE_SELECTIONS: Dict[str, List[str]] = {}
QUEUE_LOCKS: Dict[str, asyncio.Lock] = {}
DELETE_MANIFESTS: Dict[str, Dict[str, Any]] = {}
DELETE_MANIFESTS_LOCK = threading.Lock()

def get_vault_lock(vault: str) -> asyncio.Lock:
    if vault not in QUEUE_LOCKS:
        QUEUE_LOCKS[vault] = asyncio.Lock()
    return QUEUE_LOCKS[vault]

_JSON_DECODER = json.JSONDecoder(strict=False)
_CUT_OFF_NOTE = "(The response was cut off, so no changes were saved.)"


def _raw_json_object(payload: str):
    try:
        data, _end = _JSON_DECODER.raw_decode(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    return data if isinstance(data, dict) else None


def _skip_ws_back(text: str, index: int) -> int:
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    return index


def _escaped_by_backslash(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _has_later_quote(text: str, index: int) -> bool:
    cursor = index + 1
    limit = len(text)
    while cursor < limit:
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == '"':
            return True
        cursor += 1
    return False


def _escape_quote_at(text: str, index: int):
    """Escape a quote that closed a JSON string in the middle of prose."""
    if index < 0 or index >= len(text) or text[index] != '"':
        return None
    if _escaped_by_backslash(text, index):
        return None
    if not _has_later_quote(text, index):
        return None
    return text[:index] + '\\"' + text[index + 1:]


def _needs_comma_before(text: str, pos: int) -> bool:
    """Return whether `pos` starts a JSON value that is missing its comma."""
    if pos >= len(text):
        return False
    rest = text[pos:]
    if re.match(r'"[A-Za-z_][A-Za-z0-9_]*"\s*:', rest):
        return True
    if rest[0] in "{[":
        return True
    if rest[0] == "-" or rest[0].isdigit():
        cursor = 1
        while cursor < len(rest) and (rest[cursor].isdigit() or rest[cursor] in ".eE+-"):
            cursor += 1
    else:
        word = next((item for item in ("null", "true", "false") if rest.startswith(item)), None)
        if word is None:
            return False
        cursor = len(word)
    while cursor < len(rest) and rest[cursor] in " \t\r\n":
        cursor += 1
    return cursor >= len(rest) or rest[cursor] in ",}]"


def _repair_json_once(text: str, err: json.JSONDecodeError):
    pos = min(err.pos, len(text))
    msg = err.msg
    if msg.startswith("Expecting ',' delimiter"):
        if pos >= len(text):
            return text + "}"
        if _needs_comma_before(text, pos):
            return text[:pos] + "," + text[pos:]
        quote_at = _skip_ws_back(text, pos - 1)
        if quote_at >= 0 and text[quote_at] == '"':
            nxt = text[quote_at + 1] if quote_at + 1 < len(text) else ""
            if nxt not in ",}]":
                return _escape_quote_at(text, quote_at)
        return None
    if "trailing comma" in msg:
        comma_at = pos if pos < len(text) and text[pos] == "," else _skip_ws_back(text, pos - 1)
        if comma_at >= 0 and text[comma_at] == ",":
            return text[:comma_at] + text[comma_at + 1:]
        return None
    if msg.startswith("Expecting property name"):
        cursor = pos
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor < len(text) and text[cursor] in "}]":
            comma_at = _skip_ws_back(text, cursor - 1)
            if comma_at >= 0 and text[comma_at] == ",":
                return text[:comma_at] + text[comma_at + 1:]
        bare_key = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", text[cursor:])
        if bare_key:
            name = bare_key.group(1)
            return text[:cursor] + '"' + name + '"' + text[cursor + len(name):]
        previous = _skip_ws_back(text, pos - 1)
        if previous >= 0 and text[previous] == ",":
            quote_at = _skip_ws_back(text, previous - 1)
            if quote_at >= 0 and text[quote_at] == '"':
                if not re.match(r'"[A-Za-z0-9_]+"\s*:', text[cursor:]):
                    return _escape_quote_at(text, quote_at)
        return None
    if msg.startswith("Invalid \\escape") or msg.startswith("Invalid \\u"):
        slash_at = pos if pos < len(text) and text[pos] == "\\" else pos - 1
        if 0 <= slash_at < len(text) and text[slash_at] == "\\":
            return text[:slash_at] + "\\\\" + text[slash_at + 1:]
        return None
    if msg.startswith("Unterminated string"):
        return text + '"'
    if msg.startswith("Expecting value") and pos >= len(text):
        return text + "null"
    return None


def _repair_json_text(payload: str):
    text = payload
    seen = set()
    truncated = False
    first_error = None
    limit = max(200, payload.count('"') + 20)
    for _ in range(limit):
        try:
            _JSON_DECODER.raw_decode(text)
            return text, truncated, first_error
        except json.JSONDecodeError as err:
            if first_error is None:
                first_error = err
            state = (err.pos, err.msg, len(text))
            if state in seen:
                return None, truncated, first_error
            seen.add(state)
            if err.msg.startswith("Unterminated string"):
                truncated = True
            fixed = _repair_json_once(text, err)
            if not fixed or fixed == text:
                return None, truncated, err
            text = fixed
    return None, truncated, first_error


def clean_json_response(raw_text: str | None) -> dict:
    """Parse Gemini JSON, including prose quotes and other small syntax slips."""
    if raw_text is None:
        raise json.JSONDecodeError("Empty model response", "", 0)
    cleaned = str(raw_text).strip().lstrip("\ufeff")
    if not cleaned:
        raise json.JSONDecodeError("Empty model response", "", 0)

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[^\n]*\n?", "", cleaned)
        cleaned = re.sub(r"\n```(?:[^\n]*)?\s*$", "", cleaned)

    start = next((i for i, ch in enumerate(cleaned) if ch in "{["), None)
    if start is None:
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)

    payload = cleaned[start:]
    data = _raw_json_object(payload)
    truncated = False
    if data is None:
        repaired, truncated, err = _repair_json_text(payload)
        data = _raw_json_object(repaired) if repaired is not None else None
        if data is None:
            if isinstance(err, json.JSONDecodeError):
                logger.error("Unparseable model JSON (%s)", err)
                raise err
            raise json.JSONDecodeError("Could not parse model JSON", payload, 0)
        logger.warning("Repaired malformed model JSON (%s)", err)

    if truncated:
        if re.search(r'"updated_content"\s*:\s*"', payload):
            data["updated_content"] = None
        data["actions"] = []
        reply = data.get("reply") if isinstance(data.get("reply"), str) else ""
        if _CUT_OFF_NOTE not in reply:
            data["reply"] = (reply + "\n\n" + _CUT_OFF_NOTE).strip()
    return data

class ChatEnqueueRequest(BaseModel):
    prompt: str
    vault: str = "default"
    file_path: Optional[str] = None
    file_content: Optional[str] = None
    channel: Optional[str] = None
    tab: Optional[str] = None
    context_files: Optional[List[str]] = None
    allow_file_write: bool = True
    ephemeral: bool = False
    ephemeral_history: Optional[List[Dict[str, str]]] = None


class PromptFilesUpdateRequest(BaseModel):
    vault: str = "default"
    selected: List[str] = []

class ClearHistoryRequest(BaseModel):
    vault: str = "default"
    channel: str = "writing"

class FileSaveRequest(BaseModel):
    vault: str = "default"
    file_path: str
    content: str = ""
    expected_revision: Optional[str] = None

class FileCreateRequest(BaseModel):
    vault: str = "default"
    folder: str = ""
    name: Optional[str] = None
    content: str = ""
    conflict: Optional[Literal["replace", "keep"]] = None
    verifier: Optional[str] = None


class FileConvertRequest(BaseModel):
    vault: str = "default"
    file_path: str
    content: str
    verifier: str
    confirmed: bool = False


class FileDecryptRequest(BaseModel):
    vault: str = "default"
    file_path: str
    content: str
    expected_revision: Optional[str] = None
    confirmed: bool = False


class UnlockReportRequest(BaseModel):
    vault: str = "default"
    file_id: str
    proof: Optional[str] = None


class DeleteManifestRequest(BaseModel):
    vault: str = "default"
    path: str


class ConfirmDeleteRequest(BaseModel):
    vault: str = "default"
    path: str
    login_password: str
    manifest: str
    proofs: Dict[str, str] = {}

class FolderCreateRequest(BaseModel):
    vault: str = "default"
    folder: str = ""
    name: Optional[str] = None
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


class PasswordChangeRequest(BaseModel):
    current: str
    next: str
    confirm: str = ""


class HistoryLabelRequest(BaseModel):
    vault: str
    path: str
    rev: str
    name: str = ""


class HistoryRestoreRequest(BaseModel):
    vault: str
    path: str
    rev: str


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


def _delete_syncthing_versions(vault: str, rel_path: str) -> None:
    root = vault_root(vault)
    versions = root / ".stversions"
    if not versions.is_dir():
        return
    relative = Path(rel_path)
    parent = versions.joinpath(*relative.parts[:-1])
    if not parent.is_dir():
        return
    stem = relative.stem
    suffix = relative.suffix
    for candidate in parent.iterdir():
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if candidate.name == relative.name or (
            candidate.name.startswith(f"{stem}~") and candidate.name.endswith(suffix)
        ):
            candidate.unlink()


def finish_conversion_cleanup(vault: str, rel_path: str) -> None:
    purge_vault_history(vault)
    _delete_syncthing_versions(vault, rel_path)
    clear_conversion_pending(vault)


def recover_pending_conversions() -> None:
    for vault, item in pending_conversions().items():
        path = item.get("path") if isinstance(item, dict) else None
        if not path:
            continue
        try:
            with vault_lock(vault):
                finish_conversion_cleanup(vault, path)
        except Exception:
            logger.exception("Could not finish encrypted conversion cleanup for %s", vault)


def _path_manifest(vault: str, rel_path: str) -> Dict[str, Any]:
    root = vault_root(vault)
    target = resolve_in_vault(vault, rel_path)
    if target.resolve() == root.resolve() or not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    paths = [target] if target.is_file() else sorted(path for path in target.rglob("*") if path.is_file())
    entries = []
    encrypted_ids = set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest = file_revision(path)
        file_id = None
        if is_encrypted_path(path):
            file_id = envelope_file_id(path.read_text(encoding="utf-8"))
            encrypted_ids.add(file_id)
        entries.append({"path": relative, "revision": digest, "file_id": file_id})
    return {
        "vault": sanitize_vault_name(vault),
        "path": target.relative_to(root).as_posix(),
        "entries": entries,
        "encrypted_ids": sorted(encrypted_ids),
    }


def _manifest_matches(manifest: Dict[str, Any]) -> bool:
    current = _path_manifest(str(manifest["vault"]), str(manifest["path"]))
    return current["entries"] == manifest["entries"]


def _vault_has_file_id(vault: str, file_id: str) -> bool:
    root = vault_root(vault)
    for path in root.rglob("*"):
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            if envelope_file_id(path.read_text(encoding="utf-8")) == file_id:
                return True
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return False


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


def normalize_clarification(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    question = raw.strip()
    return question or None


def normalize_new_file(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    raw_name = raw.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raw_name = "extract.md"
    base = Path(raw_name.replace("\\", "/")).name.strip() or "extract.md"
    try:
        name = ensure_markdown_name(base)
    except HTTPException:
        name = "extract.md"
    return {"name": name, "content": content}


def clear_pending_clarification(vault: str) -> None:
    changed = False
    for msg in CHAT_HISTORIES.get(vault, []):
        if msg.get("clarification"):
            msg["clarification"] = False
            changed = True
    if changed:
        save_chat_histories()


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
        raise HTTPException(
            status_code=400,
            detail="Delete the existing path with password confirmation before replacing it",
        )
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

def _login_page(request: Request, setup: bool, error: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"setup": setup, "error": error},
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if password_is_set() and session_ok(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return _login_page(request, setup=not password_is_set())


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm") or "")
    setup = not password_is_set()
    if setup:
        if password != confirm:
            return _login_page(request, setup=True, error="Passwords do not match")
        try:
            set_password(password)
        except ValueError as exc:
            return _login_page(request, setup=True, error=str(exc))
    elif not check_password(password):
        return _login_page(request, setup=False, error="Wrong password")
    response = RedirectResponse("/", status_code=303)
    return apply_session_cookie(response)


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    return clear_session_cookie(response)


@app.post("/api/password")
def update_password(req: PasswordChangeRequest, response: Response):
    if req.next != req.confirm:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    try:
        change_password(req.current, req.next)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    apply_session_cookie(response)
    return {"status": "saved"}


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the primary UI."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"asset_version": asset_version()},
        headers={"Cache-Control": "no-store, max-age=0"},
    )

async def process_chat_queue(
    vault: str,
    file_path: Optional[str],
    file_content: Optional[str],
    context_files: Optional[List[str]] = None,
    allow_file_write: bool = True,
) -> Optional[Dict[str, Any]]:
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

        doc_context = active_document_block(file_path, file_content)
        reference_context = context_prompt_block(vault, context_files, file_path)

        history_context = "\n".join(formatted_history[-10:]) # Keep last 10 turns
        
        lock_note = "" if allow_file_write else "The active document is locked. Leave updated_content null. Do not change that file.\n"
        full_prompt = f"""
        You are an intelligent writing co-pilot.
        Reference files are read-only. Follow a shortcut in SKILL.md or in any included reference file, and do not change those files.
        {lock_note}
        {doc_context}
        {reference_context}
        
        --- RECENT CONVERSATION HISTORY ---
        {history_context}
        --- END HISTORY ---

        USER INSTRUCTION(S):
        {combined_prompts}

        Return a valid JSON object with these fields:
        1. "reply": Your conversational response answering the user's latest instruction(s).
        2. "clarification": One specific question if any part of the instruction is ambiguous, otherwise null. When this is set, put that same question in "reply", and leave "updated_content" and "new_file" null. Do not guess. On the next turn the user's message answers this question; then complete the original request.
        3. "updated_content": Complete revised text for the active document when the user asked to change that file, or when a shortcut in SKILL.md or a reference file says to update it, and no clarification is needed; otherwise null. A shortcut that says not to change the document must leave this null. Extracting into a new file leaves this null unless they also asked to edit the active document.
        4. "new_file": null unless the user asked to extract, split, or save material into another file and no clarification is needed. Then an object {{"name": "scene.md", "content": "the extracted text only"}}.
        """

        try:
            # Run blocking Gemini SDK call in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=full_prompt,
                    config=gemini_json_config(),
                )
            )

            data = clean_json_response(response.text)
            reply_text = data.get("reply", "Done.")
            clarification = normalize_clarification(data.get("clarification"))
            updated_content = data.get("updated_content")
            new_file = normalize_new_file(data.get("new_file"))
            if clarification:
                reply_text = clarification
                updated_content = None
                new_file = None
            elif not document_update_allowed(combined_prompts):
                updated_content = None
            if not allow_file_write:
                updated_content = None

            # Update pending items status to completed
            for item in pending_items:
                item["status"] = "completed"

            # Append Assistant response turn
            history.append({
                "id": str(datetime.now().timestamp()),
                "role": "model",
                "text": reply_text,
                "status": "completed",
                "clarification": bool(clarification),
                "timestamp": datetime.now().isoformat()
            })

            # Save file updates if requested. A clarification pauses every file change.
            if not clarification:
                await asyncio.to_thread(write_updated_document, vault, file_path, updated_content)

            save_chat_histories()
            return {
                "reply": reply_text,
                "updated_content": updated_content,
                "new_file": new_file,
                "clarification": clarification,
            }

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
            save_chat_histories()
            return {
                "reply": f"Error processing request: {str(e)}",
                "updated_content": None,
                "new_file": None,
                "clarification": None,
            }


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
    with vault_lock(vault):
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


async def process_todo_queue(
    vault: str,
    file_path: Optional[str] = None,
    file_content: Optional[str] = None,
    context_files: Optional[List[str]] = None,
    allow_file_write: bool = True,
) -> Optional[Dict[str, Any]]:
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
        doc_context = active_document_block(file_path, file_content)
        reference_context = context_prompt_block(vault, context_files, file_path)
        lock_note = "" if allow_file_write else "The active document is locked. Leave updated_content null. Do not change that file.\n"
        full_prompt = f"""
You are Guppy, a concise to-do list manager for a writer.
You manage the vault TODO.md list. Edit the active document only when a shortcut in SKILL.md or a reference file says to update it.
{lock_note}
Today's date is {date.today().isoformat()}.
{doc_context}
{reference_context}
Reference files are read-only. Follow a shortcut in SKILL.md or in any included reference file, and do not change those files.
If the user invokes a shortcut from SKILL.md or a reference file, answer by following that shortcut against the active document and leave "actions" empty unless they also asked to change the to-do list. When that shortcut says to update the document, set "updated_content" to the complete revised text. When it says not to change the document, leave "updated_content" null.

CURRENT TODO LIST (1-based numbers):
{digest}

--- RECENT TODO CONVERSATION ---
{chr(10).join(formatted_history[-10:])}
--- END HISTORY ---

USER INSTRUCTION(S):
{combined_prompts}

Return valid JSON with:
1. "reply": A helpful conversational answer. You may summarize, group items, suggest priorities, or confirm changes.
2. "updated_content": The complete revised active document when a shortcut in SKILL.md or a reference file says to update it, otherwise null.
3. "actions": An array of mutations to apply, or [] if the user only asked a question.
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
                    config=gemini_json_config(),
                )
            )
            data = clean_json_response(response.text)
            reply_text = data.get("reply", "Done.")
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                actions = []
            updated_content = data.get("updated_content")
            if not document_update_allowed(combined_prompts) or not allow_file_write:
                updated_content = None
            await asyncio.to_thread(write_updated_document, vault, file_path, updated_content)
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
            save_chat_histories()
            return {
                "reply": reply_text,
                "items": new_items,
                "actions": applied,
                "updated_content": updated_content,
            }
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
            save_chat_histories()
            return {"reply": f"Error processing request: {str(e)}", "items": items, "actions": []}


def _queue_user_message(req: ChatEnqueueRequest, channel: str = "writing") -> tuple[str, dict]:
    vault = req.vault or "default"
    store = chat_store(channel)
    if vault not in store:
        store[vault] = []

    if channel == "writing":
        clear_pending_clarification(vault)
    user_msg = {
        "id": str(datetime.now().timestamp()),
        "role": "user",
        "text": req.prompt,
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    store[vault].append(user_msg)
    save_chat_histories()
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
def get_file(response: Response, vault: str = "default", path: Optional[str] = None, filename: Optional[str] = None):
    rel = path or filename
    file_full_path = resolve_in_vault(vault, rel)
    if not file_full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    content = file_full_path.read_text(encoding="utf-8")
    encrypted = is_encrypted_text(content)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return {
        "vault": sanitize_vault_name(vault),
        "filename": file_full_path.relative_to(vault_root(vault)).as_posix(),
        "content": content,
        "encrypted": encrypted,
        "revision": file_revision(file_full_path),
        "file_id": envelope_file_id(content) if encrypted else None,
    }


@app.get("/api/file/download")
def download_file(vault: str = "default", path: Optional[str] = None):
    if not path:
        raise HTTPException(status_code=400, detail="Missing file path")
    target = resolve_in_vault(vault, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media = "text/markdown; charset=utf-8" if target.suffix.lower() == ".md" else "application/octet-stream"
    response = FileResponse(
        path=target,
        filename=target.name,
        media_type=media,
        content_disposition_type="attachment",
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.post("/api/file")
def save_file(req: FileSaveRequest):
    with vault_lock(req.vault):
        file_full_path = resolve_in_vault(req.vault, req.file_path)
        incoming_encrypted = is_encrypted_text(req.content)
        if req.content.startswith(ENCRYPTED_PREFIX) and not incoming_encrypted:
            raise HTTPException(status_code=400, detail="Invalid encrypted file")
        existing_encrypted = is_encrypted_path(file_full_path)
        if not file_full_path.exists() and incoming_encrypted:
            raise HTTPException(status_code=409, detail="Use encrypted file creation")
        if file_full_path.exists() and req.expected_revision:
            if file_revision(file_full_path) != req.expected_revision:
                raise HTTPException(status_code=409, detail="File changed on another device")
        if existing_encrypted and not incoming_encrypted:
            raise HTTPException(status_code=409, detail="Cannot save plaintext over an encrypted file")
        if file_full_path.is_file() and not existing_encrypted and incoming_encrypted:
            raise HTTPException(status_code=409, detail="Use confirmed conversion")
        if existing_encrypted and incoming_encrypted:
            old_id = envelope_file_id(file_full_path.read_text(encoding="utf-8"))
            if envelope_file_id(req.content) != old_id:
                raise HTTPException(status_code=409, detail="Encrypted file identity changed")
        file_full_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(file_full_path, req.content)
    return {
        "status": "saved",
        "vault": sanitize_vault_name(req.vault),
        "filename": file_full_path.relative_to(vault_root(req.vault)).as_posix(),
        "revision": file_revision(file_full_path),
    }


@app.post("/api/file/convert")
def convert_file(req: FileConvertRequest):
    if not req.confirmed:
        raise HTTPException(status_code=400, detail="Conversion confirmation required")
    try:
        envelope = parse_envelope(req.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with vault_lock(req.vault):
        target = resolve_in_vault(req.vault, req.file_path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        if is_encrypted_path(target):
            raise HTTPException(status_code=409, detail="File is already encrypted")
        relative = target.relative_to(vault_root(req.vault)).as_posix()
        mark_conversion_pending(req.vault, relative)
        atomic_write_text(target, req.content)
        register_verifier(req.vault, str(envelope["file_id"]), req.verifier)
        finish_conversion_cleanup(req.vault, relative)
    return {
        "status": "converted",
        "vault": sanitize_vault_name(req.vault),
        "filename": relative,
        "revision": file_revision(target),
        "file_id": envelope["file_id"],
    }


@app.post("/api/file/decrypt")
def decrypt_file(req: FileDecryptRequest):
    if not req.confirmed:
        raise HTTPException(status_code=400, detail="Decryption confirmation required")
    if is_encrypted_text(req.content) or req.content.startswith(ENCRYPTED_PREFIX):
        raise HTTPException(status_code=400, detail="Expected plaintext")
    with vault_lock(req.vault):
        target = resolve_in_vault(req.vault, req.file_path)
        if not is_encrypted_path(target):
            raise HTTPException(status_code=409, detail="File is not encrypted")
        if req.expected_revision and file_revision(target) != req.expected_revision:
            raise HTTPException(status_code=409, detail="File changed on another device")
        file_id = envelope_file_id(target.read_text(encoding="utf-8"))
        atomic_write_text(target, req.content)
        remove_identity_if_unused(req.vault, file_id, vault_root(req.vault))
    return {
        "status": "decrypted",
        "vault": sanitize_vault_name(req.vault),
        "filename": req.file_path,
        "revision": file_revision(target),
    }


@app.get("/api/file/unlock-status")
def unlock_status(vault: str, file_id: str):
    name = sanitize_vault_name(vault)
    if not _vault_has_file_id(name, file_id):
        raise HTTPException(status_code=404, detail="Encrypted file identity not found")
    return throttle_status(name, file_id)


@app.post("/api/file/unlock-failure")
def unlock_failure(req: UnlockReportRequest):
    name = sanitize_vault_name(req.vault)
    if not _vault_has_file_id(name, req.file_id):
        raise HTTPException(status_code=404, detail="Encrypted file identity not found")
    return report_unlock_failure(name, req.file_id)


@app.post("/api/file/unlock-success")
def unlock_success(req: UnlockReportRequest):
    name = sanitize_vault_name(req.vault)
    if not _vault_has_file_id(name, req.file_id):
        raise HTTPException(status_code=404, detail="Encrypted file identity not found")
    if not req.proof or not verify_deletion(name, req.file_id, req.proof):
        raise HTTPException(status_code=400, detail="Invalid unlock proof")
    report_unlock_success(name, req.file_id)
    return {"status": "reset"}


TODO_FILENAME = "TODO.md"
SKILL_FILENAME = "SKILL.md"
_BUNDLED_SKILL_PATH = Path(__file__).resolve().parent / SKILL_FILENAME


def read_skill_text(vault: str) -> str:
    """Vault SKILL.md if the user has one, otherwise the bundled shortcut list."""
    try:
        name = sanitize_vault_name(vault)
    except HTTPException:
        name = ""
    if name:
        path = VAULTS_DIR / name / SKILL_FILENAME
        try:
            if path.is_file() and path.resolve().is_relative_to(VAULTS_DIR.resolve()):
                return path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read %s", path)
    try:
        return _BUNDLED_SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read bundled %s", _BUNDLED_SKILL_PATH)
        return ""


def skill_prompt_block(vault: str) -> str:
    text = read_skill_text(vault).strip()
    if not text:
        return ""
    return (
        "\n--- SKILL.md ---\n"
        f"{text}\n"
        "--- END SKILL.md ---\n"
    )


def normalize_rel_path(vault: str, rel_path: str) -> str:
    """Vault-relative posix path. Rejects escapes without creating the vault."""
    sanitize_vault_name(vault)
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise HTTPException(status_code=400, detail="Missing file path")
    raw = rel_path.strip()
    if raw.startswith(("/", "\\")) or Path(raw).is_absolute():
        raise HTTPException(status_code=400, detail="Invalid file path")
    parts = []
    for part in raw.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise HTTPException(status_code=400, detail="Invalid file path")
        parts.append(part)
    if not parts:
        raise HTTPException(status_code=400, detail="Missing file path")
    return "/".join(parts)


def vault_has_skill_file(vault: str) -> bool:
    try:
        return resolve_in_vault(vault, SKILL_FILENAME).is_file()
    except HTTPException:
        return False


def validate_context_paths(vault: str, paths: List[str]) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for raw in paths:
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="Invalid file path")
        rel = normalize_rel_path(vault, raw)
        if rel in seen:
            continue
        seen.add(rel)
        cleaned.append(rel)
    return cleaned


def resolve_prompt_paths(vault: str, context_files: Optional[List[str]]) -> List[str]:
    """Posted paths win. Otherwise the saved list, or SKILL.md when nothing is saved."""
    name = sanitize_vault_name(vault)
    if context_files is not None:
        return validate_context_paths(name, context_files)
    if name in PROMPT_FILE_SELECTIONS:
        return list(PROMPT_FILE_SELECTIONS[name])
    return [SKILL_FILENAME]


def context_file_bytes(vault: str, rel: str) -> int:
    if rel == SKILL_FILENAME and not vault_has_skill_file(vault):
        try:
            return len(_BUNDLED_SKILL_PATH.read_bytes())
        except OSError:
            return 0
    try:
        path = resolve_in_vault(vault, rel)
    except HTTPException:
        return 0
    if not path.is_file():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def prompt_file_entry(vault: str, rel: str) -> Dict[str, Any]:
    bundled = rel == SKILL_FILENAME and not vault_has_skill_file(vault)
    encrypted = False
    if not bundled:
        try:
            encrypted = is_encrypted_path(resolve_in_vault(vault, rel))
        except HTTPException:
            encrypted = False
    return {
        "path": rel,
        "bytes": context_file_bytes(vault, rel),
        "label": ("SKILL.md (built-in)" if bundled else rel) + (" (encrypted; unavailable)" if encrypted else ""),
        "encrypted": encrypted,
    }


def prompt_files_catalog(vault: str) -> Dict[str, Any]:
    name = sanitize_vault_name(vault)
    if name in PROMPT_FILE_SELECTIONS:
        selected_paths = list(PROMPT_FILE_SELECTIONS[name])
    else:
        selected_paths = [SKILL_FILENAME]
    selected = set(selected_paths)
    available_paths = [path for path in list_vault_files(name) if path not in selected]
    if SKILL_FILENAME not in selected and SKILL_FILENAME not in available_paths:
        available_paths.append(SKILL_FILENAME)
        available_paths.sort()
    return {
        "vault": name,
        "available": [prompt_file_entry(name, path) for path in available_paths],
        "selected": [prompt_file_entry(name, path) for path in selected_paths],
    }


def read_reference_text(vault: str, rel: str) -> str:
    if rel == SKILL_FILENAME:
        return read_skill_text(vault)
    try:
        path = resolve_in_vault(vault, rel)
    except HTTPException:
        return ""
    if not path.is_file():
        return ""
    if is_encrypted_path(path):
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning("Could not read reference file %s/%s", vault, rel)
        return ""


def context_prompt_block(
    vault: str,
    context_files: Optional[List[str]],
    active_path: Optional[str],
) -> str:
    paths = [SKILL_FILENAME] if context_files is None else context_files
    active = ""
    if active_path:
        try:
            active = normalize_rel_path(vault, active_path)
        except HTTPException:
            active = str(active_path).strip().lstrip("/")
    blocks: List[str] = []
    seen = set()
    for rel in paths:
        try:
            key = normalize_rel_path(vault, rel)
        except HTTPException:
            continue
        if key in seen or (active and key == active):
            continue
        seen.add(key)
        text = read_reference_text(vault, key).strip()
        if not text:
            continue
        if key == SKILL_FILENAME:
            blocks.append(f"\n--- SKILL.md ---\n{text}\n--- END SKILL.md ---\n")
        else:
            blocks.append(f"\n--- REFERENCE {key} ---\n{text}\n--- END REFERENCE ---\n")
    return "".join(blocks)


def active_document_block(file_path: Optional[str], file_content: Optional[str]) -> str:
    if not file_content:
        return ""
    return f"\n--- ACTIVE DOCUMENT ({file_path or 'Untitled'}) ---\n{file_content}\n--- END DOCUMENT ---\n"


_REPORT_ONLY_SHORTCUT = re.compile(
    r"(?i)(?:^|\s)/(?:critique|continuity|consistency|dialogue|dialog|synopsis|summary)\b"
)


def document_update_allowed(prompts: str) -> bool:
    """Grammar may rewrite the open document. Report-only shortcuts must not."""
    text = prompts or ""
    if re.search(r"(?i)(?:^|\s)/grammar\b", text):
        return True
    if _REPORT_ONLY_SHORTCUT.search(text):
        return False
    return True


def write_updated_document(vault: str, file_path: Optional[str], updated_content: Optional[str]) -> None:
    if not updated_content or not file_path:
        return
    try:
        file_full_path = resolve_in_vault(vault, file_path)
        with vault_lock(vault):
            if is_encrypted_path(file_full_path):
                logger.info("Skipped server-side write to encrypted file %s/%s", vault, file_path)
                return
            file_full_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(file_full_path, updated_content)
    except HTTPException:
        logger.warning("Skipped writing updated_content outside vault: %s/%s", vault, file_path)


def _ephemeral_history_text(items: Optional[List[Dict[str, str]]]) -> str:
    rows = []
    for item in (items or [])[-10:]:
        role = str(item.get("role") or "")
        text = str(item.get("text") or "")
        if role in {"user", "model"} and text:
            rows.append(f"{role.upper()}: {text}")
    return "\n".join(rows)


async def process_ephemeral_chat(
    req: ChatEnqueueRequest,
    channel: str,
    prompt: str,
    context_files: List[str],
) -> Dict[str, Any]:
    history_text = _ephemeral_history_text(req.ephemeral_history)
    reference_context = context_prompt_block(req.vault, context_files, req.file_path)
    if channel == "todos":
        items, preamble, trailer = read_todo_state(req.vault)
        digest = format_todo_digest(items)
        full_prompt = f"""You are Guppy, a concise to-do list manager for a writer.
Today's date is {date.today().isoformat()}.
CURRENT TODO LIST:
{digest}
{reference_context}
RECENT CONVERSATION:
{history_text}
USER INSTRUCTION:
{prompt}
Return JSON with "reply" and "actions". Never delete items."""
    else:
        doc_context = active_document_block(req.file_path, req.file_content)
        full_prompt = f"""You are an intelligent writing co-pilot.
{doc_context}
{reference_context}
RECENT CONVERSATION:
{history_text}
USER INSTRUCTION:
{prompt}
Return JSON with "reply", "clarification", "updated_content", and "new_file"."""
    response = await asyncio.to_thread(
        lambda: client.models.generate_content(
            model=GEMINI_MODEL,
            contents=full_prompt,
            config=gemini_json_config(),
        )
    )
    data = clean_json_response(response.text)
    if channel == "todos":
        actions = data.get("actions") if isinstance(data.get("actions"), list) else []
        new_items, applied = apply_todo_actions(items, actions)
        if applied:
            write_todo_state(req.vault, new_items, preamble, trailer)
        return {
            "reply": data.get("reply", "Done."),
            "items": new_items,
            "actions": applied,
            "updated_content": None,
        }
    clarification = normalize_clarification(data.get("clarification"))
    updated = None if clarification or not req.allow_file_write else data.get("updated_content")
    return {
        "reply": clarification or data.get("reply", "Done."),
        "updated_content": updated,
        "new_file": None if clarification else normalize_new_file(data.get("new_file")),
        "clarification": clarification,
    }


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
    with vault_lock(req.vault):
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
    with vault_lock(req.vault):
        return _create_file(req)


def _create_file(req: FileCreateRequest):
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
    content = req.content or ""
    if content.startswith(ENCRYPTED_PREFIX):
        try:
            envelope = parse_envelope(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not req.verifier:
            raise HTTPException(status_code=400, detail="Missing deletion verifier")
        atomic_write_text(dest, content)
        register_verifier(req.vault, str(envelope["file_id"]), req.verifier)
    else:
        atomic_write_text(dest, content)
    return {
        "status": "created",
        "vault": sanitize_vault_name(req.vault),
        "filename": rel_posix(req.vault, dest),
        "content": content,
        "encrypted": is_encrypted_text(content),
        "revision": file_revision(dest),
    }


@app.post("/api/file/mkdir")
def create_folder(req: FolderCreateRequest):
    with vault_lock(req.vault):
        return _create_folder(req)


def _create_folder(req: FolderCreateRequest):
    dest_dir = folder_path(req.vault, req.folder)
    name = sanitize_filename(req.name or "untitled")
    dest = dest_dir / name
    if dest.exists():
        if req.name:
            dest = resolve_conflict_destination(dest, req.conflict)
        else:
            dest = next_available_path(dest)
    dest = resolve_in_vault(req.vault, rel_posix(req.vault, dest))
    dest.mkdir(parents=True, exist_ok=False)
    return {
        "status": "created",
        "vault": sanitize_vault_name(req.vault),
        "kind": "folder",
        "filename": rel_posix(req.vault, dest),
    }


@app.post("/api/file/rename")
def rename_file(req: FileRenameRequest):
    with vault_lock(req.vault):
        return _rename_file(req)


def _rename_file(req: FileRenameRequest):
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
    with vault_lock(req.vault):
        return _move_file(req)


def _move_file(req: FileRelocateRequest):
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
    with vault_lock(req.vault):
        return _copy_file(req)


def _copy_file(req: FileRelocateRequest):
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
    raise HTTPException(status_code=400, detail="Password-confirmed deletion required")


@app.post("/api/file/delete-manifest")
def delete_manifest(req: DeleteManifestRequest):
    with vault_lock(req.vault):
        manifest = _path_manifest(req.vault, req.path)
    token = secrets.token_urlsafe(32)
    with DELETE_MANIFESTS_LOCK:
        DELETE_MANIFESTS[token] = {**manifest, "expires": time.time() + 300}
    return {
        "manifest": token,
        "path": manifest["path"],
        "entries": manifest["entries"],
        "encrypted_ids": manifest["encrypted_ids"],
        "expires_in": 300,
    }


@app.post("/api/file/delete-confirmed")
def delete_confirmed(req: ConfirmDeleteRequest):
    if not check_password(req.login_password):
        raise HTTPException(status_code=400, detail="Current password is wrong")
    with DELETE_MANIFESTS_LOCK:
        manifest = DELETE_MANIFESTS.get(req.manifest)
    if not manifest or float(manifest.get("expires", 0)) < time.time():
        raise HTTPException(status_code=409, detail="Delete confirmation expired")
    if manifest["vault"] != sanitize_vault_name(req.vault) or manifest["path"] != req.path:
        raise HTTPException(status_code=409, detail="Delete confirmation does not match")
    with vault_lock(req.vault):
        try:
            if not _manifest_matches(manifest):
                raise HTTPException(status_code=409, detail="Folder changed; review deletion again")
        except HTTPException:
            raise
        for file_id in manifest["encrypted_ids"]:
            status = throttle_status(req.vault, file_id)
            if status["blocked"]:
                raise HTTPException(
                    status_code=429,
                    detail=f"Passphrase cooldown active for encrypted file {file_id}",
                    headers={"Retry-After": str(status["retry_after"])},
                )
            proof = req.proofs.get(file_id)
            if not proof or not verify_deletion(req.vault, file_id, proof):
                report_unlock_failure(req.vault, file_id)
                raise HTTPException(status_code=400, detail=f"Missing or wrong passphrase for encrypted file {file_id}")
        result = _delete_file(req.vault, req.path)
        for file_id in manifest["encrypted_ids"]:
            remove_identity_if_unused(req.vault, file_id, vault_root(req.vault))
    with DELETE_MANIFESTS_LOCK:
        DELETE_MANIFESTS.pop(req.manifest, None)
    return result


def _delete_file(vault: str, path: str):
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


@app.get("/api/history")
def api_history(vault: str = "", path: str = ""):
    if not (vault or "").strip() or not (path or "").strip():
        raise HTTPException(status_code=400, detail="Missing vault or path")
    commits = []
    for row in file_history(vault, path):
        commits.append({key: value for key, value in row.items() if key != "path_at_rev"})
    return {
        "vault": sanitize_vault_name(vault),
        "path": path,
        "commits": commits,
    }


@app.get("/api/history/content")
def api_history_content(vault: str = "", path: str = "", rev: str = ""):
    if not (vault or "").strip() or not (path or "").strip() or not (rev or "").strip():
        raise HTTPException(status_code=400, detail="Missing vault, path, or rev")
    return {
        "vault": sanitize_vault_name(vault),
        "path": path,
        "rev": rev,
        "content": read_file_at(vault, path, rev),
    }


@app.post("/api/history/label")
def api_history_label(req: HistoryLabelRequest):
    label = set_file_label(req.vault, req.path, req.rev, req.name)
    return {
        "vault": sanitize_vault_name(req.vault),
        "path": req.path,
        "rev": req.rev,
        "label": label,
    }


@app.post("/api/history/restore")
def api_history_restore(req: HistoryRestoreRequest):
    content = restore_file(req.vault, req.path, req.rev)
    return {
        "status": "restored",
        "vault": sanitize_vault_name(req.vault),
        "path": req.path,
        "rev": req.rev,
        "content": content,
    }


@app.get("/api/prompt-files")
def get_prompt_files(vault: str = "default"):
    return prompt_files_catalog(vault)


@app.put("/api/prompt-files")
def put_prompt_files(req: PromptFilesUpdateRequest):
    name = sanitize_vault_name(req.vault)
    PROMPT_FILE_SELECTIONS[name] = validate_context_paths(name, req.selected)
    save_prompt_files()
    return prompt_files_catalog(name)


@app.post("/api/chat/enqueue")
async def enqueue_chat(req: ChatEnqueueRequest, background_tasks: BackgroundTasks):
    if req.ephemeral:
        raise HTTPException(status_code=400, detail="Ephemeral chat must use the synchronous endpoint")
    context_files = resolve_prompt_paths(req.vault, req.context_files)
    channel, prompt = resolve_chat_channel(req)
    req.prompt = prompt
    vault, user_msg = _queue_user_message(req, channel=channel)
    if channel == "todos":
        background_tasks.add_task(
            process_todo_queue, vault, req.file_path, req.file_content, context_files, req.allow_file_write
        )
    else:
        background_tasks.add_task(
            process_chat_queue, vault, req.file_path, req.file_content, context_files, req.allow_file_write
        )
    return {"status": "queued", "channel": channel, "message": user_msg}


@app.post("/api/chat")
async def chat(req: ChatEnqueueRequest):
    context_files = resolve_prompt_paths(req.vault, req.context_files)
    channel, prompt = resolve_chat_channel(req)
    req.prompt = prompt
    if req.ephemeral:
        result = await process_ephemeral_chat(req, channel, prompt, context_files)
        reply = result.get("reply", "Done.")
        return {
            "response": reply,
            "message": reply,
            "channel": channel,
            "items": result.get("items"),
            "actions": result.get("actions") or [],
            "updated_content": result.get("updated_content"),
            "new_file": result.get("new_file"),
            "clarification": result.get("clarification"),
            "ephemeral": True,
        }
    if channel == "todos":
        vault, _user_msg = _queue_user_message(req, channel="todos")
        result = await process_todo_queue(
            vault, req.file_path, req.file_content, context_files, req.allow_file_write
        ) or {}
        reply = result.get("reply", "Done.")
        return {
            "response": reply,
            "message": reply,
            "channel": "todos",
            "items": result.get("items"),
            "actions": result.get("actions") or [],
            "updated_content": result.get("updated_content"),
        }
    vault, _user_msg = _queue_user_message(req, channel="writing")
    result = await process_chat_queue(
        vault, req.file_path, req.file_content, context_files, req.allow_file_write
    ) or {}
    reply = result.get("reply", "Done.")
    return {
        "response": reply,
        "message": reply,
        "channel": "writing",
        "updated_content": result.get("updated_content"),
        "new_file": result.get("new_file"),
        "clarification": result.get("clarification"),
    }


@app.get("/api/chat/history")
def get_chat_history(vault: str = "default", channel: str = "writing"):
    store = chat_store("todos" if channel in {"todos", "todo", "guppy"} else "writing")
    return {"channel": "todos" if store is TODO_CHAT_HISTORIES else "writing", "history": store.get(vault, [])}


@app.post("/api/chat/clear")
async def clear_chat_history(req: ClearHistoryRequest):
    vault = sanitize_vault_name(req.vault or "default")
    channel = "todos" if (req.channel or "writing") in {"todos", "todo", "guppy"} else "writing"
    async with get_vault_lock(vault):
        chat_store(channel)[vault] = []
        save_chat_histories()
    return {"status": "cleared", "vault": vault, "channel": channel}


@app.post("/api/chat/clear-all")
async def clear_all_chat(req: ClearHistoryRequest):
    vault = sanitize_vault_name(req.vault or "default")
    async with get_vault_lock(vault):
        CHAT_HISTORIES[vault] = []
        TODO_CHAT_HISTORIES[vault] = []
        save_chat_histories()
    return {"status": "cleared", "vault": vault, "channels": ["writing", "todos"]}
