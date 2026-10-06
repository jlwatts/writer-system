import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.main as main
from app.main import app


@pytest.fixture(autouse=True)
def auth_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    return tmp_path / "auth"


def test_choose_password_rejects_a_mismatch(auth_dir):
    client = TestClient(app)
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/login"

    page = client.get("/login")
    assert page.status_code == 200
    assert "Choose a password" in page.text

    mismatched = client.post(
        "/login",
        data={"password": "correct-horse", "confirm": "other-horse"},
        follow_redirects=False,
    )
    assert mismatched.status_code == 200
    assert "do not match" in mismatched.text
    assert auth.password_is_set() is False
    assert auth.COOKIE_NAME not in client.cookies
    still_locked = client.get("/", follow_redirects=False)
    assert still_locked.status_code == 303


def test_setting_a_password_signs_in(auth_dir):
    client = TestClient(app)
    saved = client.post(
        "/login",
        data={"password": "correct-horse", "confirm": "correct-horse"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/"
    assert auth.password_is_set() is True
    assert "hash" in (auth_dir / "auth.json").read_text()
    assert "correct-horse" not in (auth_dir / "auth.json").read_text()
    assert client.get("/api/vaults").status_code == 200
    editor = client.get("/")
    assert "WriterSystem" in editor.text
    assert "Choose a password" not in editor.text


def test_short_password_is_rejected(auth_dir):
    client = TestClient(app)
    short = client.post(
        "/login",
        data={"password": "short", "confirm": "short"},
        follow_redirects=False,
    )
    assert short.status_code == 200
    assert auth.password_is_set() is False


def test_api_without_a_cookie_is_rejected(auth_dir):
    auth.set_password("correct-horse")
    client = TestClient(app)
    denied = client.get("/api/files", params={"vault": "default"})
    assert denied.status_code == 401


def test_wrong_password_does_not_sign_in(auth_dir):
    auth.set_password("correct-horse")
    client = TestClient(app)
    wrong = client.post("/login", data={"password": "nope-nope"}, follow_redirects=False)
    assert wrong.status_code == 200
    assert "Wrong password" in wrong.text
    assert auth.COOKIE_NAME not in client.cookies
    assert client.get("/api/vaults", follow_redirects=False).status_code == 401

    right = client.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    assert right.status_code == 303
    assert client.get("/api/vaults").status_code == 200


def test_change_password_rejects_bad_current_and_mismatch(auth_dir):
    auth.set_password("correct-horse")
    client = TestClient(app)
    client.post("/login", data={"password": "correct-horse"}, follow_redirects=False)

    bad_current = client.post("/api/password", json={
        "current": "wrong-current",
        "next": "new-password",
        "confirm": "new-password",
    })
    assert bad_current.status_code == 400
    assert auth.check_password("correct-horse")

    mismatch = client.post("/api/password", json={
        "current": "correct-horse",
        "next": "new-password",
        "confirm": "other-password",
    })
    assert mismatch.status_code == 400
    assert auth.check_password("correct-horse")
    assert not auth.check_password("new-password")


def test_change_password_signs_out_other_sessions(auth_dir):
    auth.set_password("correct-horse")
    first = TestClient(app)
    second = TestClient(app)
    first.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    second.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    assert second.get("/api/vaults").status_code == 200

    changed = first.post("/api/password", json={
        "current": "correct-horse",
        "next": "new-password",
        "confirm": "new-password",
    })
    assert changed.status_code == 200
    assert changed.json()["status"] == "saved"
    assert first.get("/api/vaults").status_code == 200
    assert second.get("/api/vaults").status_code == 401

    logged_out = TestClient(app)
    old = logged_out.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    assert old.status_code == 200
    assert "Wrong password" in old.text
    new = logged_out.post("/login", data={"password": "new-password"}, follow_redirects=False)
    assert new.status_code == 303
    assert logged_out.get("/api/vaults").status_code == 200


def test_logout_requires_sign_in_again(auth_dir):
    auth.set_password("correct-horse")
    client = TestClient(app)
    client.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    goodbye = client.get("/logout", follow_redirects=False)
    assert goodbye.status_code == 303
    assert goodbye.headers["location"] == "/login"
    assert client.get("/api/vaults", follow_redirects=False).status_code == 401


def test_auth_file_is_outside_the_vault(tmp_path, monkeypatch):
    vaults = tmp_path / "vaults"
    data = tmp_path / "data"
    monkeypatch.setattr(main, "VAULTS_DIR", vaults)
    monkeypatch.setattr(auth, "AUTH_DIR", data)
    auth.set_password("correct-horse")
    stored = (data / "auth.json").resolve()
    assert stored.is_file()
    vaults.mkdir(parents=True, exist_ok=True)
    assert vaults.resolve() not in stored.parents
