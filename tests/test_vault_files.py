from pathlib import Path
from unittest.mock import MagicMock, patch
import os

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app, CHAT_HISTORIES

client = TestClient(app)


@pytest.fixture
def vaults_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    assert replaced.status_code == 200
    assert replaced.json()["filename"] == "scene.md"

    deleted = client.delete("/api/file?vault=books&path=scene.md")
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

    removed = client.delete("/api/file?vault=books&path=chapters")
    assert removed.status_code == 200
    assert removed.json()["kind"] == "folder"
    assert not (vaults_dir / "books" / "chapters").exists()

    root = client.delete("/api/file?vault=books&path=.")
    assert root.status_code == 400
