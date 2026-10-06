"""One system password and a signed session cookie. The hash never lives in the vault."""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

AUTH_DIR = Path(os.getenv("AUTH_DIR", "./data"))
COOKIE_NAME = "writer_session"
_MIN_PASSWORD = 8
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def cookie_secure() -> bool:
    return os.getenv("COOKIE_SECURE", "") == "1"


def auth_path() -> Path:
    return Path(AUTH_DIR) / "auth.json"


def _load() -> dict | None:
    path = auth_path()
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    directory = Path(AUTH_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = auth_path()
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(directory, 0o700)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _secret_key(secret: str) -> bytes:
    return bytes.fromhex(secret)


def password_is_set() -> bool:
    data = _load()
    return bool(data and data.get("hash") and data.get("salt"))


def set_password(password: str, rotate_secret: bool = False) -> None:
    if len(password) < _MIN_PASSWORD:
        raise ValueError("Password must be at least 8 characters")
    data = _load() or {}
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    data["salt"] = salt.hex()
    data["hash"] = digest.hex()
    data["n"] = _SCRYPT_N
    data["r"] = _SCRYPT_R
    data["p"] = _SCRYPT_P
    if rotate_secret or not data.get("secret"):
        data["secret"] = secrets.token_hex(32)
    _save(data)


def check_password(password: str) -> bool:
    data = _load()
    if not data or not data.get("hash") or not data.get("salt"):
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(data["salt"]),
        n=int(data.get("n", _SCRYPT_N)),
        r=int(data.get("r", _SCRYPT_R)),
        p=int(data.get("p", _SCRYPT_P)),
        dklen=32,
    )
    return hmac.compare_digest(digest.hex(), data["hash"])


def change_password(current: str, new: str) -> None:
    if not check_password(current):
        raise ValueError("Current password is wrong")
    set_password(new, rotate_secret=True)


def sign_session() -> str:
    data = _load()
    if not data or not data.get("secret"):
        raise RuntimeError("No session secret")
    nonce = secrets.token_hex(16)
    mac = hmac.new(_secret_key(data["secret"]), nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{nonce}.{mac}"


def session_ok(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, mac = token.split(".", 1)
    data = _load()
    if not data or not data.get("secret"):
        return False
    expect = hmac.new(_secret_key(data["secret"]), nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, mac)


def apply_session_cookie(response):
    response.set_cookie(
        COOKIE_NAME,
        sign_session(),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
        max_age=60 * 60 * 24 * 30,
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=cookie_secure(),
        httponly=True,
    )
    return response


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_session(request, call_next):
        path = request.url.path
        if path.startswith("/static") or (path == "/login" and request.method in {"GET", "POST"}):
            return await call_next(request)
        if session_ok(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)
        if path.startswith("/api"):
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
