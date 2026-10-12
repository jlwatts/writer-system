import re

from fastapi.testclient import TestClient

from app.main import app
from app.markdown_preview import render_markdown

client = TestClient(app)


def test_headings_lists_and_emphasis():
    html = render_markdown("# Title\n\nA **bold** and *italic* word.\n\n- one\n- two\n\n1. first\n2. second\n")
    assert "<h1>Title</h1>" in html
    assert "# Title" not in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<ol>" in html and "<li>first</li>" in html


def test_hash_without_space_is_a_title():
    html = render_markdown("#something\n\nBody text\n")
    assert "<h1>something</h1>" in html
    assert "#something" not in html
    assert "<p>Body text</p>" in html


def test_fenced_code_and_safe_links():
    html = render_markdown("```\n<script>x</script>\n```\n\n[ok](https://example.com) [bad](javascript:alert(1))\n")
    assert "<script>" not in html
    assert 'href="https://example.com"' in html
    assert "href=\"javascript:" not in html.lower()


def test_raw_html_is_not_executable():
    rendered = render_markdown("<img src=x onerror=alert(1)>")
    assert not re.search(r"(?i)<[^>]*\sonerror\s*=", rendered)


def test_preview_api():
    response = client.post("/api/preview", json={"content": "## Hello\n\nWorld"})
    assert response.status_code == 200
    assert "<h2>Hello</h2>" in response.json()["html"]
    assert "<p>World</p>" in response.json()["html"]
