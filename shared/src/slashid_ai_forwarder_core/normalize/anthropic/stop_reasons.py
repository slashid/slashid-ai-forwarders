"""Anthropic response ``stop_reason`` string → canonical ``AIStopReason``.

Vocabulary reference (Anthropic Messages API):
    https://docs.anthropic.com/en/api/messages

Values Anthropic emits (as of 2026-01):
    end_turn, max_tokens, stop_sequence, tool_use, pause_turn,
    refusal, malformed_model_output

Anything else — new values, empty string, None — maps to ``"unknown"``.
Kept as a small module-level dict for O(1) lookup; grow the dict when
Anthropic adds new reasons.
"""

from __future__ import annotations

from ...events import AIStopReason

_MAP: dict[str, AIStopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use": "tool_use",
    "pause_turn": "pause_turn",
    "refusal": "refusal",
    "malformed_model_output": "malformed_model_output",
}


def map(value: str | None) -> AIStopReason:
    """Return the canonical stop reason for an Anthropic ``stop_reason`` string.

    Unknown / empty / None input returns ``"unknown"``.
    """
    if not value:
        return "unknown"
    return _MAP.get(value, "unknown")
