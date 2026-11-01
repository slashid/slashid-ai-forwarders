"""The smallest MCP server ChatGPT can connect to: one tool, no sessions.

Streamable HTTP with JSON responses only. A hook never needs a stream, a
session id or server-initiated messages, so none is offered; a client that
asks for SSE in ``Accept`` gets JSON, which the transport allows when the
server answers with a single response.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .hook import INPUT_SCHEMA, TOOL_NAME, HookCall

log = logging.getLogger(__name__)

# Newest first; an unknown requested version is answered with the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_NAME = "slashid-preflight"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

TOOL = {
    "name": TOOL_NAME,
    "title": "SlashID AI policy check",
    "description": (
        "Asks SlashID whether the organization's AI policy allows a prompt or a tool call. "
        "Meant to be called by a ChatGPT Work managed hook, not by the model."
    ),
    "inputSchema": INPUT_SCHEMA,
}


@dataclass(frozen=True)
class UnreadableCall:
    """Arguments that did not parse: the admin's template does not match the
    tool. ``event`` is what they claimed to be, when they said, so a deny
    still takes the shape that event accepts."""

    event: str | None
    errors: int


# The hook call's verdict text.
Decide = Callable[[HookCall | UnreadableCall], Awaitable[str]]


def _result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


async def handle(message: Any, decide: Decide, *, version: str) -> dict[str, Any] | None:
    """The response to one JSON-RPC message; None for a notification or a
    response, which the transport answers with 202."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        # Batches went away in 2025-06-18; nobody this serves sends one.
        return error(None, INVALID_REQUEST, "expected one JSON-RPC 2.0 message")
    if "method" not in message:
        return None
    id_ = message.get("id")
    if id_ is None:
        return None
    method = message["method"]
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(
            id_,
            {
                "protocolVersion": requested
                if requested in PROTOCOL_VERSIONS
                else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": version},
            },
        )
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": [TOOL]})
    if method == "tools/call":
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return error(id_, INVALID_PARAMS, f"unknown tool; this server has {TOOL_NAME}")
        arguments = params.get("arguments") or {}
        try:
            call: HookCall | UnreadableCall = HookCall.model_validate(arguments)
        except ValidationError as exc:
            event = arguments.get("hook_event_name") if isinstance(arguments, dict) else None
            call = UnreadableCall(event if isinstance(event, str) else None, exc.error_count())
        text = await decide(call)
        # Never isError: Codex treats it as a failed hook, which does not
        # block. A verdict, even the fail mode's, is always a result.
        return _result(id_, {"content": [{"type": "text", "text": text}], "isError": False})
    return error(id_, METHOD_NOT_FOUND, f"method not found: {method}")
