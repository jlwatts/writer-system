"""In-flight chat bubbles stay visible, and Send unlocks if the reply never lands."""

from pathlib import Path

from test_composer_keys import load_composer

ROOT = Path(__file__).resolve().parents[1]
CHAT = load_composer((ROOT / "app/static/chat-send.js").read_text(encoding="utf-8"))
PAGE = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")


def test_history_redraw_keeps_the_inflight_bubble():
    assert CHAT["bubbleSurvivesHistory"]("loading-1", "loading-1") is True
    assert CHAT["bubbleSurvivesHistory"]("loading-1", "loading-2") is False
    assert CHAT["bubbleSurvivesHistory"]("", "loading-1") is False
    assert CHAT["bubbleSurvivesHistory"](None, None) is False


def test_send_unlocks_after_success_error_or_timeout_without_the_bubble():
    assert CHAT["buttonEnabledAfter"]("inflight") is False
    assert CHAT["buttonEnabledAfter"]("success") is True
    assert CHAT["buttonEnabledAfter"]("error") is True
    assert CHAT["buttonEnabledAfter"]("timeout") is True


def test_page_reattaches_the_bubble_and_times_out():
    assert "WriterChat.bubbleSurvivesHistory" in PAGE
    assert "WriterChat.buttonEnabledAfter" in PAGE
    assert "AbortSignal.timeout(WriterChat.chatTimeoutMs())" in PAGE
    assert "reattachInflight(channel, history)" in PAGE
    assert CHAT["chatTimeoutMs"]() == 180000
