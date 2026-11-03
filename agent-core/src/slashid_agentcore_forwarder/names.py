"""The name ``resolve_tool`` parses, for the tool and server identity only."""

from __future__ import annotations


def resolve_name(raw: str, tool_type: str | None) -> str:
    """Gateway ``<target>___<tool>`` → ``mcp__<target>__<tool>``; a Harness ``remote_mcp`` name
    (its server is unknown) goes on a placeholder MCP server; anything else is left as it is."""
    if tool_type == "remote_mcp":
        return f"mcp__MCP Tool Server__{raw}"
    if "___" in raw:
        target, tool = raw.split("___", 1)
        if target and tool:
            return f"mcp__{target}__{tool}"
    return raw
