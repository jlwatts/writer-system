from datetime import date
from unittest.mock import MagicMock, patch

from app.todos import parse_todos, serialize_todos, is_todo_channel, strip_todo_command, apply_todo_actions
from fastapi.testclient import TestClient

from app.main import app
import app.main as main

client = TestClient(app)


def test_parse_and_serialize_roundtrip():
    raw = "# Notes\n- [ ] first\n- [x] second done\nfooter\n"
    parsed = parse_todos(raw)
    assert parsed["preamble"] == "# Notes"
    assert parsed["items"] == [
        {"text": "first", "done": False, "due": None, "resolved": None},
        {"text": "second done", "done": True, "due": None, "resolved": None},
    ]
    assert parsed["trailer"] == "footer"
    assert serialize_todos(parsed["items"], parsed["preamble"], parsed["trailer"]) == (
        "# Notes\n- [ ] first\n- [x] second done\nfooter\n"
    )


def test_todos_api_edit_reorder_and_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "TODO.md").write_text(
        "- [ ] first\n- [ ] second\n- [x] third\n",
        encoding="utf-8",
    )

    loaded = client.get("/api/todos?vault=books")
    assert loaded.status_code == 200
    assert loaded.json()["items"][0]["text"] == "first"
    assert loaded.json()["items"][2]["done"] is True

    updated = [
        {"text": "second", "done": True},
        {"text": "first edited", "done": False},
        {"text": "third", "done": False},
    ]
    saved = client.put("/api/todos", json={"vault": "books", "items": updated})
    assert saved.status_code == 200
    assert saved.json()["status"] == "saved"
    assert (vault / "TODO.md").read_text(encoding="utf-8") == (
        "- [x] second\n- [ ] first edited\n- [ ] third\n"
    )


def test_todos_api_creates_file_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    missing = client.get("/api/todos?vault=books")
    assert missing.status_code == 200
    assert missing.json()["exists"] is False
    assert missing.json()["items"] == []

    created = client.put("/api/todos", json={
        "vault": "books",
        "items": [{"text": "brand new", "done": False}],
    })
    assert created.status_code == 200
    assert (tmp_path / "books" / "TODO.md").read_text(encoding="utf-8") == "- [ ] brand new\n"


def test_todo_intent_and_actions():
    assert is_todo_channel("/todo add milk")
    assert is_todo_channel("please complete the todo about drafts")
    assert is_todo_channel("hi guppy, what's next?")
    assert is_todo_channel("hello", tab="todos")
    assert not is_todo_channel("tighten the opening scene")
    assert strip_todo_command("/todo add milk") == "add milk"

    items = [
        {"text": "first", "done": False},
        {"text": "second", "done": False},
        {"text": "third", "done": True},
    ]
    updated, applied = apply_todo_actions(items, [
        {"op": "complete", "index": 1},
        {"op": "add", "text": "fourth"},
        {"op": "move", "match": "third", "position": "top"},
        {"op": "downgrade", "match": "second"},
        {"op": "uncomplete", "match": "third"},
    ], today="2026-09-09")
    assert [item["text"] for item in updated] == ["third", "first", "fourth", "second"]
    assert updated[0]["done"] is False
    assert updated[0]["resolved"] is None
    assert updated[1]["done"] is True
    assert updated[1]["resolved"] == "2026-09-09"
    assert {row["op"] for row in applied} == {"complete", "add", "move", "downgrade", "uncomplete"}


def test_todo_due_dates_parse_and_actions():
    raw = "- [ ] pay tax due:2020-01-01\n- [x] done thing due:2020-01-01 resolved:2020-02-02\n- [ ] leftover (2026-09-08 09:08)\n"
    parsed = parse_todos(raw)
    assert parsed["items"][0]["due"] == "2020-01-01"
    assert parsed["items"][0]["text"] == "pay tax"
    assert parsed["items"][1]["due"] == "2020-01-01"
    assert parsed["items"][1]["resolved"] == "2020-02-02"
    assert parsed["items"][2]["due"] is None
    assert serialize_todos(parsed["items"]) == (
        "- [ ] pay tax due:2020-01-01\n"
        "- [x] done thing due:2020-01-01 resolved:2020-02-02\n"
        "- [ ] leftover (2026-09-08 09:08)\n"
    )
    updated, applied = apply_todo_actions(parsed["items"], [
        {"op": "set_due", "match": "leftover", "due": "2026-12-25"},
        {"op": "set_due", "match": "pay tax", "due": "2020-01-02"},
        {"op": "add", "text": "new with date", "due": "2026-11-01"},
        {"op": "complete", "match": "leftover"},
    ], today="2026-09-09")
    assert updated[0]["due"] == "2020-01-01"
    assert updated[2]["due"] == "2026-12-25"
    assert updated[2]["done"] is True
    assert updated[2]["resolved"] == "2026-09-09"
    assert updated[3]["due"] == "2026-11-01"
    assert {row["op"] for row in applied} == {"set_due", "add", "complete"}


@patch("app.main.client.models.generate_content")
def test_guppy_chat_is_separate_and_mutates_todos(mock_gemini, tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    main.CHAT_HISTORIES.clear()
    main.TODO_CHAT_HISTORIES.clear()
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "TODO.md").write_text("- [ ] first\n- [ ] second\n", encoding="utf-8")

    mock_response = MagicMock()
    mock_response.text = '{"reply": "Marked first done.", "actions": [{"op":"complete","index":1}]}'
    mock_gemini.return_value = mock_response

    guppy = client.post("/api/chat", json={
        "prompt": "/todo complete the first item",
        "vault": "books",
    })
    assert guppy.status_code == 200
    assert guppy.json()["channel"] == "todos"
    assert guppy.json()["items"][0]["done"] is True
    assert guppy.json()["items"][0]["resolved"] == date.today().isoformat()
    assert (vault / "TODO.md").read_text(encoding="utf-8") == (
        f"- [x] first resolved:{date.today().isoformat()}\n- [ ] second\n"
    )

    writing = client.post("/api/chat", json={
        "prompt": "What is the theme of this chapter?",
        "vault": "books",
        "file_content": "A quiet opening.",
    })
    assert writing.status_code == 200
    assert writing.json()["channel"] == "writing"

    writing_history = client.get("/api/chat/history?vault=books&channel=writing").json()["history"]
    todo_history = client.get("/api/chat/history?vault=books&channel=todos").json()["history"]
    assert any("theme" in (msg.get("text") or "") for msg in writing_history)
    assert any("complete the first item" in (msg.get("text") or "") for msg in todo_history)
    assert all("theme" not in (msg.get("text") or "") for msg in todo_history)


@patch("app.main.client.models.generate_content")
def test_guppy_delete_downgrades_instead(mock_gemini, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    main.CHAT_HISTORIES.clear()
    main.TODO_CHAT_HISTORIES.clear()
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "TODO.md").write_text("- [ ] keep me\n- [ ] demote me\n- [ ] tail\n", encoding="utf-8")

    mock_response = MagicMock()
    mock_response.text = '{"reply": "Demoted it.", "actions": [{"op":"delete","match":"demote me"}]}'
    mock_gemini.return_value = mock_response

    guppy = client.post("/api/chat", json={
        "prompt": "/todo delete demote me",
        "vault": "books",
    })
    assert guppy.status_code == 200
    texts = [item["text"] for item in guppy.json()["items"]]
    assert texts == ["keep me", "tail", "demote me"]
    assert guppy.json()["actions"] == [{"op": "downgrade", "text": "demote me", "to": 3}]
    assert (vault / "TODO.md").read_text(encoding="utf-8") == (
        "- [ ] keep me\n- [ ] tail\n- [ ] demote me\n"
    )
