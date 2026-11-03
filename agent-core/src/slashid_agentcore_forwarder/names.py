"""The name ``resolve_tool`` parses, for the tool and server identity only."""

from __future__ import annotations


def resolve_name(raw: str, tool_type: str | None) -> str:
    """Gateway ``<target>___<tool>`` → ``mcp__<target>__<tool>``; a Harness ``remote_mcp`` name
    (its server is unknown) goes on a placeholder MCP server; builtin and inline names are left
    as they are, and a Gateway message has no ``toolType``."""
    if tool_type == "remote_mcp":
        return f"mcp__MCP Tool Server__{raw}"
    if tool_type in (None, "agentcore_gateway") and "___" in raw:
        target, tool = raw.split("___", 1)
        if target and tool:
            return f"mcp__{target}__{tool}"
    return raw
