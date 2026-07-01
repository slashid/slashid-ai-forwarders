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


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return `record` with Anthropic-shape inputs/outputs rewritten to Converse-shape.

    Mutates the record's nested dicts but returns the same outer object for chaining.
    """
    body = (record.get("input") or {}).get("inputBodyJson")
    if isinstance(body, dict):
        _normalize_tools_section(body)

    out = (record.get("output") or {}).get("outputBodyJson")
    if isinstance(out, list):
        record["output"]["outputBodyJson"] = _reconstruct_message_from_stream(out)
    elif isinstance(out, dict) and _looks_like_anthropic_message(out):
        record["output"]["outputBodyJson"] = _reconstruct_message_from_dict(out)

    return record


def _looks_like_anthropic_message(body: dict[str, Any]) -> bool:
    """Detect a non-streaming Anthropic Messages response.

    Distinguishing marks: Anthropic returns `{"type": "message",
    "role": "assistant", "content": [...], "stop_reason": "...", ...}`,
    while Converse returns `{"output": {"message": ...}, "stopReason": ...}`.
    The `output` key is the cheapest disambiguator.
    """
    if "output" in body:
        return False
    if body.get("type") == "message":
        return True
    return body.get("role") == "assistant" and isinstance(body.get("content"), list)


def _normalize_tools_section(body: dict[str, Any]) -> None:
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
