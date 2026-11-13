from wordcount import count_words


def test_single_spaces():
    assert count_words("one two three") == 3


def test_more_than_one_space():
    assert count_words("one  two   three") == 3


def test_empty_text():
    assert count_words("") == 0


def test_tabs_and_line_breaks():
    assert count_words("one\ttwo\nthree") == 3
