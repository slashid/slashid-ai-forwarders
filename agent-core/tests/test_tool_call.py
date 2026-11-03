import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError
from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIToolUse, AWSIdentityDetails
from slashid_ai_forwarder_core.normalize.normalized.connector_files import connector_refs

from slashid_agentcore_forwarder.names import resolve_name
from slashid_agentcore_forwarder.tool_call import (
    CachedInput,
    ToolCall,
    build_event,
    call_files,
    result_files,
)

CONFIG = BaseConfig(endpoint="http://test", push_token="test")
NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
IDENTITY = AWSIdentityDetails(
    agent_arn="arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/h"
)


def make_call(
    raw_name: str,
    tool_input: JsonValue = None,
    *,
    surface: Literal["harness", "gateway"] = "gateway",
    tool_type: str | None = None,
    call_id: str = "call-1",
) -> ToolCall:
    return ToolCall(
        surface=surface,
        raw_name=raw_name,
        resolved_name=resolve_name(raw_name, tool_type),
        call_id=call_id,
        tool_input=tool_input,
        request_id="s:e1",
        conversation_id="session-1",
        user_agent="example-agent/1.0",
        identity=IDENTITY,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_call_check_event_for_a_drive_read() -> None:
    call = make_call("gdrive___read_file_content", {"fileId": "ID1"})
    event = build_event(call, result_is_error=None, files=call_files(call), now=NOW)
    assert event.parsed_as == "agentcore-gateway-hook"
    assert event.model.id == "unknown"
    assert event.request_id == "s:e1"
    assert event.timestamp == "2026-11-03T12:00:00+00:00"
    assert event.identity_details == IDENTITY
    assert event.user_agent == "example-agent/1.0"
    assert event.conversation_id == "session-1"
    [tool] = event.available_tools or []
    [server] = event.available_tool_servers or []
    assert (server.name, server.kind) == ("gdrive", "mcp")
    assert tool.tool_server_id == server.id
    assert event.requested_tool_uses == [AIToolUse(tool_id=tool.id, tool_use_id="call-1")]
    assert event.used_tools is None
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]
    assert file.content_hashes is None


def test_a_bare_tool_is_on_the_builtin_server_and_has_no_files() -> None:
    call = make_call("shell", {"command": "ls"}, surface="harness", tool_type="builtin")
    event = build_event(call, result_is_error=None, files=call_files(call))
    assert event.parsed_as == "agentcore-harness-hook"
    [server] = event.available_tool_servers or []
    assert (server.name, server.kind) == ("builtin", "runtime")
    assert event.accessed_files is None


def test_a_remote_mcp_tool_sits_on_the_placeholder_server() -> None:
    call = make_call("ask_question", {"q": "?"}, surface="harness", tool_type="remote_mcp")
    event = build_event(call, result_is_error=None, files=[])
    [server] = event.available_tool_servers or []
    assert (server.name, server.kind) == ("MCP Tool Server", "mcp")


def test_the_timestamp_defaults_to_now_in_utc() -> None:
    event = build_event(make_call("shell"), result_is_error=None, files=[])
    stamp = datetime.fromisoformat(event.timestamp)
    assert stamp.utcoffset() == UTC.utcoffset(None)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 5


def test_result_check_event() -> None:
    call = make_call("gdrive___read_file_content", {"fileId": "ID1"})
    event = build_event(call, result_is_error=False, files=[], now=NOW)
    [tool] = event.available_tools or []
    assert event.used_tools == [AIToolUse(tool_id=tool.id, tool_use_id="call-1", is_error=False)]
    assert event.requested_tool_uses is None
    assert event.accessed_files is None
    failed = build_event(call, result_is_error=True, files=[], now=NOW)
    assert failed.used_tools == [AIToolUse(tool_id=tool.id, tool_use_id="call-1", is_error=True)]


def test_a_download_result_gives_refs_and_hashes() -> None:
    call = make_call("gdrive___download_file_content", {"fileId": "ID1"})
    text = json.dumps({"content": _b64(b"# Bread\n"), "id": "ID1", "title": "banana-bread.md"})
    files = result_files(call, call.tool_input, [{"text": text}], config=CONFIG)
    event = build_event(call, result_is_error=False, files=files, now=NOW)
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]
    assert file.name == "banana-bread.md"
    assert file.content_hashes is not None
    assert file.content_hashes["sha256"] == hashlib.sha256(b"# Bread\n").hexdigest()


def test_a_search_result_object_gives_a_ref_per_hit() -> None:
    call = make_call("gdrive___search_files", {"query": "q"})
    hits = [
        {"id": "A", "title": "a.md", "mimeType": "text/markdown"},
        {"id": "B", "title": "b.pdf", "mimeType": "application/pdf"},
    ]
    output: JsonValue = {"content": [{"type": "text", "text": json.dumps({"files": hits})}]}
    files = result_files(call, call.tool_input, output, config=CONFIG)
    assert [f.file_refs for f in files] == [["google/A"], ["google/B"]]
    assert [f.name for f in files] == ["a.md", "b.pdf"]


def test_a_truncated_result_gives_refs_from_the_input_alone() -> None:
    call = make_call("gdrive___read_file_content", None, surface="harness")
    cached = CachedInput(tool_name=call.raw_name, tool_input={"fileId": "ID1"})
    files = result_files(call, cached.tool_input, None, config=CONFIG)
    event = build_event(call, result_is_error=False, files=files, now=NOW)
    [file] = event.accessed_files or []
    assert (file.file_refs, file.content_hashes) == (["google/ID1"], None)
    assert event.used_tools is not None
    assert event.used_tools[0].is_error is False


def test_call_files_merge_the_handlers_that_share_a_name() -> None:
    call = make_call("gdrive___get_file_metadata", {"fileId": "ID1"})
    assert len(connector_refs(call.raw_name, call.tool_input)) > 1
    [file] = call_files(call)
    assert file.file_refs == ["google/ID1"]


def test_result_files_merge_too() -> None:
    call = make_call("gdrive___get_file_metadata", {"fileId": "ID1"})
    output = [{"text": json.dumps({"id": "ID1", "title": "a.md", "mimeType": "text/markdown"})}]
    [file] = result_files(call, call.tool_input, output, config=CONFIG)
    assert (file.file_refs, file.name) == (["google/ID1"], "a.md")


def test_no_input_no_output_no_files() -> None:
    call = make_call("gdrive___read_file_content", None)
    assert call_files(call) == []
    assert result_files(call, None, None, config=CONFIG) == []


def test_cached_input_matches_on_the_raw_name() -> None:
    cached = CachedInput(tool_name="gdrive___read_file_content", tool_input={"fileId": "ID1"})
    assert cached.matches(make_call("gdrive___read_file_content"))
    assert not cached.matches(make_call("gdrive___search_files"))
    assert not cached.matches(make_call("mcp__gdrive__read_file_content"))


def test_a_call_is_immutable() -> None:
    with pytest.raises(ValidationError):
        make_call("shell").call_id = "x"
