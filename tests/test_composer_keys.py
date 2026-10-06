"""Behavior of the composer double-Return rule.

The page loads app/static/composer-keys.js. These tests execute that file
through a small translator so the suite does not need a JavaScript runtime.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "app" / "static" / "composer-keys.js"
PAGE_PATH = ROOT / "app" / "templates" / "index.html"

_REGEX_TEST = re.compile(r"/((?:\\.|[^/])*)/\.test\(([^)]+)\)")


def _translate_expr(expr: str) -> str:
    expr = expr.strip()

    def regex_test(match: re.Match) -> str:
        pattern = match.group(1).replace("\\", "\\\\")
        inner = _translate_expr(match.group(2))
        return f"(re.search('{pattern}', {inner}) is not None)"

    expr = _REGEX_TEST.sub(regex_test, expr)
    expr = re.sub(r"\bString\((.*)\)", r"str(\1)", expr)
    expr = expr.replace("Math.min", "min")
    expr = expr.replace("===", "==")
    expr = expr.replace("&&", " and ")
    expr = expr.replace("||", " or ")
    expr = expr.replace("!", " not ")
    expr = re.sub(r"\bfalse\b", "False", expr)
    expr = re.sub(r"\btrue\b", "True", expr)
    return expr


def _translate_body(body: str) -> list[str]:
    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("var "):
            name, expr = line[4:].split("=", 1)
            lines.append(f"{name.strip()} = {_translate_expr(expr.strip().rstrip(';'))}")
            continue
        conditional = re.match(r"if \((.*)\) return (.*);$", line)
        if conditional:
            lines.append(f"if {_translate_expr(conditional.group(1))}:")
            lines.append(f"    return {_translate_expr(conditional.group(2))}")
            continue
        returned = re.match(r"return (.*);$", line)
        if returned:
            lines.append(f"return {_translate_expr(returned.group(1))}")
            continue
        raise AssertionError(f"untranslated composer line: {line}")
    return lines


def load_composer(source: str | None = None) -> dict:
    source = JS_PATH.read_text(encoding="utf-8") if source is None else source
    source = re.sub(r"//.*?$", "", source, flags=re.M)
    header = re.compile(r"function (\w+)\(([^)]*)\) \{")
    defs = []
    for match in header.finditer(source):
        name = match.group(1)
        args = [part.strip() for part in match.group(2).split(",") if part.strip()]
        depth = 1
        index = match.end()
        while index < len(source) and depth:
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
            index += 1
        body = _translate_body(source[match.end() : index - 1])
        indented = "\n".join(f"    {line}" for line in body)
        defs.append(f"def {name}({', '.join(args)}):\n{indented}")
    namespace = {"re": re}
    exec("\n\n".join(defs), namespace)
    return namespace


COMPOSER = load_composer()


class Env(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def env(**overrides):
    base = {
        "userAgent": "Mozilla/5.0 (X11; Linux x86_64)",
        "maxTouchPoints": 0,
        "screenWidth": 1440,
        "screenHeight": 900,
        "landscape": True,
        "coarsePointer": False,
        "awaiting": False,
    }
    base.update(overrides)
    return Env(base)


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"


def test_iphone_double_enter_does_not_send():
    for landscape in (False, True):
        phone = env(userAgent=IPHONE, maxTouchPoints=5, screenWidth=390, screenHeight=844, landscape=landscape, coarsePointer=True)
        assert COMPOSER["isIPhone"](phone) is True
        assert COMPOSER["sendByDoubleEnter"](phone) is False
        assert COMPOSER["doubleEnterSends"]("Revise the scene\n\n", phone) is False


def test_iphone_desktop_site_is_still_a_phone():
    phone = env(userAgent=MAC, maxTouchPoints=5, screenWidth=430, screenHeight=932, landscape=False, coarsePointer=True)
    assert COMPOSER["isIPhone"](phone) is True
    assert COMPOSER["doubleEnterSends"]("Note\n\n", phone) is False


def test_ipad_keeps_double_enter_send():
    pad = env(userAgent=IPAD, maxTouchPoints=5, screenWidth=1024, screenHeight=768, landscape=True, coarsePointer=True)
    assert COMPOSER["isIPhone"](pad) is False
    assert COMPOSER["doubleEnterSends"]("Edit this\n\n", pad) is True
    portrait = env(**{**pad, "landscape": False, "screenWidth": 768, "screenHeight": 1024})
    assert COMPOSER["doubleEnterSends"]("Edit this\n\n", portrait) is True


def test_ipad_desktop_site_is_not_a_phone():
    pad = env(userAgent=MAC, maxTouchPoints=5, screenWidth=1024, screenHeight=768, landscape=True, coarsePointer=True)
    assert COMPOSER["isIPhone"](pad) is False
    assert COMPOSER["sendByDoubleEnter"](pad) is True


def test_desktop_landscape_uses_modifier_instead_of_double_enter():
    desktop = env(landscape=True, coarsePointer=False)
    assert COMPOSER["doubleEnterSends"]("Paragraph\n\n", desktop) is False
    portrait_window = env(landscape=False, coarsePointer=False)
    assert COMPOSER["doubleEnterSends"]("Paragraph\n\n", portrait_window) is True


def test_single_return_never_sends():
    pad = env(userAgent=IPAD, maxTouchPoints=5, screenWidth=768, screenHeight=1024, landscape=False, coarsePointer=True)
    assert COMPOSER["doubleEnterSends"]("Still typing\n", pad) is False
    assert COMPOSER["doubleEnterSends"]("", pad) is False


def test_placeholders_follow_the_same_rule():
    phone = env(userAgent=IPHONE, maxTouchPoints=5, screenWidth=390, screenHeight=844, landscape=False, coarsePointer=True)
    assert COMPOSER["composerPlaceholder"]("writing", phone) == "Ask AI or instruct edits…"
    assert COMPOSER["composerPlaceholder"]("todos", phone) == "Ask Guppy…"
    phone["landscape"] = True
    assert "Ctrl+Enter" in COMPOSER["composerPlaceholder"]("writing", phone)

    pad = env(userAgent=IPAD, maxTouchPoints=5, screenWidth=768, screenHeight=1024, landscape=False, coarsePointer=True)
    assert "double-tap return" in COMPOSER["composerPlaceholder"]("writing", pad)

    waiting = env(**{**phone, "awaiting": True})
    assert COMPOSER["composerPlaceholder"]("writing", waiting) == "Answer to continue..."


def test_page_uses_the_shared_rule():
    page = PAGE_PATH.read_text(encoding="utf-8")
    assert "/static/composer-keys.js" in page
    assert "WriterComposer.doubleEnterSends(this.value, composerEnv())" in page
    assert "WriterComposer.composerPlaceholder(" in page
    assert "function isIPhone(" not in page
    assert "function sendByDoubleEnter(" not in page


if __name__ == "__main__":
    for test_name, test_fn in list(globals().items()):
        if test_name.startswith("test_"):
            test_fn()
            print(f"ok {test_name}")
