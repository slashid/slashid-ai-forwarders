"""Pure string utilities for content normalization and truncation."""

from __future__ import annotations

import re

_CAT_N_LINE = re.compile(r"^\s*\d+\t", re.MULTILINE)

_WORD_BOUNDARY = re.compile(r"\b")
# Max chars we'll give up to land on a word boundary when truncating.
# Must match s3._SNAP_TOLERANCE.
SNAP_TOLERANCE = 10


def truncate_middle(text: str, max_chars: int) -> str:
    """Truncate ``text`` to at most ``max_chars`` unicode characters using middle elision.

    Splits on character (unicode scalar) boundaries — never mid-codepoint.
    Python str indexing always operates on codepoints, so slicing is safe.
    The ellipsis character (U+2026) occupies one slot, so each half gets
    (max_chars - 1) // 2 characters.

    Each cut is nudged to the nearest word boundary within SNAP_TOLERANCE
    characters. If no boundary is found within tolerance the hard cut is used.
    """
    if len(text) <= max_chars:
        return text
    # head gets the larger half when max_chars-1 is odd.
    tail_len = (max_chars - 1) // 2
    head_len = max_chars - 1 - tail_len

    # Snap head cut backwards to the closest word boundary within tolerance.
    # We want the largest boundary position strictly less than head_len,
    # and strictly greater than 0 (so head is never empty).
    # Note: finditer's endpos is not fully exclusive for zero-width \b matches,
    # so we filter candidates explicitly.
    head_end = head_len
    lo = max(1, head_len - SNAP_TOLERANCE)
    for m in reversed(list(_WORD_BOUNDARY.finditer(text, lo, head_len + 1))):
        pos = m.start()
        if lo <= pos < head_len:
            head_end = pos
            break

    # Tail budget is whatever remains after the actual head and the ellipsis.
    actual_tail_len = max_chars - head_end - 1
    tail_start = len(text) - actual_tail_len

    # Snap tail cut forwards to the closest word boundary within tolerance.
    # Accept the first boundary strictly after tail_start that still leaves
    # at least (actual_tail_len - tolerance) chars in the tail.
    min_tail = max(1, actual_tail_len - SNAP_TOLERANCE)
    for m in _WORD_BOUNDARY.finditer(text, tail_start + 1, len(text) + 1):
        candidate = m.start()
        if tail_start < candidate <= len(text) - min_tail:
            tail_start = candidate
        break

    return text[:head_end] + "…" + text[tail_start:]


def strip_cat_n(text: str) -> str:
    """Strip Claude Code's ``cat -n`` line-number prefixes if every non-empty line has one.

    Returns the stripped text when all non-empty lines carry the prefix,
    or the original text unchanged when the format doesn't match.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    if not all(_CAT_N_LINE.match(ln) for ln in lines if ln.strip()):
        return text
    return _CAT_N_LINE.sub("", text)
