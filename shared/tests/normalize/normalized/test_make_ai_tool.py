"""Tests for make_ai_tool — JSON-Schema-dict → wire-shape AITool adapter."""

from __future__ import annotations

import json

from slashid_ai_forwarder_core.events import AITool
from slashid_ai_forwarder_core.normalize.normalized.tools import make_ai_tool


def test_make_ai_tool_serializes_input_schema() -> None:
    """input_schema (dict on Python side) → JSON string on wire (AITool.input_schema)."""
    tool = make_ai_tool(
        id="tool_1",
        name="read_file",
        description="Read a file from disk",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert isinstance(tool, AITool)
    assert tool.id == "tool_1"
    assert tool.name == "read_file"
    assert tool.description == "Read a file from disk"
    assert tool.input_schema is not None
    assert json.loads(tool.input_schema) == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }


def test_make_ai_tool_none_schemas_stay_none() -> None:
    """Missing schemas remain None on the wire (not "null" string)."""
    tool = make_ai_tool(id="tool_1", name="ping")
    assert tool.input_schema is None
    assert tool.output_schema is None


def test_make_ai_tool_preserves_extra_ai_tool_kwargs() -> None:
    """Extra AITool kwargs (tool_server_id, annotations) forwarded through **kwargs."""
    tool = make_ai_tool(id="tool_1", name="ping", tool_server_id="srv_1")
    assert tool.tool_server_id == "srv_1"


def test_make_ai_tool_input_schema_uses_compact_json() -> None:
    """Wire string uses compact JSON separators — hash-stable across runs."""
    tool = make_ai_tool(
        id="t", name="x", input_schema={"a": 1, "b": [1, 2, 3]}
    )
    # sort_keys + compact separators produce a stable form.
    assert tool.input_schema == '{"a":1,"b":[1,2,3]}'
