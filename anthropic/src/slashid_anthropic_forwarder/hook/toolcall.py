"""The preflight event for a tool call frame: what the model asked to run,
judged before any of it does."""

from __future__ import annotations

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AITool,
    AIToolServer,
    AIToolUse,
    AnthropicIdentityDetails,
)
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool

from .envelope import PARSED_AS, signed_at_iso
from .frame import ToolCallFrame, ToolUse


def _raw_name(use: ToolUse) -> str:
    """The name ``resolve_tool`` parses. A third-party tool arrives bare, its
    server in the toolset, so it takes the ``mcp__`` form a local MCP tool has."""
    if use.tool_info.type == "third_party" and use.tool_info.toolset_name:
        return f"mcp__{use.tool_info.toolset_name}__{use.tool_name}"
    return use.tool_name


def tool_call_event(frame: ToolCallFrame, *, signed_at: int) -> AIInvocationObservedV1 | None:
    """``None`` when there is nothing to send: the server rejects an identity
    with no identifier, and a frame with no call has nothing to judge."""
    uses = frame.tool_uses()
    if not frame.actor.id or not uses:
        return None
    tools: dict[str, AITool] = {}
    servers: dict[str, AIToolServer] = {}
    requested: list[AIToolUse] = []
    for use in uses:
        tool, server = resolve_tool(_raw_name(use))
        tools.setdefault(tool.id, tool)
        servers.setdefault(server.id, server)
        requested.append(AIToolUse(tool_id=tool.id, tool_use_id=use.id))
    return AIInvocationObservedV1(
        request_id=frame.request_id,
        timestamp=signed_at_iso(signed_at),
        identity_details=AnthropicIdentityDetails(user_id=frame.actor.id),
        model=AIModel(id=frame.model or "unknown", provider="anthropic", raw_model_id=frame.model),
        parsed_as=PARSED_AS,
        user_agent=frame.source.application,
        conversation_id=frame.session_id,
        requested_tool_uses=requested,
        available_tools=list(tools.values()),
        available_tool_servers=list(servers.values()),
    )
