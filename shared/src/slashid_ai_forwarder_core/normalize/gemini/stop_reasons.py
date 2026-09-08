"""Gemini ``finishReason`` string → canonical ``AIStopReason``.

Vocabulary reference (Google Cloud Vertex AI FinishReason enum):
    https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/GenerateContentResponse#finishreason

Design notes:
- Every ``SAFETY``/``BLOCKLIST``/``PROHIBITED_CONTENT``/``SPII``/
  ``IMAGE_SAFETY`` value folds into ``content_filtered`` — matches the
  Converse ``guardrail_intervened`` fold and OpenAI's ``content_filter``.
- ``RECITATION``, ``LANGUAGE``, ``OTHER``, ``UNEXPECTED_TOOL_CALL`` all
  collapse to ``unknown``: they're rare, and the wire vocabulary has no
  dedicated bucket for them.
- ``MALFORMED_FUNCTION_CALL`` maps to ``malformed_tool_use`` — matches
  the shape of a bad tool-use emit; ``model_context_window_exceeded``
  is Bedrock-only, not surfaced here.

Callers use ``STOP_REASONS.get(raw or "", "unknown")``.
"""

from __future__ import annotations

from ...events import AIStopReason

STOP_REASONS: dict[str, AIStopReason] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "content_filtered",
    "RECITATION": "unknown",
    "LANGUAGE": "unknown",
    "OTHER": "unknown",
    "BLOCKLIST": "content_filtered",
    "PROHIBITED_CONTENT": "content_filtered",
    "SPII": "content_filtered",
    "MALFORMED_FUNCTION_CALL": "malformed_tool_use",
    "IMAGE_SAFETY": "content_filtered",
    "UNEXPECTED_TOOL_CALL": "unknown",
}
