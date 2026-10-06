import os

os.environ["SNAPSHOTS_ENABLED"] = "0"

import pytest

import app.auth as auth


@pytest.fixture(autouse=True)
def signed_in(request, tmp_path, monkeypatch):
    """Keep existing API tests behind the login cookie without a shared password file."""
    if request.module.__name__.endswith("test_auth"):
        yield
        return
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / ".auth")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    auth.set_password("test-password")
    token = auth.sign_session()
    client = getattr(request.module, "client", None)
    if client is not None and hasattr(client, "cookies"):
        client.cookies.set(auth.COOKIE_NAME, token)
    yield
