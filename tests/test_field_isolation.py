"""Safari's previous/next bar hides when only the focused field stays enabled."""

from pathlib import Path

from test_composer_keys import load_composer

ROOT = Path(__file__).resolve().parents[1]
FIELDS = load_composer((ROOT / "app/static/field-isolation.js").read_text(encoding="utf-8"))
PAGE = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")


def test_focused_field_stays_enabled_and_the_others_do_not():
    assert FIELDS["disabledDuringFocus"](True) is False
    assert FIELDS["disabledDuringFocus"](False) is True


def test_blur_does_not_reenable_a_field_that_was_already_disabled():
    assert FIELDS["disabledAfterBlur"](True) is True
    assert FIELDS["disabledAfterBlur"](False) is False


def test_find_fields_stay_disabled_when_the_bar_is_closed():
    assert FIELDS["shouldKeepDisabled"](False, True, True) is True
    assert FIELDS["shouldKeepDisabled"](True, False, True) is True
    assert FIELDS["shouldKeepDisabled"](False, False, False) is False


def test_page_isolates_the_three_textareas():
    assert PAGE.count('enterkeyhint="enter"') >= 3
    assert 'autocomplete="off"' in PAGE
    assert "WriterFields.disabledDuringFocus" in PAGE
    assert "WriterFields.disabledAfterBlur" in PAGE
    assert "WriterFields.shouldKeepDisabled" in PAGE
    assert "bindFieldIsolation(editor)" in PAGE
    assert "bindFieldIsolation(chatInput)" in PAGE
    assert "bindFieldIsolation(guppyInput)" in PAGE
    assert "if (!isiOSFieldBar()) restoreIsolatedFields()" in PAGE
    assert 'id="find-query" type="text" placeholder="Match" disabled' in PAGE
    assert 'id="find-replace" type="text" placeholder="Replace with" disabled' in PAGE
    assert 'placeholder="Find"' not in PAGE
