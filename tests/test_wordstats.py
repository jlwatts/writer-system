from app.wordstats import reading_time_minutes, word_count


def test_word_count_ignores_extra_whitespace():
    assert word_count("") == 0
    assert word_count(None) == 0
    assert word_count("  one   two\nthree ") == 3


def test_reading_time_rounds_up():
    assert reading_time_minutes("") == 0
    assert reading_time_minutes("word") == 1
    assert reading_time_minutes(" ".join(["w"] * 200), wpm=200) == 1
    assert reading_time_minutes(" ".join(["w"] * 201), wpm=200) == 2
