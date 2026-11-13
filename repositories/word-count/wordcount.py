"""Count the words of a text."""


def count_words(text: str) -> int:
    """Return the number of words in a text. White space separates the words."""
    return len(text.split(" "))
