import pytest

from slashid_agentcore_forwarder.names import resolve_name


@pytest.mark.parametrize(
    ("raw", "tool_type", "want"),
    [
        ("gdrive___read_file_content", None, "mcp__gdrive__read_file_content"),
        ("gdrive___read_file_content", "agentcore_gateway", "mcp__gdrive__read_file_content"),
        ("ask_question", "remote_mcp", "mcp__MCP Tool Server__ask_question"),
        ("shell", "builtin", "shell"),
        ("a__b", "inline_function", "a__b"),
        ("plain", None, "plain"),
        ("___tool", None, "___tool"),
        ("target___", None, "target___"),
    ],
)
def test_resolve_name(raw: str, tool_type: str | None, want: str) -> None:
    assert resolve_name(raw, tool_type) == want
