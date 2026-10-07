"""OpenAI Responses ``status`` + ``incomplete_details.reason`` and Chat
``finish_reason`` → canonical ``AIStopReason``.

The Responses API has no stop-reason field; a completed response that
ends in a tool call is ``tool_use``.
"""

from __future__ import annotations

from ...events import AIStopReason

_INCOMPLETE_REASONS: dict[str, AIStopReason] = {
    "max_output_tokens": "max_tokens",
    "content_filter": "content_filtered",
}


_FINISH_REASONS: dict[str, AIStopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filtered",
}


def chat_stop_reason(finish_reason: str | None) -> AIStopReason:
    return _FINISH_REASONS.get(finish_reason or "", "unknown")


def responses_stop_reason(
    status: str | None, incomplete_reason: str | None, *, has_tool_call: bool
) -> AIStopReason:
    match status:
        case "completed":
            return "tool_use" if has_tool_call else "end_turn"
        case "incomplete":
            return _INCOMPLETE_REASONS.get(incomplete_reason or "", "unknown")
        case "failed":
            return "error"
        case _:
            return "unknown"
