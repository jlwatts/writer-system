import json
import asyncio
import threading
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
    from app.main import TODO_CHAT_HISTORIES, PROMPT_FILE_SELECTIONS
    TODO_CHAT_HISTORIES.clear()
    PROMPT_FILE_SELECTIONS.clear()
    yield
    CHAT_HISTORIES.clear()
    TODO_CHAT_HISTORIES.clear()
    PROMPT_FILE_SELECTIONS.clear()

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


@patch("app.main.client.models.generate_content")
def test_file_lock_blocks_document_writes(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "chapter.md").write_text("The color was gray.", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Corrected spelling.", "updated_content": "The colour was grey.", "actions": [{"op": "add", "text": "Add milk"}]}'
    mock_gemini.return_value = mock_response

    locked = client.post("/api/chat", json={
        "prompt": "/grammar",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "The color was gray.",
        "allow_file_write": False,
    })
    assert locked.status_code == 200
    assert locked.json()["updated_content"] is None
    assert (vault / "chapter.md").read_text(encoding="utf-8") == "The color was gray."
    assert "The active document is locked." in _sent_prompt(mock_gemini)

    guppy = client.post("/api/chat", json={
        "prompt": "Add milk",
        "vault": "books",
        "channel": "todos",
        "file_path": "chapter.md",
        "file_content": "The color was gray.",
        "allow_file_write": False,
    })
    assert guppy.status_code == 200
    assert guppy.json()["updated_content"] is None
    assert (vault / "chapter.md").read_text(encoding="utf-8") == "The color was gray."
    assert any(item["text"] == "Add milk" for item in guppy.json()["items"])


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


@patch("app.main.client.models.generate_content")
def test_document_write_does_not_freeze_other_requests(mock_gemini, tmp_path, monkeypatch):
    """A snapshot holding the vault lock must not stop the event loop from answering."""
    from httpx import ASGITransport, AsyncClient

    from app import auth as auth_mod
    from app import main
    from app.history import vault_lock

    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    books = tmp_path / "books"
    books.mkdir()
    (books / "chapter.md").write_text("old", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Revised.", "updated_content": "new text", "clarification": null, "new_file": null}'
    mock_gemini.return_value = mock_response

    entered = threading.Event()
    release = threading.Event()
    held = threading.Event()
    real_write = main.write_updated_document

    def blocking_write(vault, file_path, updated_content):
        entered.set()
        return real_write(vault, file_path, updated_content)

    monkeypatch.setattr(main, "write_updated_document", blocking_write)

    def hold_lock():
        with vault_lock("books"):
            held.set()
            release.wait(timeout=8)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert held.wait(timeout=2)

    token = client.cookies.get(auth_mod.COOKIE_NAME)

    async def run():
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={auth_mod.COOKIE_NAME: token},
        ) as http:
            async def post_chat():
                return await http.post("/api/chat", json={
                    "prompt": "Revise the opening",
                    "vault": "books",
                    "channel": "writing",
                    "file_path": "chapter.md",
                    "file_content": "old",
                })

            async def history_while_writing():
                await asyncio.to_thread(entered.wait, 5)
                return await http.get("/api/chat/history?vault=books&channel=writing")

            post_task = asyncio.create_task(post_chat())
            history = await asyncio.wait_for(history_while_writing(), timeout=3)
            assert history.status_code == 200
            assert not post_task.done()
            release.set()
            return await asyncio.wait_for(post_task, timeout=5)

    try:
        response = asyncio.run(run())
    finally:
        release.set()
        holder.join(timeout=5)

    assert response.status_code == 200
    assert (books / "chapter.md").read_text(encoding="utf-8") == "new text"
    config = mock_gemini.call_args.kwargs["config"]
    assert config.automatic_function_calling.disable is True


def _paths(entries):
    return [item["path"] for item in entries]


def test_prompt_file_selection_persists_and_defaults(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "cast.md").write_text("Cast list.", encoding="utf-8")
    (vault / "notes" / "world.md").write_text("World notes.", encoding="utf-8")

    missing = client.get("/api/prompt-files", params={"vault": "books"})
    assert missing.status_code == 200
    body = missing.json()
    assert _paths(body["selected"]) == ["SKILL.md"]
    assert body["selected"][0]["label"] == "SKILL.md (built-in)"
    assert body["selected"][0]["bytes"] > 0
    assert "notes/cast.md" in _paths(body["available"])
    assert "SKILL.md" not in _paths(body["available"])

    cleared = client.put("/api/prompt-files", json={"vault": "books", "selected": []})
    assert cleared.status_code == 200
    assert cleared.json()["selected"] == []
    assert client.get("/api/prompt-files", params={"vault": "books"}).json()["selected"] == []
    assert "SKILL.md" in _paths(client.get("/api/prompt-files", params={"vault": "books"}).json()["available"])

    main.PROMPT_FILE_SELECTIONS.clear()
    main.load_prompt_files()
    reloaded_empty = client.get("/api/prompt-files", params={"vault": "books"})
    assert reloaded_empty.json()["selected"] == []

    ordered = client.put("/api/prompt-files", json={
        "vault": "books",
        "selected": ["notes/world.md", "notes/cast.md", "notes/cast.md"],
    })
    assert ordered.status_code == 200
    assert _paths(ordered.json()["selected"]) == ["notes/world.md", "notes/cast.md"]
    assert "notes/world.md" not in _paths(ordered.json()["available"])
    assert ordered.json()["selected"][0]["bytes"] == len("World notes.".encode("utf-8"))

    main.PROMPT_FILE_SELECTIONS.clear()
    main.load_prompt_files()
    reloaded = client.get("/api/prompt-files", params={"vault": "books"})
    assert _paths(reloaded.json()["selected"]) == ["notes/world.md", "notes/cast.md"]

    main.prompt_files_store_path().unlink()
    main.PROMPT_FILE_SELECTIONS.clear()
    main.load_prompt_files()
    fresh = client.get("/api/prompt-files", params={"vault": "books"})
    assert _paths(fresh.json()["selected"]) == ["SKILL.md"]


def test_prompt_files_reject_paths_outside_the_vault(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    (tmp_path / "books").mkdir()
    for bad in ("../escape.md", "/etc/passwd", "notes/../../escape.md"):
        saved = client.put("/api/prompt-files", json={"vault": "books", "selected": [bad]})
        assert saved.status_code == 400
        chat = client.post("/api/chat", json={
            "prompt": "Hi",
            "vault": "books",
            "channel": "writing",
            "context_files": [bad],
        })
        assert chat.status_code == 400
    assert "books" not in main.PROMPT_FILE_SELECTIONS


@patch("app.main.client.models.generate_content")
def test_posted_context_files_are_sent_in_order(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "world.md").write_text("WORLD-MARKER", encoding="utf-8")
    (vault / "notes" / "cast.md").write_text("CAST-MARKER", encoding="utf-8")
    (vault / "other.md").write_text("OTHER-MARKER", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Noted.", "updated_content": null}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Use the notes",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "OPEN-MARKER",
        "context_files": ["notes/world.md", "notes/cast.md"],
    })
    assert response.status_code == 200
    sent = _sent_prompt(mock_gemini)
    assert sent.index("WORLD-MARKER") < sent.index("CAST-MARKER")
    assert "--- REFERENCE notes/world.md ---" in sent
    assert "--- REFERENCE notes/cast.md ---" in sent
    assert "OTHER-MARKER" not in sent
    assert "/critique" not in sent
    assert "OPEN-MARKER" in sent
    assert "chapter.md" in sent

    guppy = client.post("/api/chat", json={
        "prompt": "What is on the list?",
        "vault": "books",
        "channel": "todos",
        "file_path": "chapter.md",
        "file_content": "OPEN-MARKER",
        "context_files": ["notes/cast.md"],
    })
    assert guppy.status_code == 200
    assert guppy.json()["channel"] == "todos"
    guppy_sent = _sent_prompt(mock_gemini)
    assert "CAST-MARKER" in guppy_sent
    assert "WORLD-MARKER" not in guppy_sent
    assert "/critique" not in guppy_sent


@patch("app.main.client.models.generate_content")
def test_saved_context_is_used_when_the_request_omits_it(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "cast.md").write_text("CAST-MARKER", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Noted.", "updated_content": null}'
    mock_gemini.return_value = mock_response

    saved = client.put("/api/prompt-files", json={"vault": "books", "selected": ["notes/cast.md"]})
    assert saved.status_code == 200
    response = client.post("/api/chat", json={
        "prompt": "Who is in the scene?",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "The door was red.",
    })
    assert response.status_code == 200
    sent = _sent_prompt(mock_gemini)
    assert "CAST-MARKER" in sent
    assert "/critique" not in sent
    assert "The door was red." in sent


@patch("app.main.client.models.generate_content")
def test_open_file_is_not_pasted_twice(mock_gemini, tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "VAULTS_DIR", tmp_path)
    vault = tmp_path / "books"
    vault.mkdir()
    (vault / "chapter.md").write_text("DISK-ONLY-PHRASE", encoding="utf-8")
    mock_response = MagicMock()
    mock_response.text = '{"reply": "Noted.", "updated_content": null}'
    mock_gemini.return_value = mock_response

    response = client.post("/api/chat", json={
        "prompt": "Tighten the opening",
        "vault": "books",
        "channel": "writing",
        "file_path": "chapter.md",
        "file_content": "EDITOR-ONLY-PHRASE",
        "context_files": ["chapter.md", "SKILL.md"],
    })
    assert response.status_code == 200
    sent = _sent_prompt(mock_gemini)
    assert sent.count("EDITOR-ONLY-PHRASE") == 1
    assert "DISK-ONLY-PHRASE" not in sent
    assert "--- REFERENCE chapter.md ---" not in sent
    assert "--- SKILL.md ---" in sent
    assert "/critique" in sent
