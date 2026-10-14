from __future__ import annotations

import json

import pytest
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool

from slashid_codex.envelope import failed_calls
from slashid_codex.item_calls import failed, pairs, tool_name
from slashid_codex.rollout import CommandExecution, FileChange, ImageView, McpToolCall, WebSearch


def _item(**fields: object) -> McpToolCall:
    base: dict[str, object] = {
        "type": "McpToolCall",
        "id": "exec-1",
        "server": "codex_apps",
        "tool": "google_drive.fetch",
        "arguments": {"url": "u"},
        "status": "completed",
        "result": {"content": [], "structuredContent": {"id": "abc"}, "isError": False},
    }
    return McpToolCall.model_validate(base | fields)


def test_name_is_the_hooks() -> None:
    name = tool_name(_item())
    assert name == "mcp__codex_apps__google_drive__fetch"
    tool, server = resolve_tool(name)
    assert (server.name, tool.name) == ("codex_apps", "google_drive__fetch")


def test_pair_carries_arguments_and_the_result_by_alias() -> None:
    calls, outputs = pairs([_item()])
    [call], [output] = calls, outputs
    assert (call.call_id, call.name) == ("exec-1", "mcp__codex_apps__google_drive__fetch")
    assert json.loads(call.arguments) == {"url": "u"}
    assert output.call_id == "exec-1"
    [part] = output.output
    assert json.loads(part.text) == {  # ty: ignore[unresolved-attribute]
        "content": [],
        "structuredContent": {"id": "abc"},
        "isError": False,
    }


def test_a_failed_call_without_a_result_says_so() -> None:
    _, [output] = pairs([_item(status="failed", result=None)])
    assert json.loads(output.output[0].text) == {"isError": True}  # ty: ignore[unresolved-attribute]


def test_other_servers_get_a_pair_too() -> None:
    [call], _ = pairs([_item(server="paulo", tool="web_fetch")])
    assert call.name == "mcp__paulo__web_fetch"


def test_pairs_are_synthetic() -> None:
    [call], _ = pairs([_item()])
    assert call.synthetic is True


def test_shell_command_pair_is_the_hooks_bash() -> None:
    item = CommandExecution.model_validate(
        {
            "type": "CommandExecution",
            "id": "exec-2",
            "command": ["/bin/bash", "-lc", "cat my note.txt"],
            "cwd": "file:///home/user/my%20work",
            "formatted_output": "line one\n",
            "exit_code": 0,
        }
    )
    [call], [output] = pairs([item])
    assert (call.call_id, call.name) == ("exec-2", "Bash")
    assert json.loads(call.arguments) == {
        "command": "cat my note.txt",
        "workdir": "/home/user/my work",
    }
    assert output.call_id == "exec-2"
    assert output.output[0].text == "line one\n"  # ty: ignore[unresolved-attribute]


def test_shell_command_with_another_argv_is_joined() -> None:
    item = CommandExecution.model_validate(
        {"type": "CommandExecution", "id": "exec-3", "command": ["ls", "-la", "my dir"]}
    )
    [call], _ = pairs([item])
    assert json.loads(call.arguments) == {"command": "ls -la 'my dir'"}


def test_image_view_pair_is_view_image() -> None:
    item = ImageView.model_validate(
        {"type": "ImageView", "id": "exec-4", "path": "file:///home/user/image%201.png"}
    )
    [call], [output] = pairs([item])
    assert (call.call_id, call.name) == ("exec-4", "view_image")
    assert json.loads(call.arguments) == {"path": "/home/user/image 1.png"}
    assert output.output == []


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, False),
        ({"status": "failed"}, True),
        ({"result": {"isError": True}}, True),
        ({"result": None}, False),
    ],
)
def test_failed(fields: dict[str, object], expected: bool) -> None:
    assert failed(_item(**fields)) is expected


def test_failed_calls_include_failed_mcp_items() -> None:
    assert failed_calls([_item(status="failed"), _item(id="exec-2")]) == {"exec-1"}


def test_failed_calls_include_failed_commands() -> None:
    command = CommandExecution.model_validate(
        {"type": "CommandExecution", "id": "exec-5", "exit_code": 1}
    )
    assert failed_calls([command]) == {"exec-5"}


def test_file_change_pair_is_an_apply_patch_with_a_rebuilt_patch() -> None:
    item = FileChange.model_validate(
        {
            "type": "FileChange",
            "id": "exec-6",
            "changes": {
                "/w/new.txt": {"type": "add", "content": "hi\nthere\n"},
                "/w/hello.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1,2 +1,2 @@\n hi\n-line2\n+LINE-TWO\n",
                    "move_path": None,
                },
                "/w/old.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-m\n+n\n",
                    "move_path": "/w/renamed.txt",
                },
                "/w/gone.txt": {"type": "delete", "content": "secret\n"},
            },
            "status": "completed",
            "stdout": "Success. Updated the following files:\nA new.txt\n",
        }
    )
    [call], [output] = pairs([item])
    assert (call.call_id, call.name) == ("exec-6", "apply_patch")
    assert json.loads(call.arguments)["command"] == "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: /w/new.txt",
            "+hi",
            "+there",
            "*** Update File: /w/hello.txt",
            "@@",
            " hi",
            "-line2",
            "+LINE-TWO",
            "*** Update File: /w/old.txt",
            "*** Move to: /w/renamed.txt",
            "@@",
            "-m",
            "+n",
            "*** Delete File: /w/gone.txt",
            "*** End Patch",
        ]
    )
    assert "secret" not in call.arguments
    assert output.output[0].text.startswith("Success.")  # ty: ignore[unresolved-attribute]


def test_failed_calls_include_a_failed_patch() -> None:
    item = FileChange.model_validate(
        {"type": "FileChange", "id": "exec-7", "changes": {}, "status": "failed"}
    )
    assert failed_calls([item]) == {"exec-7"}


def test_resource_tools_are_named_without_the_server() -> None:
    for tool in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
        [call], _ = pairs([_item(server="paulo", tool=tool, arguments={"server": "paulo"})])
        assert call.name == tool


def test_web_run_is_webrun() -> None:
    item = WebSearch.model_validate(
        {
            "type": "Extension",
            "kind": "web.search",
            "id": "exec-9",
            "query": "https://example.com",
            "action": {"type": "openPage", "url": "https://example.com"},
            "results": [{"title": "Example Domain"}],
        }
    )
    [call], [output] = pairs([item])
    assert (call.call_id, call.name) == ("exec-9", "webrun")
    assert json.loads(call.arguments)["action"]["url"] == "https://example.com"
    assert "Example Domain" in output.output[0].text  # ty: ignore[unresolved-attribute]
