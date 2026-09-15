import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app, CHAT_HISTORIES

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
