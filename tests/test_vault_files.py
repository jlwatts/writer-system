import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app, CHAT_HISTORIES

client = TestClient(app)


def confirmed_delete(vault: str, path: str):
    manifest = client.post(
        "/api/file/delete-manifest", json={"vault": vault, "path": path}
    )
    assert manifest.status_code == 200
    return client.post(
        "/api/file/delete-confirmed",
        json={
            "vault": vault,
            "path": path,
            "login_password": "test-password",
            "manifest": manifest.json()["manifest"],
            "proofs": {},
        },
    )


@pytest.fixture
def vaults_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    CHAT_HISTORIES.clear()
    yield tmp_path
    CHAT_HISTORIES.clear()


def test_list_vaults_creates_default(vaults_dir: Path):
    response = client.get("/api/vaults")
    assert response.status_code == 200
    assert response.json()["vaults"] == ["default"]
    assert (vaults_dir / "default").is_dir()


def test_save_list_and_read_file(vaults_dir: Path):
    save = client.post("/api/file", json={
        "vault": "books",
        "file_path": "chapter-1.md",
        "content": "Once upon a time.",
    })
    assert save.status_code == 200
    assert save.json()["filename"] == "chapter-1.md"
    assert (vaults_dir / "books" / "chapter-1.md").read_text() == "Once upon a time."

    nested = client.post("/api/file", json={
        "vault": "books",
        "file_path": "notes/todo-scratch.md",
        "content": "scratch",
    })
    assert nested.status_code == 200

    files = client.get("/api/files?vault=books")
    assert files.status_code == 200
    assert files.json()["files"] == ["chapter-1.md", "notes/todo-scratch.md"]
    assert "" in files.json()["folders"]
    assert "notes" in files.json()["folders"]
    mtimes = files.json()["mtimes"]
    assert mtimes["chapter-1.md"] > 0
    assert mtimes["notes/todo-scratch.md"] > 0
    assert mtimes["notes"] >= mtimes["notes/todo-scratch.md"]

    opened = client.get("/api/file?vault=books&path=chapter-1.md")
    assert opened.status_code == 200
    assert opened.json()["content"] == "Once upon a time."
    assert opened.json()["filename"] == "chapter-1.md"

    todo = client.post("/api/file", json={
        "vault": "books",
        "file_path": "TODO.md",
        "content": "- [ ] Write more",
    })
    assert todo.status_code == 200
    loaded = client.get("/api/file?vault=books&filename=TODO.md")
    assert loaded.status_code == 200
    assert loaded.json()["content"] == "- [ ] Write more"


def test_folder_mtime_follows_newest_file(vaults_dir: Path):
    books = vaults_dir / "books"
    (books / "zeta").mkdir(parents=True)
    (books / "alpha").mkdir(parents=True)
    older = books / "zeta" / "old.md"
    newer = books / "alpha" / "new.md"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    base = older.stat().st_mtime
    os.utime(older, (base - 120, base - 120))
    os.utime(newer, (base + 120, base + 120))

    files = client.get("/api/files?vault=books")
    mtimes = files.json()["mtimes"]
    assert mtimes["alpha"] >= mtimes["zeta"]
    assert mtimes["alpha/new.md"] > mtimes["zeta/old.md"]


def test_rejects_path_traversal(vaults_dir: Path):
    response = client.get("/api/file?vault=books&path=../secret.md")
    assert response.status_code == 400

    save = client.post("/api/file", json={
        "vault": "books",
        "file_path": "../escape.md",
        "content": "nope",
    })
    assert save.status_code == 400


def test_missing_file_is_404(vaults_dir: Path):
    (vaults_dir / "books").mkdir()
    response = client.get("/api/file?vault=books&path=missing.md")
    assert response.status_code == 404


@patch("app.main.client.models.generate_content")
def test_sync_chat_returns_ui_payload(mock_gemini, vaults_dir: Path):
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Revised the scene.", "updated_content": "New draft"}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Tighten the opening",
        "vault": "books",
        "file_path": "chapter-1.md",
        "file_content": "Old draft",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Revised the scene."
    assert body["message"] == "Revised the scene."
    assert body["updated_content"] == "New draft"
    assert (vaults_dir / "books" / "chapter-1.md").read_text() == "New draft"


@patch("app.main.client.models.generate_content")
def test_clarification_blocks_file_changes(mock_gemini, vaults_dir: Path):
    books = vaults_dir / "books"
    books.mkdir()
    (books / "chapter-1.md").write_text("Old draft", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "reply": "I will rewrite everything.",
        "clarification": "Which scene should I extract?",
        "updated_content": "Replaced",
        "new_file": {"name": "scene.md", "content": "Extracted"},
    })
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Pull a scene out",
        "vault": "books",
        "file_path": "chapter-1.md",
        "file_content": "Old draft",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["clarification"] == "Which scene should I extract?"
    assert body["response"] == "Which scene should I extract?"
    assert body["updated_content"] is None
    assert body["new_file"] is None
    assert (books / "chapter-1.md").read_text(encoding="utf-8") == "Old draft"
    assert not (books / "scene.md").exists()

    history = client.get("/api/chat/history?vault=books").json()["history"]
    model_turns = [msg for msg in history if msg["role"] == "model"]
    assert model_turns[-1]["text"] == "Which scene should I extract?"
    assert model_turns[-1]["clarification"] is True

    mock_response.text = json.dumps({
        "reply": "Saved the tavern scene.",
        "clarification": None,
        "updated_content": None,
        "new_file": None,
    })
    follow = client.post("/api/chat", json={
        "prompt": "The tavern scene",
        "vault": "books",
        "file_path": "chapter-1.md",
        "file_content": "Old draft",
    })
    assert follow.status_code == 200
    assert follow.json()["clarification"] is None
    history = client.get("/api/chat/history?vault=books").json()["history"]
    assert all(not msg.get("clarification") for msg in history)


@patch("app.main.client.models.generate_content")
def test_chat_returns_new_file_without_writing(mock_gemini, vaults_dir: Path):
    books = vaults_dir / "books"
    books.mkdir()
    (books / "chapter-1.md").write_text("Keep me", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "reply": "I pulled the tavern scene.",
        "clarification": None,
        "updated_content": None,
        "new_file": {"name": "notes/scene.md", "content": "The tavern was loud."},
    })
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Extract the tavern scene",
        "vault": "books",
        "file_path": "notes/chapter-1.md",
        "file_content": "Keep me",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["clarification"] is None
    assert body["updated_content"] is None
    assert body["new_file"] == {"name": "scene.md", "content": "The tavern was loud."}
    assert (books / "chapter-1.md").read_text(encoding="utf-8") == "Keep me"
    assert not (books / "scene.md").exists()
    assert not (books / "notes" / "scene.md").exists()

    mock_response.text = json.dumps({
        "reply": "Here is the extract.",
        "updated_content": None,
        "new_file": {"name": "..", "content": "Hello"},
    })
    repaired = client.post("/api/chat", json={
        "prompt": "Save that extract",
        "vault": "books",
        "file_path": "chapter-1.md",
        "file_content": "Keep me",
    })
    assert repaired.json()["new_file"] == {"name": "extract.md", "content": "Hello"}
    assert not (books / "extract.md").exists()


@patch("app.main.client.models.generate_content")
def test_chat_drops_invalid_new_file(mock_gemini, vaults_dir: Path):
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "reply": "Nothing to save.",
        "updated_content": None,
        "new_file": {"name": "scene.md", "content": "   "},
    })
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Extract something",
        "vault": "books",
        "file_path": "chapter-1.md",
        "file_content": "Old draft",
    })
    assert response.status_code == 200
    assert response.json()["new_file"] is None
    assert not (vaults_dir / "books" / "scene.md").exists()

    mock_response.text = json.dumps({
        "reply": "Still nothing.",
        "updated_content": None,
        "new_file": "scene.md",
    })
    again = client.post("/api/chat", json={
        "prompt": "Try again",
        "vault": "books",
        "file_path": "chapter-1.md",
        "file_content": "Old draft",
    })
    assert again.status_code == 200
    assert again.json()["new_file"] is None


def test_create_rename_move_copy_delete(vaults_dir: Path):
    created = client.post("/api/file/create", json={"vault": "books", "folder": "notes"})
    assert created.status_code == 200
    created_path = created.json()["filename"]
    assert created_path == "notes/untitled.md"
    assert (vaults_dir / "books" / created_path).is_file()

    second = client.post("/api/file/create", json={
        "vault": "books",
        "folder": "notes",
        "content": "draft text",
    })
    assert second.status_code == 200
    assert second.json()["filename"] == "notes/untitled (1).md"
    assert (vaults_dir / "books" / "notes" / "untitled (1).md").read_text() == "draft text"

    renamed = client.post("/api/file/rename", json={
        "vault": "books",
        "path": created_path,
        "new_name": "scene.md",
    })
    assert renamed.status_code == 200
    assert renamed.json()["filename"] == "notes/scene.md"

    client.post("/api/file", json={"vault": "books", "file_path": "drafts/scene.md", "content": "other"})
    conflict = client.post("/api/file/move", json={
        "vault": "books",
        "path": "notes/scene.md",
        "destination": "drafts",
    })
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["suggested"] == "scene (1).md"

    kept = client.post("/api/file/move", json={
        "vault": "books",
        "path": "notes/scene.md",
        "destination": "drafts",
        "conflict": "keep",
    })
    assert kept.status_code == 200
    assert kept.json()["filename"] == "drafts/scene (1).md"

    copied = client.post("/api/file/copy", json={
        "vault": "books",
        "path": "drafts/scene.md",
        "destination": "",
    })
    assert copied.status_code == 200
    assert copied.json()["filename"] == "scene.md"

    replaced = client.post("/api/file/copy", json={
        "vault": "books",
        "path": "drafts/scene.md",
        "destination": "",
        "conflict": "replace",
    })
    assert replaced.status_code == 400
    assert "password confirmation" in replaced.json()["detail"]

    deleted = confirmed_delete("books", "scene.md")
    assert deleted.status_code == 200
    assert not (vaults_dir / "books" / "scene.md").exists()


def test_rename_and_delete_folder(vaults_dir: Path):
    (vaults_dir / "books" / "notes").mkdir(parents=True)
    (vaults_dir / "books" / "notes" / "scene.md").write_text("draft", encoding="utf-8")

    renamed = client.post("/api/file/rename", json={
        "vault": "books",
        "path": "notes",
        "new_name": "chapters",
    })
    assert renamed.status_code == 200
    assert renamed.json()["kind"] == "folder"
    assert renamed.json()["filename"] == "chapters"
    assert (vaults_dir / "books" / "chapters" / "scene.md").read_text(encoding="utf-8") == "draft"
    assert not (vaults_dir / "books" / "notes").exists()

    removed = confirmed_delete("books", "chapters")
    assert removed.status_code == 200
    assert removed.json()["kind"] == "folder"
    assert not (vaults_dir / "books" / "chapters").exists()

    root = client.delete("/api/file?vault=books&path=.")
    assert root.status_code == 400


def test_create_and_delete_folder(vaults_dir: Path):
    created = client.post("/api/file/mkdir", json={"vault": "books", "folder": ""})
    assert created.status_code == 200
    body = created.json()
    assert body["kind"] == "folder"
    assert body["filename"] == "untitled"
    assert (vaults_dir / "books" / "untitled").is_dir()
    assert list((vaults_dir / "books" / "untitled").iterdir()) == []

    listing = client.get("/api/files?vault=books")
    assert listing.status_code == 200
    assert "untitled" in listing.json()["folders"]

    nested = client.post("/api/file/mkdir", json={"vault": "books", "folder": "untitled"})
    assert nested.status_code == 200
    assert nested.json()["filename"] == "untitled/untitled"
    assert (vaults_dir / "books" / "untitled" / "untitled").is_dir()

    second = client.post("/api/file/mkdir", json={"vault": "books", "folder": ""})
    assert second.status_code == 200
    assert second.json()["filename"] == "untitled (1)"

    conflict = client.post("/api/file/mkdir", json={
        "vault": "books",
        "folder": "",
        "name": "untitled",
    })
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["suggested"] == "untitled (2)"

    kept = client.post("/api/file/mkdir", json={
        "vault": "books",
        "folder": "",
        "name": "untitled",
        "conflict": "keep",
    })
    assert kept.status_code == 200
    assert kept.json()["filename"] == "untitled (2)"

    empty_removed = confirmed_delete("books", "untitled (2)")
    assert empty_removed.status_code == 200
    assert empty_removed.json()["kind"] == "folder"
    assert not (vaults_dir / "books" / "untitled (2)").exists()

    after = client.get("/api/files?vault=books")
    assert "untitled (2)" not in after.json()["folders"]

    nested_removed = confirmed_delete("books", "untitled")
    assert nested_removed.status_code == 200
    assert nested_removed.json()["kind"] == "folder"
    assert not (vaults_dir / "books" / "untitled").exists()

    root = client.delete("/api/file?vault=books&path=.")
    assert root.status_code == 400


def test_download_file(vaults_dir: Path):
    save = client.post("/api/file", json={
        "vault": "books",
        "file_path": "notes/scene.md",
        "content": "download me",
    })
    assert save.status_code == 200

    downloaded = client.get("/api/file/download?vault=books&path=notes/scene.md")
    assert downloaded.status_code == 200
    assert downloaded.content == b"download me"
    disposition = downloaded.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "scene.md" in disposition

    missing = client.get("/api/file/download?vault=books&path=missing.md")
    assert missing.status_code == 404

    no_path = client.get("/api/file/download?vault=books")
    assert no_path.status_code == 400

    folder = client.get("/api/file/download?vault=books&path=notes")
    assert folder.status_code == 404

    traversal = client.get("/api/file/download?vault=books&path=../secret.md")
    assert traversal.status_code == 400
