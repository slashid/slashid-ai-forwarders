"""Conversation-turn boundary helpers.

An assistant message marks a turn boundary in every AI conversation shape
we canonicalize: the model produced it, then the user (or tool runtime)
follows up. "Fresh" content for the current invocation is everything
AFTER the last assistant message in the input history — earlier
attachments, tool results, or user turns were already emitted on prior
invocation events.

``after_last_assistant`` is duck-typed via a ``.role`` attribute — works
for ``NormalizedMessage`` (canonical) and for vendor-specific typed
messages like ``ConverseRequestMessage``. Same rule, one implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class _MessageWithRole(Protocol):
    role: str


def after_last_assistant[MessageT: _MessageWithRole](
    messages: Sequence[MessageT],
) -> Sequence[MessageT]:
    """Return the tail of ``messages`` starting just after the last
    ``role == "assistant"`` message.

    If no assistant message is present, returns the whole sequence
    unchanged (every message is "fresh" for the first turn).
    """
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.role == "assistant"),
        default=-1,
    )
    return messages[last_assistant + 1 :]
