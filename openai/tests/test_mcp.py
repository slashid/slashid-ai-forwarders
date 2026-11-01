from __future__ import annotations

import json

from slashid_openai_forwarder import mcp
from slashid_openai_forwarder.hook import TOOL_NAME, HookCall

CALLS: list[HookCall | mcp.UnreadableCall] = []


async def _decide(call: HookCall | mcp.UnreadableCall) -> str:
    CALLS.append(call)
    return json.dumps({"decision": "block", "reason": "r"})


def _req(method: str, params: object = None, id_: object = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method, "id": id_}
    if params is not None:
        msg["params"] = params
    return msg


async def test_initialize_echoes_a_known_version_and_offers_tools() -> None:
    out = await mcp.handle(
        _req("initialize", {"protocolVersion": "2025-06-18"}), _decide, version="1"
    )
    assert out is not None
    assert out["result"]["protocolVersion"] == "2025-06-18"
    assert out["result"]["capabilities"] == {"tools": {"listChanged": False}}
    unknown = await mcp.handle(
        _req("initialize", {"protocolVersion": "1999-01-01"}), _decide, version="1"
    )
    assert unknown is not None
    assert unknown["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


async def test_tools_list_names_the_one_tool() -> None:
    out = await mcp.handle(_req("tools/list"), _decide, version="1")
    assert out is not None
    assert [t["name"] for t in out["result"]["tools"]] == [TOOL_NAME]


async def test_notifications_and_responses_get_no_reply() -> None:
    assert (
        await mcp.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, _decide, version="1"
        )
        is None
    )
    assert await mcp.handle({"jsonrpc": "2.0", "id": 3, "result": {}}, _decide, version="1") is None


async def test_a_call_always_answers_with_a_result_never_is_error() -> None:
    CALLS.clear()
    args = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t", "prompt": "p"}
    out = await mcp.handle(
        _req("tools/call", {"name": TOOL_NAME, "arguments": args}), _decide, version="1"
    )
    assert out is not None
    assert out["result"]["isError"] is False
    assert json.loads(out["result"]["content"][0]["text"])["decision"] == "block"
    assert isinstance(CALLS[-1], HookCall)

    bad = await mcp.handle(
        _req(
            "tools/call",
            {"name": TOOL_NAME, "arguments": {"hook_event_name": "PreToolUse", "session_id": "s"}},
        ),
        _decide,
        version="1",
    )
    assert bad is not None and bad["result"]["isError"] is False
    assert CALLS[-1] == mcp.UnreadableCall("PreToolUse", 1)


async def test_unknown_tools_methods_and_shapes_are_errors() -> None:
    tool = await mcp.handle(_req("tools/call", {"name": "other"}), _decide, version="1")
    assert tool is not None and tool["error"]["code"] == mcp.INVALID_PARAMS
    method = await mcp.handle(_req("resources/list"), _decide, version="1")
    assert method is not None and method["error"]["code"] == mcp.METHOD_NOT_FOUND
    batch = await mcp.handle([_req("ping")], _decide, version="1")
    assert batch is not None and batch["error"]["code"] == mcp.INVALID_REQUEST
