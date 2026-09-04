"""Anthropic response ``stop_reason`` string → canonical ``AIStopReason``.

Vocabulary reference (Anthropic Messages API):
    https://docs.anthropic.com/en/api/messages

Values Anthropic emits (as of 2026-01):
    end_turn, max_tokens, stop_sequence, tool_use, pause_turn,
    refusal, malformed_model_output

Callers use ``STOP_REASONS.get(raw or "", "unknown")`` — dict miss on
unknown / empty / None falls through to ``"unknown"`` naturally.
"""

from __future__ import annotations

from ...events import AIStopReason

STOP_REASONS: dict[str, AIStopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use": "tool_use",
    "pause_turn": "pause_turn",
    "refusal": "refusal",
    "malformed_model_output": "malformed_model_output",
}
