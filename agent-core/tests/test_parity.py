"""One tool call through both surfaces builds the same tool, server and file references."""

import base64
import json
from dataclasses import dataclass

import pytest
from pydantic import JsonValue
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from helpers import Load, checker_of, ctx, make_runtime, mcp_of, obj, with_result
from slashid_agentcore_forwarder.handler import dispatch

TOOLS = ["gdrive___read_file_content", "gdrive___download_file_content"]


def drive_text() -> str:
    body = {"content": base64.b64encode(b"# Bread\n").decode(), "id": "ID1", "title": "bread.md"}
    return json.dumps(body)


def harness_call(load: Load, tool: str) -> dict[str, JsonValue]:
    payload = load("harness_before_tool_call")
    obj(payload["context"]).update(toolName=tool, toolInput={"fileId": "ID1"})
    return payload


def harness_result(load: Load, tool: str) -> dict[str, JsonValue]:
    payload = load("harness_after_tool_call")
    result: JsonValue = {"status": "success", "content": [{"text": drive_text()}]}
    obj(payload["context"]).update(toolName=tool, toolResult=result)
    return payload


def gateway_call(load: Load, tool: str) -> dict[str, JsonValue]:
    payload = load("gateway_request")
    params = obj(obj(obj(mcp_of(payload)["gatewayRequest"])["body"])["params"])
    params.update(name=tool, arguments={"fileId": "ID1"})
    return payload


def gateway_result(load: Load, tool: str) -> dict[str, JsonValue]:
    payload = with_result(
        load, {"isError": False, "content": [{"type": "text", "text": drive_text()}]}
    )
    params = obj(obj(obj(mcp_of(payload)["gatewayRequest"])["body"])["params"])
    params.update(name=tool, arguments={"fileId": "ID1"})
    return payload


@dataclass(frozen=True)
class Shape:
    """What the two surfaces must agree on; ids and identities differ by design."""

    tool: tuple[str | None, str | None]
    server: tuple[str | None, str | None]
    use: tuple[str, bool | None]
    requested: bool
    files: list[tuple[list[str] | None, str | None, object]]


def shape(event: AIInvocationObservedV1) -> Shape:
    [tool] = event.available_tools or []
    [server] = event.available_tool_servers or []
    [use] = (event.requested_tool_uses or []) + (event.used_tools or [])
    return Shape(
        tool=(tool.id, tool.name),
        server=(server.name, server.kind),
        use=(use.tool_id, use.is_error),
        requested=event.requested_tool_uses is not None,
        files=[(f.file_refs, f.name, f.content_hashes) for f in event.accessed_files or []],
    )


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_call_check_builds_the_same_event_on_both_surfaces(load: Load, tool: str) -> None:
    harness, gateway = make_runtime(), make_runtime()
    await dispatch(harness, harness_call(load, tool), ctx())
    await dispatch(gateway, gateway_call(load, tool), ctx())
    [from_harness] = checker_of(harness).invocations
    [from_gateway] = checker_of(gateway).invocations
    assert shape(from_harness) == shape(from_gateway)
    assert from_harness.requested_tool_uses is not None
    assert shape(from_harness).files == [(["google/ID1"], None, None)]


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_result_check_builds_the_same_event_on_both_surfaces(load: Load, tool: str) -> None:
    harness, gateway = make_runtime(), make_runtime()
    await dispatch(harness, harness_call(load, tool), ctx())  # leaves the input for the result
    await dispatch(harness, harness_result(load, tool), ctx())
    await dispatch(gateway, gateway_result(load, tool), ctx())
    from_harness = checker_of(harness).invocations[-1]
    [from_gateway] = checker_of(gateway).invocations
    assert from_harness.used_tools is not None
    assert shape(from_harness) == shape(from_gateway)
    [(refs, name, _)] = shape(from_harness).files
    assert (refs, name) == (["google/ID1"], "bread.md")
