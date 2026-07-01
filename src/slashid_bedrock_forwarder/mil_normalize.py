"""Normalize Bedrock MIL records to the Converse shape.

Bedrock's Model Invocation Logging emits records in whatever shape the
caller's API uses:

- **Converse / ConverseStream** — `input.inputBodyJson.toolConfig.tools[].toolSpec`,
  `output.outputBodyJson.output.message.content[]` (single dict response).
- **InvokeModelWithResponseStream against Anthropic models** — Anthropic's
  native Messages API streamed: `input.inputBodyJson.tools[]` (flat) and
  `output.outputBodyJson` as a *list* of streaming events.
- **InvokeModel (non-streaming) against Anthropic models** — Anthropic's
  native Messages API single-shot: `input.inputBodyJson.tools[]` (flat) and
  `output.outputBodyJson` as a *dict* with top-level `content: [...]` and
  `stop_reason` in snake_case.

Downstream code is written against the Converse shape, so this module
rewrites Anthropic-shape records in place. Already-Converse records pass
through unchanged.
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


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Dispatch on the record's API family and rewrite in place to Converse shape.

    Each family-normalizer owns the full input+output rewrite for its
    shape, because the caller's API family determines both. Unrecognized
    shapes (Converse, other-vendor streams) are left untouched.
    """
    out = (record.get("output") or {}).get("outputBodyJson")
    if _looks_like_anthropic_stream(out):
        _normalize_anthropic_stream(record)
    elif _looks_like_anthropic_message(out):
        _normalize_anthropic_message(record)
    return record


def _looks_like_anthropic_stream(out: Any) -> bool:
    """Detect an Anthropic streaming Messages response by event-type markers.

    Any list at `outputBodyJson` is a Bedrock streaming SSE decode, but
    the event vocabulary is per-vendor — we only claim ownership of
    Anthropic's. Non-Anthropic streams are left alone.
    """
    if not isinstance(out, list):
        return False
    return any(isinstance(e, dict) and e.get("type") in _ANTHROPIC_STREAM_EVENT_TYPES for e in out)


def _looks_like_anthropic_message(out: Any) -> bool:
    """Detect a non-streaming Anthropic Messages response by shape triad.

    Anthropic's response envelope always carries all three markers:
    `type: "message"`, `role: "assistant"`, and a `content` list.
    Requiring all three avoids false-positives on unrelated shapes that
    happen to reuse one of the fields (e.g. a future Bedrock envelope
    that also uses `type: "message"`).

    Also short-circuit on the presence of `output`, which is Converse's
    top-level wrapper — cheapest possible negative check.
    """
    if not isinstance(out, dict) or "output" in out:
        return False
    return (
        out.get("type") == "message"
        and out.get("role") == "assistant"
        and isinstance(out.get("content"), list)
    )


def _normalize_anthropic_stream(record: dict[str, Any]) -> None:
    """Rewrite an Anthropic streaming record in place to Converse shape.

    Owns both the input tools rewrite (`body.tools[]` → `body.toolConfig`)
    and the streamed-response reconstruction.
    """
    input_body = (record.get("input") or {}).get("inputBodyJson")
    if isinstance(input_body, dict):
        _normalize_anthropic_tools(input_body)
    events = record["output"]["outputBodyJson"]
    record["output"]["outputBodyJson"] = _reconstruct_message_from_stream(events)


def _normalize_anthropic_message(record: dict[str, Any]) -> None:
    """Rewrite an Anthropic non-streaming record in place to Converse shape.

    Owns both the input tools rewrite (`body.tools[]` → `body.toolConfig`)
    and the single-dict response rewrite.
    """
    input_body = (record.get("input") or {}).get("inputBodyJson")
    if isinstance(input_body, dict):
        _normalize_anthropic_tools(input_body)
    body = record["output"]["outputBodyJson"]
    record["output"]["outputBodyJson"] = _reconstruct_message_from_dict(body)


def _normalize_anthropic_tools(body: dict[str, Any]) -> None:
    """Rewrite `body.tools[]` (Anthropic) → `body.toolConfig.tools[].toolSpec` (Converse)."""
    if "toolConfig" in body:
        return
    anthropic_tools = body.get("tools")
    if not isinstance(anthropic_tools, list) or not anthropic_tools:
        return
    body["toolConfig"] = {
        "tools": [
            {
                "toolSpec": {
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "inputSchema": {"json": t.get("input_schema") or {}},
                }
            }
            for t in anthropic_tools
            if isinstance(t, dict)
        ]
    }


def _reconstruct_message_from_dict(body: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a non-streaming Anthropic Messages response → Converse shape.

    Anthropic:
      {type: "message", role: "assistant",
       content: [{type: "text", text}, {type: "tool_use", id, name, input},
                 {type: "thinking", thinking}],
       stop_reason: "..."}

    Converse:
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
            content.append({"text": block.get("thinking", "")})

    result: dict[str, Any] = {"output": {"message": {"role": "assistant", "content": content}}}
    stop_reason = body.get("stop_reason")
    if isinstance(stop_reason, str) and stop_reason:
        result["stopReason"] = stop_reason
    return result


def _reconstruct_message_from_stream(events: list[Any]) -> dict[str, Any]:
    """Walk Anthropic SSE-style events and produce a Converse-shape outputBodyJson."""
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
