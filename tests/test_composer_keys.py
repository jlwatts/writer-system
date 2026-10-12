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


def sends(key, shift=False, ctrl=False, meta=False, alt=False):
    return COMPOSER["shiftEnterSends"](key, shift, ctrl, meta, alt)


def test_shift_enter_sends_on_iphone_ipad_and_desktop():
    """The send chord does not depend on the device. Shift+Enter is the only one."""
    assert sends("Enter", shift=True) is True


def test_plain_enter_does_not_send():
    assert sends("Enter") is False
    assert sends("a", shift=True) is False


def test_ctrl_cmd_and_alt_enter_do_not_send():
    assert sends("Enter", shift=True, ctrl=True) is False
    assert sends("Enter", shift=True, meta=True) is False
    assert sends("Enter", shift=True, alt=True) is False
    assert sends("Enter", ctrl=True) is False
    assert sends("Enter", meta=True) is False


def test_placeholders_use_shift_enter_everywhere():
    waiting = env(awaiting=True)
    idle = env(awaiting=False)
    assert COMPOSER["composerPlaceholder"]("writing", waiting) == "Answer to continue..."
    for device in (
        env(userAgent=IPHONE, maxTouchPoints=5, screenWidth=390, screenHeight=844, landscape=False, coarsePointer=True),
        env(userAgent=IPAD, maxTouchPoints=5, screenWidth=1024, screenHeight=768, landscape=True, coarsePointer=True),
        env(landscape=True, coarsePointer=False),
    ):
        writing = COMPOSER["composerPlaceholder"]("writing", device)
        todos = COMPOSER["composerPlaceholder"]("todos", device)
        assert "Shift+Enter" in writing
        assert "Shift+Enter" in todos
        assert "Ctrl+Enter" not in writing
        assert "double-tap" not in writing
    assert COMPOSER["composerPlaceholder"]("writing", idle).startswith("Ask AI")
    assert "doubleEnterSends" not in JS_PATH.read_text(encoding="utf-8")
    assert "isIPhone" not in JS_PATH.read_text(encoding="utf-8")


def test_page_uses_shift_enter_only():
    page = PAGE_PATH.read_text(encoding="utf-8")
    assert "/static/composer-keys.js" in page
    assert "WriterComposer.shiftEnterSends(" in page
    assert "WriterComposer.composerPlaceholder(" in page
    assert "composerPlaceholder('writing')" not in page
    assert "function setChatAwaiting(waiting)" in page
    assert "syncComposerPlaceholders();" in page
    assert "doubleEnterSends" not in page
    assert "Ctrl+Enter" not in page
    assert "function isIPhone(" not in page
    assert "function sendByDoubleEnter(" not in page


if __name__ == "__main__":
    for test_name, test_fn in list(globals().items()):
        if test_name.startswith("test_"):
            test_fn()
            print(f"ok {test_name}")
