from fastapi.testclient import TestClient

from app.main import app
from app.markdown_preview import render_markdown

client = TestClient(app)


def test_headings_lists_and_emphasis():
    html = render_markdown("# Title\n\nA **bold** and *italic* word.\n\n- one\n- two\n\n1. first\n2. second\n")
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<ol>" in html and "<li>first</li>" in html


def test_fenced_code_and_safe_links():
    html = render_markdown("```\n<script>x</script>\n```\n\n[ok](https://example.com) [bad](javascript:alert(1))\n")
    assert "<script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert 'href="https://example.com"' in html
    assert "javascript:" not in html
    assert "[bad](javascript:alert(1))" in html


def test_raw_html_is_escaped():
    html = render_markdown("<img src=x onerror=alert(1)>")
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_preview_api():
    response = client.post("/api/preview", json={"content": "## Hello\n\nWorld"})
    assert response.status_code == 200
    assert "<h2>Hello</h2>" in response.json()["html"]
    assert "<p>World</p>" in response.json()["html"]
