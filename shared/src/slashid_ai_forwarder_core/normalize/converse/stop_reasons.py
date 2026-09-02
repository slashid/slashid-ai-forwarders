"""Converse ``stopReason`` string → canonical ``AIStopReason``.

Vocabulary reference (AWS Bedrock ConverseResponse.stopReason):
    https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseResponse.html

Design note — ``guardrail_intervened`` folds into ``content_filtered``:
that's the canonical target for content-policy stops across every
vendor (matches OpenAI ``content_filter`` and Gemini ``SAFETY``). The
wire model retains ``guardrail_intervened`` for historical-event
deserialization compatibility, but normalizers must not emit it.
"""

from __future__ import annotations

from ...events import AIStopReason

_MAP: dict[str, AIStopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use": "tool_use",
    "content_filtered": "content_filtered",
    "guardrail_intervened": "content_filtered",  # deliberate fold — see module docstring
    "malformed_model_output": "malformed_model_output",
    "malformed_tool_use": "malformed_tool_use",
    "model_context_window_exceeded": "model_context_window_exceeded",
}


def map(value: str | None) -> AIStopReason:
    """Return the canonical stop reason for a Converse ``stopReason`` string.

    Unknown / empty / None input returns ``"unknown"``.
    """
    if not value:
        return "unknown"
    return _MAP.get(value, "unknown")
