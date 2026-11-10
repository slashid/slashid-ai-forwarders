"""Portkey webhook bodies, as the gateway's webhook plugin posts them."""

from __future__ import annotations

import base64
import json
from typing import Any

FILE_BYTES = b"quarterly numbers"

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "mcp__github__create_issue",
            "description": "Open an issue",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}}},
        },
    }
]

REQUEST: dict[str, Any] = {
    "model": "@openai-prod/gpt-5",
    "messages": [
        {"role": "system", "content": "You are Stitchy."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "summarize project falcon"},
                {
                    "type": "file",
                    "file": {
                        "filename": "q3.txt",
                        "file_data": "data:text/plain;base64,"
                        + base64.b64encode(FILE_BYTES).decode(),
                    },
                },
            ],
        },
    ],
    "tools": TOOLS,
}


def tool_call(call_id: str, title: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "mcp__github__create_issue",
            "arguments": json.dumps({"title": title}),
        },
    }


def completion(*choices: dict[str, Any], response_id: str = "chatcmpl-1") -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "chat.completion",
        "model": "gpt-5-2026-08-07",
        "choices": [
            {
                "index": i,
                "finish_reason": "tool_calls" if m.get("tool_calls") else "stop",
                "message": m,
            }
            for i, m in enumerate(choices)
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }


TEXT_ANSWER: dict[str, Any] = {"role": "assistant", "content": "Falcon is on track."}


def before(
    request: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    request_type: str = "chatComplete",
) -> dict[str, Any]:
    return {
        "request": {
            "json": REQUEST if request is None else request,
            "text": "summarize project falcon",
            "isStreamingRequest": False,
            "isTransformed": False,
        },
        "response": {"json": {}, "text": "", "statusCode": None, "isTransformed": False},
        "provider": "openai",
        "requestType": request_type,
        "metadata": {"_user": "Ada@Example.com"} if metadata is None else metadata,
        "eventType": "beforeRequestHook",
    }


def after(
    response: dict[str, Any] | None,
    request: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = before(request, metadata=metadata)
    body["eventType"] = "afterRequestHook"
    body["response"] = {
        "json": response,
        "text": "",
        "statusCode": 200,
        "isTransformed": False,
    }
    return body
