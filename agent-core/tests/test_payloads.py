from collections.abc import Callable
from types import SimpleNamespace

import pytest
from pydantic import JsonValue, ValidationError

from slashid_agentcore_forwarder.payloads import (
    ClientContext,
    GatewayPayload,
    HarnessToolHook,
    ResultBody,
    RpcReply,
    ToolsCall,
    classify_body,
    client_context,
    is_truncated,
    parse_tool_result,
)

Load = Callable[[str], dict[str, JsonValue]]
SESSION = "00000000-0000-4000-8000-000000000001"


def test_harness_before_hook(load: Load) -> None:
    hook = HarnessToolHook.model_validate(load("harness_before_tool_call"))
    assert hook.event == "before_tool_call"
    assert hook.context.toolName == "gdrive___read_file_content"
    assert hook.context.toolType == "agentcore_gateway"
    assert hook.context.toolUseId == "tooluse_EXAMPLE0001"
    assert hook.context.toolInput == {"fileId": "ID1"}
    assert hook.sessionId == SESSION
    assert hook.harnessArn.endswith("harness/AgentDemo_default_hdemo-EXAMPLE0001")
    assert hook.hookEventId


def test_harness_after_hook_has_no_input(load: Load) -> None:
    hook = HarnessToolHook.model_validate(load("harness_after_tool_call"))
    assert hook.event == "after_tool_call"
    assert hook.context.toolInput is None
    result = parse_tool_result(hook.context.toolResult)
    assert not result.is_error
    assert not is_truncated(hook.context.toolResult)


def test_truncated_result_is_flagged_and_unreadable(load: Load) -> None:
    hook = HarnessToolHook.model_validate(load("harness_after_truncated"))
    raw = hook.context.toolResult
    assert is_truncated(raw)
    result = parse_tool_result(raw)
    assert result.content is None
    assert not result.is_error


def test_inline_and_remote_mcp_hooks(load: Load) -> None:
    inline = HarnessToolHook.model_validate(load("harness_after_inline"))
    assert (inline.event, inline.context.toolType) == ("after_tool_call", "inline_function")
    remote = HarnessToolHook.model_validate(load("harness_remote_mcp"))
    assert remote.context.toolType == "remote_mcp"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ({"status": "error", "content": []}, True),
        ({"status": "success", "isError": True}, True),
        ({"status": "success", "content": []}, False),
        ({"isError": False}, False),
        ("text", False),
        ([1], False),
        (None, False),
        ({"status": 3}, False),
    ],
)
def test_tool_result_error_flag(raw: JsonValue, want: bool) -> None:
    assert parse_tool_result(raw).is_error is want


def test_is_truncated_needs_the_marker() -> None:
    assert is_truncated({"truncated": True, "preview": "{"})
    assert is_truncated({"truncated": True})
    assert not is_truncated({"truncated": False, "preview": "{"})
    assert not is_truncated({"preview": "{"})
    assert not is_truncated("truncated")
    assert not is_truncated(None)


@pytest.mark.parametrize("missing", ["harnessArn", "sessionId", "hookEventId"])
def test_hook_envelope_needs_its_ids(load: Load, missing: str) -> None:
    payload = load("harness_before_tool_call")
    del payload[missing]
    with pytest.raises(ValidationError):
        HarnessToolHook.model_validate(payload)


@pytest.mark.parametrize("field", ["toolName", "toolUseId"])
@pytest.mark.parametrize("drop", [True, False])
def test_hook_context_needs_a_name_and_call_id(load: Load, field: str, drop: bool) -> None:
    payload = load("harness_before_tool_call")
    context = payload["context"]
    assert isinstance(context, dict)
    if drop:
        del context[field]
    else:
        context[field] = ""
    with pytest.raises(ValidationError) as err:
        HarnessToolHook.model_validate(payload)
    assert "tooluse_EXAMPLE0001" not in str(err.value)


def test_hook_event_must_be_a_tool_event(load: Load) -> None:
    payload = load("harness_before_tool_call")
    payload["event"] = "before_invocation"
    with pytest.raises(ValidationError):
        HarnessToolHook.model_validate(payload)


def test_gateway_request(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_request"))
    assert payload.interceptorInputVersion == "1.0"
    assert payload.mcp is not None
    assert "gatewayResponse" in payload.mcp.model_fields_set
    assert payload.mcp.gatewayResponse is None
    request = payload.mcp.gatewayRequest
    assert request is not None
    call = ToolsCall.model_validate(request.body)
    assert call.id == 2
    assert call.params.name == "gdrive___read_file_content"
    assert call.params.arguments == {"fileId": "ID1"}
    assert call.params.meta is not None
    assert call.params.meta.baggage == f"Self=example,session.id={SESSION}"
    assert request.context is not None
    assert request.context.identity is not None
    assert request.context.identity.awsPrincipalArn == (
        "arn:aws:iam::123456789012:role/AgentCore-example-ApplicationRuntimeAgentRole"
    )


def test_gateway_jwt_request_has_no_context(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_request_jwt"))
    assert payload.mcp is not None
    assert payload.mcp.gatewayRequest is not None
    assert payload.mcp.gatewayRequest.context is None


def test_gateway_response(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response"))
    assert payload.mcp is not None
    response = payload.mcp.gatewayResponse
    assert response is not None
    assert (response.statusCode, response.isStreamingResponse) == (200, False)
    reply = RpcReply.model_validate(response.body)
    assert reply.error is None
    assert ResultBody.model_validate(reply.result).isError is False


def test_gateway_policy_denial_is_an_error_reply(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response_policy_denied"))
    assert payload.mcp is not None
    assert payload.mcp.gatewayResponse is not None
    reply = RpcReply.model_validate(payload.mcp.gatewayResponse.body)
    assert reply.result is None
    assert isinstance(reply.error, dict)
    assert reply.error["code"] == -32002


def test_gateway_response_without_request(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response_no_request"))
    assert payload.mcp is not None
    assert payload.mcp.gatewayRequest is None
    assert payload.mcp.gatewayResponse is not None


def test_gateway_version_is_required(load: Load) -> None:
    payload = load("gateway_request")
    del payload["interceptorInputVersion"]
    with pytest.raises(ValidationError):
        GatewayPayload.model_validate(payload)


def test_http_shape_has_no_mcp() -> None:
    payload = GatewayPayload.model_validate(
        {"interceptorInputVersion": "1.0", "http": {"gatewayRequest": None}}
    )
    assert payload.mcp is None


def test_client_context() -> None:
    custom = {"GATEWAY_ARN": "arn:g", "REQUEST_ID": "r-1", "SOURCE_IP": "203.0.113.1"}
    context = SimpleNamespace(client_context=SimpleNamespace(custom=custom))
    assert client_context(context) == ClientContext(GATEWAY_ARN="arn:g", REQUEST_ID="r-1")
    assert client_context(SimpleNamespace(client_context=None)) == ClientContext()
    assert client_context(SimpleNamespace(client_context=SimpleNamespace(custom=None))) == (
        ClientContext()
    )
    assert client_context(object()) == ClientContext()
    bad = SimpleNamespace(client_context=SimpleNamespace(custom={"REQUEST_ID": 3}))
    assert client_context(bad) == ClientContext()


def _call(**params: JsonValue) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": params}


def test_classify_tools_call(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_request"))
    assert payload.mcp is not None
    assert payload.mcp.gatewayRequest is not None
    got = classify_body(payload.mcp.gatewayRequest.body)
    assert got.kind == "tools_call"
    assert got.call is not None
    assert got.call.params.name == "gdrive___read_file_content"


def test_classify_tools_list_is_other(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_tools_list_request"))
    assert payload.mcp is not None
    assert payload.mcp.gatewayRequest is not None
    got = classify_body(payload.mcp.gatewayRequest.body)
    assert (got.kind, got.call) == ("other", None)


@pytest.mark.parametrize(
    "body",
    [
        _call(name=""),
        _call(arguments={}),
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call"},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": "x"},
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "tools/call", "params": {"name": "t"}},
        {"jsonrpc": "2.0", "id": 5, "method": ["tools/call"]},
        "tools/call",
        None,
        7,
    ],
)
def test_classify_unreadable(body: JsonValue) -> None:
    got = classify_body(body)
    assert (got.kind, got.call) == ("unreadable", None)


@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {},
    ],
)
def test_classify_other(body: JsonValue) -> None:
    assert classify_body(body).kind == "other"


def test_classify_batches() -> None:
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert classify_body([ping, _call(name="t")]).kind == "batch_with_call"
    assert classify_body([ping, ping]).kind == "batch_other"
    assert classify_body([]).kind == "batch_other"
    assert classify_body([ping, "x", None]).kind == "batch_other"
    assert classify_body([_call(name="")]).kind == "batch_with_call"
