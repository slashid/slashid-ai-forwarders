from __future__ import annotations

import json

import pytest
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool

from slashid_codex.envelope import failed_calls
from slashid_codex.mcp_calls import failed, pairs, tool_name
from slashid_codex.rollout import McpToolCall


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


def test_other_servers_get_no_pair() -> None:
    assert pairs([_item(server="payroll")]) == ([], [])


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
