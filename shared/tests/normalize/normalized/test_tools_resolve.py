from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared, resolve_tool


def test_resolve_tool_matches_build_tools_declared() -> None:
    tool, server = resolve_tool("mcp__payroll__read")
    tools, servers = build_tools_declared([("mcp__payroll__read", None, None)])
    assert (tool, server) == (tools[0], servers[0])
    assert (tool.name, server.name, server.kind) == ("read", "payroll", "mcp")


def test_bare_tool_lands_on_builtin() -> None:
    tool, server = resolve_tool("Bash")
    assert (tool.name, server.name, tool.tool_server_id) == ("Bash", "builtin", server.id)
