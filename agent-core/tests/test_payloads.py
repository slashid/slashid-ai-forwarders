from collections.abc import Callable
from types import SimpleNamespace

import pytest
from pydantic import JsonValue, ValidationError

from slashid_agentcore_forwarder.payloads import (
    ClientContext,
    GatewayContext,
    GatewayPayload,
    GatewayRequest,
    GatewayResponse,
    HarnessEventName,
    HarnessToolHook,
    RpcReply,
    ToolsCall,
    classify_body,
    client_context,
    is_truncated,
    parse_harness_result,
    parse_tool_result,
)

Load = Callable[[str], dict[str, JsonValue]]
SESSION = "00000000-0000-4000-8000-000000000001"


def test_harness_before_hook(load: Load) -> None:
    hook = HarnessToolHook.model_validate(load("harness_before_tool_call"))
    assert hook.context.toolName == "gdrive___read_file_content"
    assert hook.context.toolType == "agentcore_gateway"
    assert hook.context.toolUseId == "tooluse_EXAMPLE0001"
    assert hook.context.toolInput == {"fileId": "ID1"}
    assert hook.sessionId == SESSION
    assert hook.harnessArn.endswith("harness/AgentDemo_default_hdemo-EXAMPLE0001")
    assert hook.hookEventId


def test_harness_after_hook_has_no_input(load: Load) -> None:
    hook = HarnessToolHook.model_validate(load("harness_after_tool_call"))
    assert hook.context.toolInput is None
    result = parse_harness_result(hook.context.toolResult)
    assert not result.is_error
    assert not is_truncated(hook.context.toolResult)


def test_truncated_result_is_flagged_and_unreadable(load: Load) -> None:
    hook = HarnessToolHook.model_validate(load("harness_after_truncated"))
    raw = hook.context.toolResult
    assert is_truncated(raw)
    result = parse_harness_result(raw)
    assert result.content is None
    assert not result.is_error


def test_inline_and_remote_mcp_hooks(load: Load) -> None:
    inline = HarnessToolHook.model_validate(load("harness_after_inline"))
    assert inline.context.toolType == "inline_function"
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
def test_harness_result_error_flag(raw: JsonValue, want: bool) -> None:
    assert parse_harness_result(raw).is_error is want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ({"isError": True, "content": []}, True),
        ({"isError": False}, False),
        ({"status": "error"}, False),  # MCP has no status
        ({"content": []}, False),
        ("text", False),
        (None, False),
        ({"isError": "maybe"}, False),
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
    with pytest.raises(ValidationError):
        HarnessToolHook.model_validate(payload)


# Pydantic shows up to ~50 chars of the failing value, so these payloads are short.
def test_a_hook_error_does_not_echo_the_tool_input() -> None:
    payload: dict[str, JsonValue] = {
        "harnessArn": "arn:h",
        "sessionId": SESSION,
        "hookEventId": "e1",
        "context": {"toolInput": "SECRETVAL", "toolUseId": "t1"},
    }
    with pytest.raises(ValidationError) as err:
        HarnessToolHook.model_validate(payload)
    assert "toolName" in str(err.value)
    assert "SECRETVAL" not in str(err.value)


def test_a_tools_call_error_does_not_echo_the_arguments() -> None:
    body = _call(arguments="SECRETVAL")
    with pytest.raises(ValidationError) as err:
        ToolsCall.model_validate(body)
    assert "SECRETVAL" not in str(err.value)


def test_a_gateway_error_does_not_echo_the_headers() -> None:
    with pytest.raises(ValidationError) as err:
        GatewayResponse.model_validate({"headers": "SECRETVAL"})
    assert "body" in str(err.value)
    assert "SECRETVAL" not in str(err.value)


@pytest.mark.parametrize("event", ["before_tool_call", 7, None, ["x"], {}])
def test_the_event_is_read_whatever_its_type(load: Load, event: JsonValue) -> None:
    payload = load("harness_before_tool_call")
    payload["event"] = event
    assert HarnessEventName.model_validate(payload).event == event
    assert HarnessEventName.model_validate({}).event is None


def test_gateway_request(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_request"))
    assert payload.interceptorInputVersion == "1.0"
    assert payload.mcp is not None
    assert "gatewayResponse" in payload.mcp.model_fields_set
    assert payload.mcp.gatewayResponse is None
    request = GatewayRequest.model_validate(payload.mcp.gatewayRequest)
    call = ToolsCall.model_validate(request.body)
    assert call.id == 2
    assert call.params.name == "gdrive___read_file_content"
    assert call.params.arguments == {"fileId": "ID1"}
    assert call.params.baggage == f"Self=example,session.id={SESSION}"
    context = GatewayContext.model_validate(request.context)
    assert context.identity is not None
    assert context.identity.awsPrincipalArn == (
        "arn:aws:iam::123456789012:role/AgentCore-example-ApplicationRuntimeAgentRole"
    )


def test_gateway_jwt_request_has_no_context(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_request_jwt"))
    assert payload.mcp is not None
    assert GatewayRequest.model_validate(payload.mcp.gatewayRequest).context is None


def test_gateway_response(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response"))
    assert payload.mcp is not None
    response = GatewayResponse.model_validate(payload.mcp.gatewayResponse)
    assert response.statusCode == 200
    assert "statusCode" in response.model_fields_set
    assert response.isStreamingResponse is None
    reply = RpcReply.model_validate(response.body)
    assert reply.error is None
    assert parse_tool_result(reply.result).is_error is False


def test_gateway_policy_denial_is_an_error_reply(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response_policy_denied"))
    assert payload.mcp is not None
    reply = RpcReply.model_validate(
        GatewayResponse.model_validate(payload.mcp.gatewayResponse).body
    )
    assert reply.result is None
    assert isinstance(reply.error, dict)
    assert reply.error["code"] == -32002


def test_gateway_response_without_request(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response_no_request"))
    assert payload.mcp is not None
    assert payload.mcp.gatewayRequest is None
    assert payload.mcp.gatewayResponse is not None
    assert "gatewayRequest" in payload.mcp.model_fields_set


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
    got = classify_body(GatewayRequest.model_validate(payload.mcp.gatewayRequest).body)
    assert got.kind == "tools_call"
    assert got.call is not None
    assert got.call.params.name == "gdrive___read_file_content"


def test_classify_tools_list_is_other(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_tools_list_request"))
    assert payload.mcp is not None
    got = classify_body(GatewayRequest.model_validate(payload.mcp.gatewayRequest).body)
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
    ping: JsonValue = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    with_call: list[JsonValue] = [ping, _call(name="t")]
    other: list[JsonValue] = [ping, ping]
    junk: list[JsonValue] = [ping, "x", None]
    unreadable_call: list[JsonValue] = [_call(name="")]
    assert classify_body(with_call).kind == "batch_with_call"
    assert classify_body(other).kind == "batch_other"
    assert classify_body([]).kind == "batch_other"
    assert classify_body(junk).kind == "batch_other"
    assert classify_body(unreadable_call).kind == "batch_with_call"


@pytest.mark.parametrize("meta", [{"baggage": 5}, "x", 7, [1], None, {}])
def test_a_malformed_meta_leaves_the_call_readable(meta: JsonValue) -> None:
    got = classify_body(_call(name="t", _meta=meta))
    assert got.kind == "tools_call"
    assert got.call is not None
    assert (got.call.params.name, got.call.id) == ("t", 5)
    assert got.call.params.baggage is None


def test_a_call_without_an_id_is_a_notification() -> None:
    got = classify_body({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "t"}})
    assert got.kind == "tools_call"
    assert got.call is not None
    assert got.call.id is None


@pytest.mark.parametrize("name", ["", " ", " \t\n"])
def test_a_blank_tool_name_is_unreadable(name: str) -> None:
    assert classify_body(_call(name=name)).kind == "unreadable"


def test_a_blank_harness_tool_name_is_rejected(load: Load) -> None:
    payload = load("harness_before_tool_call")
    context = payload["context"]
    assert isinstance(context, dict)
    context["toolName"] = "  "
    with pytest.raises(ValidationError):
        HarnessToolHook.model_validate(payload)


def test_gateway_streaming_response(load: Load) -> None:
    payload = GatewayPayload.model_validate(load("gateway_response_streaming"))
    assert payload.mcp is not None
    response = GatewayResponse.model_validate(payload.mcp.gatewayResponse)
    assert response.isStreamingResponse is True
    assert RpcReply.model_validate(response.body).error is None


@pytest.mark.parametrize("flag", ["maybe", "true", 1, 0, [], {}])
def test_a_streaming_flag_must_be_a_boolean(flag: JsonValue) -> None:
    with pytest.raises(ValidationError):
        GatewayResponse.model_validate({"body": None, "isStreamingResponse": flag})


def test_a_response_needs_a_body_but_not_a_status_code() -> None:
    response = GatewayResponse.model_validate({"body": None})
    assert "statusCode" not in response.model_fields_set
    for bad in ({}, {"statusCode": 200}):
        with pytest.raises(ValidationError):
            GatewayResponse.model_validate(bad)


def test_strings_are_kept_as_they_came() -> None:
    body = _call(name="t", arguments={"a": " x\n"}, _meta={"baggage": " k=v "})
    body["id"] = " 7 "
    got = classify_body(body)
    assert got.call is not None
    assert got.call.id == " 7 "
    assert got.call.params.arguments == {"a": " x\n"}
    assert got.call.params.baggage == " k=v "


def test_pass_through_values_are_the_payloads_own_objects() -> None:
    arguments: JsonValue = {"a": ["b"]}
    got = classify_body(_call(name="t", arguments=arguments))
    assert got.call is not None
    assert got.call.params.arguments is arguments


def test_a_nesting_far_past_the_validation_depth_is_held() -> None:
    deep: JsonValue = "x"
    for _ in range(5_000):
        deep = {"a": deep}
    got = classify_body(_call(name="t", arguments=deep))
    assert got.kind == "tools_call"
    assert got.call is not None
    assert got.call.params.arguments is deep
    assert is_truncated(deep) is False
    assert parse_tool_result({"content": deep, "structuredContent": deep}).content is deep
