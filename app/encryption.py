"""Server-side guards and metadata for browser-encrypted vault files."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HEADER = "WRITERENC1"
PREFIX = HEADER + "\n"
VERSION = 1
KDF = "PBKDF2-HMAC-SHA-256"
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 1_000_000
EXPECTED_FIELDS = {
    "v", "kdf", "iterations", "salt", "file_id", "nonce", "ciphertext", "tag"
}
_STATE_LOCK = threading.RLock()
# Exponential unlock waits. Set ENCRYPTION_UNLOCK_COOLDOWNS=0 to disable
# waits while keeping the failure-count and tenth-failure policy.
UNLOCK_COOLDOWNS_ENABLED = os.getenv("ENCRYPTION_UNLOCK_COOLDOWNS", "1") == "1"


def _auth_dir() -> Path:
    from app import auth

    root = Path(auth.AUTH_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_path() -> Path:
    return _auth_dir() / "encryption.json"


def _empty_state() -> dict[str, Any]:
    return {"verifiers": {}, "throttles": {}, "pending_conversions": {}}


def _load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    state = _empty_state()
    if isinstance(data, dict):
        for key in state:
            if isinstance(data.get(key), dict):
                state[key] = data[key]
    return state


def _save_state(state: dict[str, Any]) -> None:
    path = state_path()
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _decode(value: Any, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Invalid encrypted file")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("Invalid encrypted file") from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("Invalid encrypted file")
    if length is not None and len(raw) != length:
        raise ValueError("Invalid encrypted file")
    return raw


def parse_envelope(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise ValueError("Invalid encrypted file")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Invalid encrypted file")
            result[key] = value
        return result
    try:
        data = json.loads(text[len(PREFIX):], object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid encrypted file") from exc
    if not isinstance(data, dict) or set(data) != EXPECTED_FIELDS:
        raise ValueError("Invalid encrypted file")
    if (
        data["v"] != VERSION
        or data["kdf"] != KDF
        or not isinstance(data["iterations"], int)
        or not MIN_ITERATIONS <= data["iterations"] <= MAX_ITERATIONS
    ):
        raise ValueError("Unsupported encrypted file")
    _decode(data["salt"], 16)
    _decode(data["file_id"], 16)
    _decode(data["nonce"], 12)
    _decode(data["tag"], 16)
    _decode(data["ciphertext"])
    return data


def is_encrypted_text(text: str) -> bool:
    try:
        parse_envelope(text)
        return True
    except ValueError:
        return False


def is_encrypted_path(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            if stream.read(len(PREFIX)) != PREFIX.encode("ascii"):
                return False
        parse_envelope(path.read_text(encoding="utf-8"))
        return True
    except (OSError, UnicodeDecodeError, ValueError):
        return False


def envelope_file_id(text: str) -> str:
    return str(parse_envelope(text)["file_id"])


def file_revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _identity(vault: str, file_id: str) -> str:
    _decode(file_id, 16)
    return f"{vault}:{file_id}"


def register_verifier(vault: str, file_id: str, verifier: str) -> None:
    raw = _decode(verifier, 32)
    salt = secrets.token_bytes(16)
    digest = hashlib.sha256(salt + raw).hexdigest()
    with _STATE_LOCK:
        state = _load_state()
        state["verifiers"][_identity(vault, file_id)] = {
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": digest,
        }
        _save_state(state)


def verify_deletion(vault: str, file_id: str, verifier: str) -> bool:
    try:
        raw = _decode(verifier, 32)
    except ValueError:
        return False
    with _STATE_LOCK:
        item = _load_state()["verifiers"].get(_identity(vault, file_id))
    if not isinstance(item, dict):
        return False
    try:
        salt = _decode(item.get("salt"), 16)
        expected = str(item["hash"])
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(hashlib.sha256(salt + raw).hexdigest(), expected)


def throttle_status(vault: str, file_id: str, now: float | None = None) -> dict[str, Any]:
    moment = time.time() if now is None else now
    with _STATE_LOCK:
        item = _load_state()["throttles"].get(_identity(vault, file_id), {})
    failures = int(item.get("failures", 0))
    until = float(item.get("until", 0))
    if not UNLOCK_COOLDOWNS_ENABLED:
        until = 0
    return {
        "failures": failures,
        "until": until,
        "retry_after": max(0, int(until - moment + 0.999)),
        "blocked": moment < until,
    }


def report_unlock_failure(vault: str, file_id: str, now: float | None = None) -> dict[str, Any]:
    moment = time.time() if now is None else now
    key = _identity(vault, file_id)
    with _STATE_LOCK:
        state = _load_state()
        current = state["throttles"].get(key, {})
        until = float(current.get("until", 0))
        if UNLOCK_COOLDOWNS_ENABLED and moment < until:
            return throttle_status(vault, file_id, moment)
        failures = min(10, int(current.get("failures", 0)) + 1)
        minutes = 2 ** (failures - 1) if UNLOCK_COOLDOWNS_ENABLED else 0
        state["throttles"][key] = {"failures": failures, "until": moment + minutes * 60}
        _save_state(state)
    return throttle_status(vault, file_id, moment)


def report_unlock_success(vault: str, file_id: str) -> None:
    key = _identity(vault, file_id)
    with _STATE_LOCK:
        state = _load_state()
        if key in state["throttles"]:
            del state["throttles"][key]
            _save_state(state)


def mark_conversion_pending(vault: str, relative_path: str) -> None:
    with _STATE_LOCK:
        state = _load_state()
        state["pending_conversions"][vault] = {"path": relative_path}
        _save_state(state)


def clear_conversion_pending(vault: str) -> None:
    with _STATE_LOCK:
        state = _load_state()
        if vault in state["pending_conversions"]:
            del state["pending_conversions"][vault]
            _save_state(state)


def pending_conversions() -> dict[str, dict[str, str]]:
    with _STATE_LOCK:
        return dict(_load_state()["pending_conversions"])


def remove_identity_if_unused(vault: str, file_id: str, vault_root: Path) -> None:
    for path in vault_root.rglob("*"):
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(vault_root).parts):
            continue
        try:
            if envelope_file_id(path.read_text(encoding="utf-8")) == file_id:
                return
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    key = _identity(vault, file_id)
    with _STATE_LOCK:
        state = _load_state()
        state["verifiers"].pop(key, None)
        state["throttles"].pop(key, None)
        _save_state(state)
