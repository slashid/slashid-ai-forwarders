"""Tests for content_utils: truncate_middle and strip_cat_n."""

from __future__ import annotations

import pytest

from slashid_bedrock_forwarder.content_utils import strip_cat_n, truncate_middle

# ---------------------------------------------------------------------------
# truncate_middle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "max_chars", "expected"),
    [
        # No truncation needed
        ("hello", 10, "hello"),
        ("hello", 5, "hello"),
        # Exact boundary: odd max_chars
        ("abcde", 3, "a…e"),
        # Even max_chars
        ("abcdef", 4, "ab…f"),
        # Hard cut when no word boundaries within tolerance
        ("a" * 200, 11, "aaaaa…aaaaa"),
        # Unicode: multi-byte codepoints are never split
        ("αβγδεζηθ", 5, "αβ…ηθ"),
        # Emoji (each is one codepoint in Python str)
        ("😀😁😂😃😄", 3, "😀…😄"),
        # Large unicode string — valid UTF-8, no surrogate halves
        ("αβγδ" * 3, 9, "αβγδ…αβγδ"),
        # Word-boundary snapping: cuts land on whitespace, not mid-word
        ("hello world foo bar baz qux quux corge grault garply", 20, "hello … garply"),
    ],
)
def test_truncate_middle(text: str, max_chars: int, expected: str) -> None:
    result = truncate_middle(text, max_chars)
    result.encode("utf-8")  # must not raise — no surrogate halves
    assert len(result) <= max_chars
    assert result == expected


# ---------------------------------------------------------------------------
# strip_cat_n
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Single line, no trailing newline
        ("     1\thello", "hello"),
        # Single line, trailing newline
        ("     1\thello\n", "hello\n"),
        # Multiple lines
        ("     1\tfoo\n     2\tbar\n", "foo\nbar\n"),
        # Multiple lines, no trailing newline
        ("     1\tfoo\n     2\tbar", "foo\nbar"),
        # Empty string
        ("", ""),
    ],
)
def test_strip_cat_n(raw: str, expected: str) -> None:
    result = strip_cat_n(raw)
    assert result == expected


def test_strip_cat_n_no_prefix_returns_none() -> None:
    """Mixed content (some lines without prefix) returns None."""
    text = "foo\n     2\tbar\n"
    assert strip_cat_n(text) is None


def test_strip_cat_n_plain_text_returns_none() -> None:
    assert strip_cat_n("just plain text\nno prefixes\n") is None


def test_strip_cat_n_partial_prefix_returns_none() -> None:
    """Only some lines have the prefix — return None (no stripping)."""
    text = "     1\twith prefix\nwithout prefix\n"
    assert strip_cat_n(text) is None
