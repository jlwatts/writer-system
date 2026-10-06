import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app, CHAT_HISTORIES, clean_json_response

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_chat_history():
    """Resets memory state before every test run."""
    CHAT_HISTORIES.clear()
    from app.main import TODO_CHAT_HISTORIES
    TODO_CHAT_HISTORIES.clear()
    yield
    CHAT_HISTORIES.clear()
    TODO_CHAT_HISTORIES.clear()

@patch("app.main.client.models.generate_content")
def test_enqueue_chat_message(mock_gemini):
    """Verify that posting to /api/chat/enqueue stores a user item."""
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Acknowledged", "updated_content": null}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat/enqueue", json={
        "prompt": "First message",
        "vault": "test_vault"
    })

    assert response.status_code == 200
    assert response.json()["status"] == "queued"

    history_resp = client.get("/api/chat/history?vault=test_vault")
    history = history_resp.json()["history"]
    user_entries = [m for m in history if m["role"] == "user"]

    assert len(user_entries) == 1
    assert user_entries[0]["text"] == "First message"
    assert user_entries[0]["status"] in ["pending", "processing", "completed"]

@patch("app.main.client.models.generate_content")
def test_batch_prompt_consolidation(mock_gemini):
    """Verify multiple queued messages get bundled and processed together."""
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Handled both prompts", "updated_content": null}'
    mock_gemini.return_value = mock_response

    from app.main import ChatEnqueueRequest, _queue_user_message, process_chat_queue
    import asyncio

    _queue_user_message(ChatEnqueueRequest(prompt="Prompt 1", vault="test_vault"))
    _queue_user_message(ChatEnqueueRequest(prompt="Prompt 2", vault="test_vault"))
    asyncio.run(process_chat_queue("test_vault", None, None))

    # Check history after background execution completes
    history_resp = client.get("/api/chat/history?vault=test_vault")
    history = history_resp.json()["history"]

    # Should have 2 user entries and 1 AI response turn
    user_entries = [m for m in history if m["role"] == "user"]
    ai_entries = [m for m in history if m["role"] == "model"]

    assert len(user_entries) == 2
    assert len(ai_entries) == 1
    assert ai_entries[0]["text"] == "Handled both prompts"

def test_clean_json_response_accepts_raw_newlines_and_trailing_junk():
    messy = (
        'Here is the result:\n'
        '{\n'
        '  "reply": "Line one\nLine two",\n'
        '  "updated_content": "Chapter\tOne\n\nHe walked."\n'
        '}\n'
        '{"extra": true}\n'
        'Thanks!'
    )
    data = clean_json_response(messy)
    assert data["reply"] == "Line one\nLine two"
    assert data["updated_content"] == "Chapter\tOne\n\nHe walked."


def test_clean_json_response_strips_markdown_fences():
    fenced = '```json\n{"reply": "Done.", "updated_content": null}\n```\n'
    assert clean_json_response(fenced) == {"reply": "Done.", "updated_content": None}


def test_clean_json_response_repairs_unescaped_quotes():
    prose = 'He said "Hello," and then "Wait?" before leaving.'
    raw = '{"reply": "Revised.", "updated_content": "' + prose + '"}'
    data = clean_json_response(raw)
    assert data["reply"] == "Revised."
    assert data["updated_content"] == prose


def test_clean_json_response_repairs_quote_deep_in_second_line():
    """Matches production failures like 'Expecting , delimiter' near char 3092."""
    prose = ("A" * 3000) + ' She said "oops" and left.'
    raw = '{\n"reply": "Revised.", "updated_content": "' + prose + '"}'
    with pytest.raises(json.JSONDecodeError) as exc:
        json.JSONDecoder(strict=False).raw_decode(raw)
    assert "Expecting ',' delimiter" in str(exc.value)
    data = clean_json_response(raw)
    assert data["reply"] == "Revised."
    assert data["updated_content"] == prose


def test_clean_json_response_keeps_already_escaped_quotes():
    raw = '{"reply": "He said \\"hi\\"", "updated_content": null}'
    assert clean_json_response(raw) == {"reply": 'He said "hi"', "updated_content": None}


def test_clean_json_response_drops_cut_off_document():
    raw = '{"reply": "I rewrote the scene.", "updated_content": "The chapter begins'
    data = clean_json_response(raw)
    assert data["updated_content"] is None
    assert data["actions"] == []
    assert "I rewrote the scene." in data["reply"]
    assert "cut off" in data["reply"]


@patch("app.main.client.models.generate_content")
def test_chat_recovers_from_malformed_gemini_json(mock_gemini):
    mock_response = MagicMock()
    mock_response.text = (
        '{\n'
        '  "reply": "Revised.\nSee draft.",\n'
        '  "updated_content": null\n'
        '}\n'
        'note: extra'
    )
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Edit this",
        "vault": "test_vault",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Revised.\nSee draft."
    assert not body["response"].startswith("Error processing request")


@patch("app.main.client.models.generate_content")
def test_chat_repairs_unescaped_quotes_in_model_json(mock_gemini):
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Added the line.", "updated_content": "She said "stay.""}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Add a line of dialogue",
        "vault": "test_vault",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Added the line."
    assert body["updated_content"] == 'She said "stay."'
    assert not body["response"].startswith("Error processing request")


@patch("app.main.client.models.generate_content")
def test_chat_history_survives_reload(mock_gemini):
    """A process restart reloads the conversation that a browser refresh asks for."""
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Still here", "updated_content": null}'
    mock_gemini.return_value = mock_response

    sent = client.post("/api/chat", json={"prompt": "Remember this", "vault": "books"})
    assert sent.status_code == 200
    assert sent.json()["response"] == "Still here"

    from app.auth import AUTH_DIR
    stored = Path(AUTH_DIR) / "chat.json"
    assert stored.is_file()
    assert stored.parent == Path(AUTH_DIR)
    assert "vaults" not in stored.parts
    assert "Remember this" in stored.read_text(encoding="utf-8")

    CHAT_HISTORIES.clear()
    from app.main import TODO_CHAT_HISTORIES, load_chat_histories
    TODO_CHAT_HISTORIES.clear()
    assert client.get("/api/chat/history?vault=books").json()["history"] == []

    load_chat_histories()
    history = client.get("/api/chat/history?vault=books").json()["history"]
    assert [msg["text"] for msg in history] == ["Remember this", "Still here"]


def test_clear_chat_history():
    """Verify clearing history wipes conversation context for a vault."""
    # Seed history
    CHAT_HISTORIES["test_vault"] = [
        {"id": "1", "role": "user", "text": "Hello", "status": "completed"}
    ]

    response = client.post("/api/chat/clear", json={"vault": "test_vault"})
    assert response.status_code == 200

    history_resp = client.get("/api/chat/history?vault=test_vault")
    assert len(history_resp.json()["history"]) == 0


@patch("app.main.client.models.generate_content")
def test_grammar_updates_document_and_critique_does_not(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "chapter.md").write_text("The color was gray.", encoding="utf-8")

    mock_response = MagicMock()
    mock_response.text = '{"reply": "Corrected spelling.", "updated_content": "The colour was grey."}'
    mock_gemini.return_value = mock_response
    grammar = client.post("/api/chat", json={
        "prompt": "/grammar",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "The color was gray.",
    })
    assert grammar.status_code == 200
    assert grammar.json()["updated_content"] == "The colour was grey."
    assert (vault / "chapter.md").read_text(encoding="utf-8") == "The colour was grey."

    mock_response.text = '{"reply": "1. A problem.", "updated_content": "Rewritten."}'
    critique = client.post("/api/chat", json={
        "prompt": "/critique male, logical",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "The colour was grey.",
    })
    assert critique.status_code == 200
    assert critique.json()["updated_content"] is None
    assert (vault / "chapter.md").read_text(encoding="utf-8") == "The colour was grey."

    for prompt in ("/consistency", "/dialog", "/summary"):
        mock_response.text = '{"reply": "Noted.", "updated_content": "Rewritten."}'
        report = client.post("/api/chat", json={
            "prompt": prompt,
            "vault": "books",
            "channel": "writing",
            "file_path": "chapter.md",
            "file_content": "The colour was grey.",
        })
        assert report.status_code == 200
        assert report.json()["updated_content"] is None
    assert (vault / "chapter.md").read_text(encoding="utf-8") == "The colour was grey."


def _sent_prompt(mock_gemini) -> str:
    return mock_gemini.call_args.kwargs["contents"]


@patch("app.main.client.models.generate_content")
def test_writing_chat_sends_skill_file_with_document(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Noted.", "updated_content": null}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Tighten the opening",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "The door was red.",
    })
    assert response.status_code == 200
    sent = _sent_prompt(mock_gemini)
    assert "--- SKILL.md ---" in sent
    assert "/critique" in sent
    assert "/grammar" in sent
    assert "The door was red." in sent
    assert "chapter.md" in sent
    assert "Tighten the opening" in sent


@patch("app.main.client.models.generate_content")
def test_vault_skill_file_replaces_bundled_shortcuts(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "SKILL.md").write_text("## /shorten\nCut the active document in half.\n", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Noted.", "updated_content": null}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "/shorten",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "A long scene.",
    })
    assert response.status_code == 200
    sent = _sent_prompt(mock_gemini)
    assert "/shorten" in sent
    assert "A long scene." in sent
    assert "/critique" not in sent


@patch("app.main.client.models.generate_content")
def test_guppy_chat_sends_skill_file_with_document(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    (tmp_path / "books").mkdir()
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Critique.", "actions": []}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "/critique",
        "vault": "books",
        "channel": "todos",
        "file_path": "chapter.md",
        "file_content": "She walked home.",
    })
    assert response.status_code == 200
    assert response.json()["channel"] == "todos"
    sent = _sent_prompt(mock_gemini)
    assert "--- SKILL.md ---" in sent
    assert "/critique" in sent
    assert "She walked home." in sent
    assert "chapter.md" in sent
