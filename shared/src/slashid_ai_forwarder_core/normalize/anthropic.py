"""Anthropic Messages API shape ↔ Converse shape pure transforms.

Reused by:

- ``bedrock/mil_normalize.py`` — envelope wraps at
  ``record["input"]["inputBodyJson"]`` / ``record["output"]["outputBodyJson"]``.
- Future ``vertex/log_normalize.py`` — envelope wraps at
  ``protoPayload.request`` / ``protoPayload.response`` for Vertex's
  ``rawPredict`` path on Anthropic models.

All functions here are pure: payload in, transformed payload out, no
envelope knowledge and no in-place mutation of caller state.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

log = logging.getLogger(__name__)


# Anthropic streaming events use a small closed vocabulary of `type` values.
# Presence of any of these is the signature that says "this list is an
# Anthropic Messages SSE decode." Non-Anthropic Bedrock streams (Nova,
# Titan, Cohere, ...) use their own event vocabularies and won't match.
_ANTHROPIC_STREAM_EVENT_TYPES = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "ping",
    }
)


def looks_like_anthropic_stream(events: Any) -> bool:
    """Detect an Anthropic streaming Messages response by event-type markers.

    Any list at a streaming-response slot is a Bedrock/Vertex SSE decode,
    but the event vocabulary is per-vendor — we only claim ownership of
    Anthropic's. Non-Anthropic streams are left alone by the caller.
    """
    if not isinstance(events, list):
        return False
    return any(
        isinstance(e, dict) and e.get("type") in _ANTHROPIC_STREAM_EVENT_TYPES for e in events
    )


def looks_like_anthropic_message(body: Any) -> bool:
    """Detect a non-streaming Anthropic Messages response by shape triad.

    Anthropic's response envelope always carries all three markers:
    ``type: "message"``, ``role: "assistant"``, and a ``content`` list.
    Requiring all three avoids false-positives on unrelated shapes that
    happen to reuse one of the fields.

    Also short-circuit on the presence of ``output``, which is Converse's
    top-level wrapper — cheapest possible negative check.
    """
    if not isinstance(body, dict) or "output" in body:
        return False
    return (
        body.get("type") == "message"
        and body.get("role") == "assistant"
        and isinstance(body.get("content"), list)
    )


def anthropic_tools_to_converse_tool_config(tools: Any) -> dict[str, Any]:
    """Convert Anthropic flat ``tools[]`` → Converse ``toolConfig`` dict.

    Accepts ``Any`` so callers can pass through whatever vendor data
    lands in the ``tools`` field without pre-validation; returns an
    empty dict on non-list / empty input.
    """
    if not isinstance(tools, list) or not tools:
        return {}
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "inputSchema": {"json": t.get("input_schema") or {}},
                }
            }
            for t in tools
            if isinstance(t, dict)
        ]
    }


def anthropic_message_to_converse(body: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a non-streaming Anthropic Messages response → Converse shape.

    Anthropic::

        {type: "message", role: "assistant",
         content: [{type: "text", text}, {type: "tool_use", id, name, input},
                   {type: "thinking", thinking}],
         stop_reason: "..."}

    Converse::

        {output: {message: {role: "assistant",
                            content: [{text}, {toolUse: {toolUseId, name, input}}]}},
         stopReason: "..."}
    """
    content: list[dict[str, Any]] = []
    for block in body.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            content.append({"text": block.get("text", "")})
        elif btype == "tool_use":
            content.append(
                {
                    "toolUse": {
                        "toolUseId": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input") or {},
                    }
                }
            )
        elif btype == "thinking":
            # Thinking / reasoning content gets folded into a plain text block
            # so it lands in the input/output content hash the same as any
            # other assistant-visible text. Matches the streaming path
            # (`anthropic_stream_to_converse`) — downstream code doesn't
            # need a separate concept for reasoning vs. reply text. Note:
            # when SLASHID_INCLUDE_RAW_CONTENT=true this text does travel to
            # SlashID; product policy is that reasoning is part of the model
            # output surface, not privileged internal state.
            content.append({"text": block.get("thinking", "")})

    result: dict[str, Any] = {"output": {"message": {"role": "assistant", "content": content}}}
    stop_reason = body.get("stop_reason")
    if isinstance(stop_reason, str) and stop_reason:
        result["stopReason"] = stop_reason
    return result


def anthropic_stream_to_converse(events: list[Any]) -> dict[str, Any]:
    """Walk Anthropic SSE-style events and produce a Converse-shape output body."""
    content: list[dict[str, Any]] = []
    by_index: dict[int, dict[str, Any]] = {}
    stop_reason: str | None = None

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")

        if etype == "content_block_start":
            idx = int(event.get("index", 0))
            cb = event.get("content_block") or {}
            cb_type = cb.get("type")
            if cb_type == "text":
                by_index[idx] = {"_type": "text", "text": cb.get("text", "")}
            elif cb_type == "tool_use":
                by_index[idx] = {
                    "_type": "tool_use",
                    "toolUseId": cb.get("id"),
                    "name": cb.get("name"),
                    "input_buf": "",
                }
            elif cb_type == "thinking":
                by_index[idx] = {"_type": "thinking", "text": ""}
            else:
                by_index[idx] = {"_type": cb_type or "unknown"}

        elif etype == "content_block_delta":
            idx = int(event.get("index", 0))
            delta = event.get("delta") or {}
            slot = by_index.get(idx)
            if slot is None:
                continue
            if delta.get("type") == "text_delta":
                slot["text"] = slot.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                slot["input_buf"] = slot.get("input_buf", "") + delta.get("partial_json", "")
            elif delta.get("type") == "thinking_delta":
                slot["text"] = slot.get("text", "") + delta.get("thinking", "")

        elif etype == "content_block_stop":
            idx = int(event.get("index", 0))
            slot = by_index.pop(idx, None)
            if slot is None:
                continue
            block_type = slot.get("_type")
            if block_type == "text":
                content.append({"text": slot.get("text", "")})
            elif block_type == "tool_use":
                parsed_input: dict[str, Any] = {}
                buf = slot.get("input_buf") or ""
                if buf:
                    try:
                        parsed_input = cast("dict[str, Any]", json.loads(buf))
                    except json.JSONDecodeError:
                        # Length only — the buffer can hold tool arguments and
                        # we don't want those bytes in CloudWatch logs.
                        log.warning("tool_use input_json malformed (%d bytes)", len(buf))
                content.append(
                    {
                        "toolUse": {
                            "toolUseId": slot.get("toolUseId"),
                            "name": slot.get("name"),
                            "input": parsed_input,
                        }
                    }
                )
            elif block_type == "thinking":
                content.append({"text": slot.get("text", "")})

        elif etype == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                stop_reason = delta["stop_reason"]

    result: dict[str, Any] = {"output": {"message": {"role": "assistant", "content": content}}}
    if stop_reason:
        result["stopReason"] = stop_reason
    return result


def extract_anthropic_stream_usage(events: list[Any]) -> dict[str, Any]:
    """Collapse ``message_start.message.usage`` and ``message_delta.usage`` into one dict.

    Streaming carries usage in two places: ``message_start`` (initial counts,
    including cache-read / cache-creation) and ``message_delta`` (final
    output count). Later values overwrite earlier ones on key collision —
    ``message_delta`` is authoritative for output_tokens.
    """
    out: dict[str, Any] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "message_start":
            m = event.get("message") or {}
            usage = m.get("usage") or {}
            if isinstance(usage, dict):
                out.update(usage)
        elif etype == "message_delta":
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                out.update(usage)
    return out
