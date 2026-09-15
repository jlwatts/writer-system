import math
import re

WORD_RE = re.compile(r"\S+")
DEFAULT_WPM = 200


def word_count(text: str | None) -> int:
    if not text:
        return 0
    return len(WORD_RE.findall(text))


def reading_time_minutes(text: str | None, wpm: int = DEFAULT_WPM) -> int:
    words = word_count(text)
    if words <= 0:
        return 0
    pace = wpm if wpm > 0 else DEFAULT_WPM
    return max(1, math.ceil(words / pace))
