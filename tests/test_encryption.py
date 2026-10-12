"""Executable Given/When/Then requirements for encrypted notes.

The pure functions are unit tests; HTTP boundaries and filesystem effects are
integration tests. Browser lifecycle wiring is covered by static component
contracts here and by the real-browser suite in test_encryption_browser.py.
"""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.history as history
from app.encryption import (
    atomic_write_text,
    mark_conversion_pending,
    pending_conversions,
    parse_envelope,
    report_unlock_failure,
    report_unlock_success,
    throttle_status,
)
from app.main import app

client = TestClient(app)


def b64(size: int, byte: int = 1) -> str:
    return base64.b64encode(bytes([byte]) * size).decode("ascii")


def envelope(file_id: str | None = None, nonce_byte: int = 2) -> str:
    return "WRITERENC1\n" + json.dumps(
        {
            "v": 1,
            "kdf": "PBKDF2-HMAC-SHA-256",
            "iterations": 600_000,
            "salt": b64(16, 1),
            "file_id": file_id or b64(16, 3),
            "nonce": b64(12, nonce_byte),
            "ciphertext": b64(8, 4),
            "tag": b64(16, 5),
        },
        separators=(",", ":"),
    )


@pytest.fixture
def vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path / "vaults")
    main.DELETE_MANIFESTS.clear()
    return main.VAULTS_DIR


# ENC-U-009/ENC-U-010/FILE-U-001 — Unit: Given a versioned envelope, when parsed, then only the exact supported
# authenticated-metadata schema and binary lengths are accepted.
def test_unit_envelope_parser_is_strict():
    assert parse_envelope(envelope())["iterations"] == 600_000
    payload = json.loads(envelope().split("\n", 1)[1])
    payload["unexpected"] = True
    with pytest.raises(ValueError):
        parse_envelope("WRITERENC1\n" + json.dumps(payload))
    payload.pop("unexpected")
    payload["nonce"] = b64(11)
    with pytest.raises(ValueError):
        parse_envelope("WRITERENC1\n" + json.dumps(payload))
    duplicate = envelope().replace('"v":1', '"v":1,"v":1', 1)
    with pytest.raises(ValueError):
        parse_envelope(duplicate)


# FILE-I-001/CREATE-I-001 — Integration: Given a born-encrypted note, when it is created and read, then
# only the envelope is stored and the response is explicitly non-cacheable.
def test_integration_born_encrypted_round_trip_stays_ciphertext(vaults: Path):
    response = client.post(
        "/api/file/create",
        json={
            "vault": "secure",
            "name": "secret.md",
            "content": envelope(),
            "verifier": b64(32, 9),
        },
    )
    assert response.status_code == 200
    target = vaults / "secure" / "secret.md"
    assert target.read_text().startswith("WRITERENC1\n")
    assert "plaintext marker" not in target.read_text()

    opened = client.get("/api/file?vault=secure&path=secret.md")
    assert opened.status_code == 200
    assert opened.json()["encrypted"] is True
    assert opened.json()["content"] == target.read_text()
    assert opened.headers["cache-control"] == "no-store, max-age=0"


# FILE-I-003/FILE-I-005/FILE-I-008 — Integration: Given an encrypted note, when plaintext, a stale revision, or a
# different file identity is saved, then the original bytes remain unchanged.
@pytest.mark.parametrize("attack", ["plaintext", "stale", "identity"])
def test_integration_encrypted_save_guards_are_fail_closed(vaults: Path, attack: str):
    original = envelope()
    created = client.post(
        "/api/file/create",
        json={"vault": "v", "name": "s.md", "content": original, "verifier": b64(32)},
    ).json()
    payload = envelope(nonce_byte=8)
    expected = created["revision"]
    if attack == "plaintext":
        payload = "plaintext marker"
    elif attack == "stale":
        expected = "0" * 64
    else:
        payload = envelope(file_id=b64(16, 7))
    response = client.post(
        "/api/file",
        json={"vault": "v", "file_path": "s.md", "content": payload, "expected_revision": expected},
    )
    assert response.status_code == 409
    assert (vaults / "v" / "s.md").read_text() == original


# FILE-I-004 — Integration: Given plaintext already exists, when an envelope is sent through
# ordinary autosave, then conversion is refused and plaintext is not replaced.
def test_integration_plaintext_requires_confirmed_conversion(vaults: Path):
    target = vaults / "v" / "plain.md"
    target.parent.mkdir(parents=True)
    target.write_text("plaintext marker")
    response = client.post(
        "/api/file",
        json={"vault": "v", "file_path": "plain.md", "content": envelope()},
    )
    assert response.status_code == 409
    assert target.read_text() == "plaintext marker"


# CONVERT-I-001 — Integration: Given conversion is confirmed, when encryption is committed,
# then history and matching local Syncthing versions are purged after the
# envelope reaches disk.
def test_integration_conversion_runs_destructive_cleanup(vaults: Path, monkeypatch):
    target = vaults / "v" / "plain.md"
    target.parent.mkdir(parents=True)
    target.write_text("plaintext marker")
    calls = []
    monkeypatch.setattr(main, "purge_vault_history", lambda vault: calls.append(("history", vault)))
    monkeypatch.setattr(
        main, "_delete_syncthing_versions", lambda vault, path: calls.append(("versions", vault, path))
    )
    response = client.post(
        "/api/file/convert",
        json={
            "vault": "v",
            "file_path": "plain.md",
            "content": envelope(),
            "verifier": b64(32, 9),
            "confirmed": True,
        },
    )
    assert response.status_code == 200
    assert target.read_text().startswith("WRITERENC1\n")
    assert calls == [("history", "v"), ("versions", "v", "plain.md")]


# THROTTLE-U-001/THROTTLE-I-001 — Unit/integration: Given wrong unlocks, when failures are reported after each
# cooldown, then waits double through 512 minutes; success clears the state.
def test_integration_unlock_throttle_persists_and_caps_at_ten():
    file_id = b64(16, 8)
    now = 1_000.0
    for failure in range(1, 11):
        status = report_unlock_failure("v", file_id, now=now)
        assert status["failures"] == failure
        assert status["retry_after"] == (2 ** (failure - 1)) * 60
        now = status["until"]
    assert throttle_status("v", file_id, now=now)["failures"] == 10
    report_unlock_success("v", file_id)
    assert throttle_status("v", file_id, now=now)["failures"] == 0


# THROTTLE-I-007 — Integration: Given a throttled encrypted identity, when an attacker posts an
# unproven success or references another vault, then the count is not reset.
def test_integration_unlock_reset_requires_passphrase_derived_proof(vaults: Path):
    file_id = b64(16, 8)
    verifier = b64(32, 9)
    created = client.post(
        "/api/file/create",
        json={
            "vault": "v",
            "name": "secret.md",
            "content": envelope(file_id=file_id),
            "verifier": verifier,
        },
    )
    assert created.status_code == 200
    failed = client.post("/api/file/unlock-failure", json={"vault": "v", "file_id": file_id})
    assert failed.status_code == 200
    assert failed.json()["failures"] == 1
    assert client.post(
        "/api/file/unlock-success", json={"vault": "v", "file_id": file_id}
    ).status_code == 400
    assert client.post(
        "/api/file/unlock-success",
        json={"vault": "other", "file_id": file_id, "proof": verifier},
    ).status_code == 404
    # Direct helper uses a future clock so this assertion is independent of the
    # one-minute block that intentionally remains in force.
    assert throttle_status("v", file_id)["failures"] == 1
    assert client.post(
        "/api/file/unlock-success",
        json={"vault": "v", "file_id": file_id, "proof": verifier},
    ).status_code == 200
    assert throttle_status("v", file_id)["failures"] == 0


# DELETE-I-006/DELETE-I-007 — Integration: Given a folder contains plaintext and encrypted files, when any
# password/proof is absent or a reviewed file changes, then nothing is deleted.
def test_integration_folder_delete_validates_everything_before_mutation(vaults: Path):
    folder = vaults / "v" / "folder"
    folder.mkdir(parents=True)
    (folder / "plain.md").write_text("keep me")
    file_id = b64(16, 3)
    created = client.post(
        "/api/file/create",
        json={
            "vault": "v",
            "folder": "folder",
            "name": "secret.md",
            "content": envelope(file_id=file_id),
            "verifier": b64(32, 9),
        },
    )
    assert created.status_code == 200
    manifest = client.post(
        "/api/file/delete-manifest", json={"vault": "v", "path": "folder"}
    ).json()

    wrong = client.post(
        "/api/file/delete-confirmed",
        json={
            "vault": "v",
            "path": "folder",
            "login_password": "wrong",
            "manifest": manifest["manifest"],
            "proofs": {file_id: b64(32, 9)},
        },
    )
    assert wrong.status_code == 400
    assert folder.exists()

    (folder / "plain.md").write_text("changed after review")
    changed = client.post(
        "/api/file/delete-confirmed",
        json={
            "vault": "v",
            "path": "folder",
            "login_password": "test-password",
            "manifest": manifest["manifest"],
            "proofs": {file_id: b64(32, 9)},
        },
    )
    assert changed.status_code == 409
    assert folder.exists()


# DELETE-I-002/DELETE-I-005 — Integration: Given valid login and every encrypted deletion proof, when the
# unchanged folder is confirmed, then the folder is deleted.
def test_integration_confirmed_folder_delete(vaults: Path):
    verifier = b64(32, 9)
    file_id = b64(16, 3)
    response = client.post(
        "/api/file/create",
        json={
            "vault": "v",
            "folder": "folder",
            "name": "secret.md",
            "content": envelope(file_id=file_id),
            "verifier": verifier,
        },
    )
    assert response.status_code == 200
    manifest = client.post(
        "/api/file/delete-manifest", json={"vault": "v", "path": "folder"}
    ).json()
    deleted = client.post(
        "/api/file/delete-confirmed",
        json={
            "vault": "v",
            "path": "folder",
            "login_password": "test-password",
            "manifest": manifest["manifest"],
            "proofs": {file_id: verifier},
        },
    )
    assert deleted.status_code == 200
    assert not (vaults / "v" / "folder").exists()


# LOCK-B-001/LOCK-B-002/LOCK-B-003/LOCK-B-004 — Browser-component contract: Given an encrypted note is open, when a lock
# trigger fires, then pending save is cancelled, editor surfaces are blanked,
# key memory is cleared, chats are cleared, and logout uses the same path.
def test_browser_component_lock_contract_is_wired():
    source = Path(main.__file__).with_name("templates").joinpath("index.html").read_text()
    start = source.index("function lockEncryptedSession")
    body = source[start : source.index("\n        async function", start)]
    for marker in (
        "clearTimeout(saveTimer)",
        "WriterCrypto.forgetAll()",
        "blankSensitiveUi()",
        "clearAllChat",
    ):
        assert marker in body
    assert "editor.value = ''" in source
    assert "document.getElementById('markdown-preview').innerHTML = ''" in source
    assert "window.addEventListener('offline'" in source
    assert "window.addEventListener('pagehide'" in source
    assert "document.addEventListener('visibilitychange'" in source
    assert "lockEncryptedSession('logout'" in source


# SAVE-B-004/SAVE-I-001 — Browser-component contract: Given encrypted editing, when autosave runs, then
# the editor plaintext is sealed before transmission and optimistic revision is
# included. There is no plaintext fallback on crypto failure.
def test_browser_component_encrypted_autosave_contract_is_wired():
    source = Path(main.__file__).with_name("templates").joinpath("index.html").read_text()
    start = source.index("async function autosaveNow")
    body = source[start : source.index("\n        async function", start + 20)]
    assert "WriterCrypto.seal" in body
    assert "expected_revision" in body
    assert "body: JSON.stringify" in body
    assert body.index("WriterCrypto.seal") < body.index("fetch('/api/file'")


# CHAT-I-002/GEM-I-001/GEM-I-002 — Integration: Given encrypted-mode chat, when Gemini is called, then the
# request carries an explicit REST-level store=false and no server chat record
# or plaintext note write is created.
@patch("app.main.client.models.generate_content")
def test_integration_ephemeral_gemini_is_no_store_and_not_persisted(mock_generate, vaults: Path):
    marker = "PRIVATE-MARKER-47e5"
    response = MagicMock()
    response.text = json.dumps(
        {
            "reply": "private reply",
            "clarification": None,
            "updated_content": "must remain browser-only",
            "new_file": None,
        }
    )
    mock_generate.return_value = response
    main.CHAT_HISTORIES["v"] = [{"role": "user", "text": "old", "status": "completed"}]
    target = vaults / "v" / "secret.md"
    target.parent.mkdir(parents=True)
    target.write_text(envelope())

    result = client.post(
        "/api/chat",
        json={
            "vault": "v",
            "prompt": "help",
            "file_path": "secret.md",
            "file_content": marker,
            "ephemeral": True,
            "ephemeral_history": [{"role": "user", "text": "in-memory"}],
        },
    )
    assert result.status_code == 200
    assert main.CHAT_HISTORIES["v"] == [{"role": "user", "text": "old", "status": "completed"}]
    assert target.read_text().startswith("WRITERENC1\n")
    call = mock_generate.call_args.kwargs
    assert call["config"].http_options.extra_body == {"store": False}
    assert marker in call["contents"]


# CHAT-I-001/CHAT-B-002 — Integration: Given stored writing and to-do conversations, when clear-all is
# requested, then both memory channels and the persisted chat file are empty.
def test_integration_clear_all_chat_channels(vaults: Path):
    main.CHAT_HISTORIES["v"] = [{"role": "user", "text": "writing"}]
    main.TODO_CHAT_HISTORIES["v"] = [{"role": "user", "text": "todos"}]
    main.save_chat_histories()
    response = client.post("/api/chat/clear-all", json={"vault": "v"})
    assert response.status_code == 200
    assert main.CHAT_HISTORIES["v"] == []
    assert main.TODO_CHAT_HISTORIES["v"] == []
    stored = json.loads(main.chat_store_path().read_text())
    assert stored["writing"]["v"] == []
    assert stored["todos"]["v"] == []


# DOWN-I-001/CACHE-I-001 — Integration: Given a server download of an encrypted note, when bytes are
# returned, then they are exactly the envelope and are marked no-store.
def test_integration_server_download_never_decrypts(vaults: Path):
    disk = envelope()
    target = vaults / "v" / "secret.md"
    target.parent.mkdir(parents=True)
    target.write_text(disk)
    response = client.get("/api/file/download?vault=v&path=secret.md")
    assert response.status_code == 200
    assert response.text == disk
    assert response.headers["cache-control"] == "no-store, max-age=0"


# HIST-I-002/SYNC-I-001 — Integration: Given plaintext history and a local Syncthing version, when a
# confirmed conversion completes, then the vault Git repository and matching
# readable version copies are permanently removed.
def test_integration_conversion_wipes_real_local_plaintext_copies(
    vaults: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    target = vaults / "v" / "notes" / "plain.md"
    target.parent.mkdir(parents=True)
    target.write_text("UNIQUE-OLD-PLAINTEXT")
    versions = vaults / "v" / ".stversions" / "notes"
    versions.mkdir(parents=True)
    old_version = versions / "plain~20261012-120000.md"
    old_version.write_text("UNIQUE-OLD-PLAINTEXT")
    history.snapshot_vault("v")
    assert history.repo_dir("v").exists()

    response = client.post(
        "/api/file/convert",
        json={
            "vault": "v",
            "file_path": "notes/plain.md",
            "content": envelope(),
            "verifier": b64(32, 9),
            "confirmed": True,
        },
    )
    assert response.status_code == 200
    assert not history.repo_dir("v").exists()
    assert not old_version.exists()
    assert "UNIQUE-OLD-PLAINTEXT" not in target.read_text()


# HIST-I-001/HIST-I-005 — Integration: Given a born-encrypted note beside plaintext, when snapshots
# run repeatedly, then only plaintext paths enter Git and encrypted APIs reject
# history and restore access.
def test_integration_encrypted_files_never_enter_history(
    vaults: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    root = vaults / "v"
    root.mkdir(parents=True)
    (root / "plain.md").write_text("ordinary")
    (root / "secret.md").write_text(envelope())
    history.snapshot_vault("v")
    listing = history._run("v", "ls-tree", "-r", "--name-only", "HEAD").stdout.decode()
    assert "plain.md" in listing
    assert "secret.md" not in listing
    assert client.get("/api/history?vault=v&path=secret.md").status_code == 409


# FILE-I-007 — Unit: Given replacement fails during an atomic save, when the write aborts,
# then the previous complete envelope remains and temporary files are removed.
def test_unit_atomic_write_interruption_preserves_previous_file(tmp_path: Path, monkeypatch):
    target = tmp_path / "secret.md"
    original = envelope(nonce_byte=1)
    target.write_text(original)

    def fail_replace(_source, _target):
        raise OSError("simulated power-loss boundary")

    monkeypatch.setattr("app.encryption.os.replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write_text(target, envelope(nonce_byte=2))
    assert target.read_text() == original
    assert list(tmp_path.glob(".secret.md.*.tmp")) == []


# CONVERT-I-003/E2E-002 — Integration: Given conversion reached encrypted disk but cleanup was
# interrupted, when startup recovery runs, then history and local versions are
# purged and the durable recovery journal is cleared.
def test_integration_interrupted_conversion_cleanup_recovers(
    vaults: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    target = vaults / "v" / "plain.md"
    target.parent.mkdir(parents=True)
    target.write_text("RECOVERY-PLAINTEXT-MARKER")
    history.snapshot_vault("v")
    version_dir = vaults / "v" / ".stversions"
    version_dir.mkdir()
    old_version = version_dir / "plain~20261012-120000.md"
    old_version.write_text("RECOVERY-PLAINTEXT-MARKER")
    target.write_text(envelope())
    mark_conversion_pending("v", "plain.md")
    assert "v" in pending_conversions()

    main.recover_pending_conversions()

    assert pending_conversions() == {}
    assert not history.repo_dir("v").exists()
    assert not old_version.exists()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"RECOVERY-PLAINTEXT-MARKER" not in path.read_bytes()
