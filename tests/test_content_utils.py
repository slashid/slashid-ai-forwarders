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
        # Long string
        ("a" * 200, 11, "aaaaa…aaaaa"),
        # Unicode: multi-byte codepoints are never split
        ("αβγδεζηθ", 5, "αβ…ηθ"),
        # Emoji (each is one codepoint in Python str)
        ("😀😁😂😃😄", 3, "😀…😄"),
    ],
)
def test_truncate_middle(text: str, max_chars: int, expected: str) -> None:
    result = truncate_middle(text, max_chars)
    assert result == expected
    assert len(result) <= max_chars


def test_truncate_middle_result_is_valid_str() -> None:
    """Result encodes cleanly to UTF-8 — no surrogate halves."""
    text = "αβγδ" * 1000
    result = truncate_middle(text, 101)
    result.encode("utf-8")  # must not raise
    assert "…" in result
    assert len(result) <= 101


def test_truncate_middle_snaps_to_word_boundary() -> None:
    """Cuts are nudged to the nearest word boundary within tolerance."""
    text = "hello world foo bar baz qux quux corge grault garply"
    result = truncate_middle(text, 20)
    assert "…" in result
    assert len(result) <= 20
    head, _, tail = result.partition("…")
    # Head should end at a word boundary (space or start of word)
    assert (
        head == ""
        or not head[-1].isalnum()
        or (len(head) < len(text) and not text[len(head)].isalnum())
    )
    # Tail should start at a word boundary
    assert tail == "" or not tail[0].isalnum() or (not text[len(text) - len(tail) - 1].isalnum())


def test_truncate_middle_hard_cut_when_no_boundary_in_tolerance() -> None:
    """Falls back to hard cut when no word boundary is within tolerance."""
    text = "a" * 200  # no word boundaries at all
    result = truncate_middle(text, 11)
    assert result == "aaaaa…aaaaa"  # exact hard cut


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
