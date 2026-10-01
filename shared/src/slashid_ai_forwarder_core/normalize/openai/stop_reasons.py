"""OpenAI Responses ``status`` + ``incomplete_details.reason`` → canonical ``AIStopReason``.

The Responses API has no stop-reason field; a completed response that
ends in a tool call is ``tool_use``.
"""

from __future__ import annotations

from ...events import AIStopReason

_INCOMPLETE_REASONS: dict[str, AIStopReason] = {
    "max_output_tokens": "max_tokens",
    "content_filter": "content_filtered",
}


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
