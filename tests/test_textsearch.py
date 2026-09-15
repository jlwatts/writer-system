from app.textsearch import find_spans, replace_all, replace_at


def test_literal_case_and_whole_word():
    text = "Foo foo food Foo"
    spans, err = find_spans(text, "foo", case_sensitive=False)
    assert err is None
    assert spans == [(0, 3), (4, 7), (8, 11), (13, 16)]

    spans, err = find_spans(text, "foo", case_sensitive=True)
    assert spans == [(4, 7), (8, 11)]

    spans, err = find_spans(text, "foo", whole_word=True)
    assert spans == [(0, 3), (4, 7), (13, 16)]


def test_regex_and_invalid_regex():
    text = "cat cap cop"
    spans, err = find_spans(text, r"c.p", regex=True)
    assert err is None
    assert spans == [(4, 7), (8, 11)]

    spans, err = find_spans(text, "(", regex=True)
    assert spans == []
    assert err and "Invalid regex" in err


def test_replace_all_and_replace_at():
    text = "alpha beta alpha"
    updated, count, err = replace_all(text, "alpha", "A")
    assert err is None
    assert count == 2
    assert updated == "A beta A"

    updated, idx, err = replace_at(text, "alpha", "A", 1)
    assert err is None
    assert idx == 1
    assert updated == "alpha beta A"


def test_empty_query_is_noop():
    assert find_spans("hello", "") == ([], None)
    updated, count, err = replace_all("hello", "", "x")
    assert (updated, count, err) == ("hello", 0, None)
