"""iOS fields retain native focus so the software keyboard stays open."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")


def test_page_does_not_install_ios_field_isolation():
    assert PAGE.count('enterkeyhint="enter"') >= 3
    assert 'autocomplete="off"' in PAGE
    assert 'id="editor-textarea" inputmode="text"' in PAGE
    assert "/static/field-isolation.js" not in PAGE
    assert "bindFieldIsolation" not in PAGE
    assert "isolateField" not in PAGE
    assert "isiOSFieldBar" not in PAGE
    assert "document.addEventListener('focusin'" not in PAGE
    assert "if (field.disabled) field.disabled = false" not in PAGE


def test_closed_find_fields_remain_intentionally_disabled():
    assert 'id="find-query" type="text" placeholder="Match" disabled' in PAGE
    assert 'id="find-replace" type="text" placeholder="Replace with" disabled' in PAGE
    assert 'placeholder="Find"' not in PAGE
