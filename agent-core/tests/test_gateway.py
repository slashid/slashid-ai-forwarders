import base64
import json
import logging
import uuid
from collections.abc import Callable

import pytest
from pydantic import JsonValue

from helpers import (
    GATEWAY_ROLE,
    LOGGER,
    PRINCIPAL,
    FakeRoles,
    checker_of,
    events,
    identity_of,
    make_runtime,
)
from slashid_agentcore_forwarder.checks import POLICY_UNAVAILABLE, UNREADABLE, Outcome
from slashid_agentcore_forwarder.deny import derive_key, is_own_deny, with_reference
from slashid_agentcore_forwarder.gateway import handle
from slashid_agentcore_forwarder.payloads import ClientContext

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-2:123456789012:gateway/gw-EXAMPLE"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/harness1-AbCdEfGhIj"
SESSION = "00000000-0000-4000-8000-000000000001"
RID = "11111111-1111-4111-8111-111111111111"
READ = "gdrive___read_file_content"
WITHHELD = "Content withheld: "
VERSION = "1.0"
SECRET = "SYNTHETIC-SIGNATURE"
Load = Callable[[str], dict[str, JsonValue]]


def ctx(**overrides: str | None) -> ClientContext:
    return ClientContext.model_validate(
        {"GATEWAY_ARN": GATEWAY_ARN, "REQUEST_ID": RID, **overrides}
    )


def obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def mcp_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(payload["mcp"])


def request_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(mcp_of(payload)["gatewayRequest"])


def body_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(request_of(payload)["body"])


def params_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(body_of(payload)["params"])


def response_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(mcp_of(payload)["gatewayResponse"])


def with_result(
    load: Load, result: JsonValue, name: str = "gateway_response"
) -> dict[str, JsonValue]:
    payload = load(name)
    obj(response_of(payload)["body"])["result"] = result
    return payload


def drive_result(file_id: str = "ID1") -> JsonValue:
    text = json.dumps(
        {
            "content": base64.b64encode(b"# Bread\n").decode(),
            "id": file_id,
            "title": "banana-bread.md",
        }
    )
    return {"isError": False, "content": [{"type": "text", "text": text}]}


def allow_request(body: JsonValue) -> JsonValue:
    return {
        "interceptorOutputVersion": VERSION,
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def deny_body(rpc_id: JsonValue, text: str) -> JsonValue:
    return {
        "interceptorOutputVersion": VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": text}]},
                },
            }
        },
    }


def denied_text(answer: JsonValue) -> str:
    """The text of a deny answer, whose shape is asserted along the way."""
    response = obj(obj(obj(answer)["mcp"])["transformedGatewayResponse"])
    assert response["statusCode"] == 200
    body = obj(response["body"])
    assert body["jsonrpc"] == "2.0"
    result = obj(body["result"])
    assert result["isError"] is True
    [block] = result["content"] if isinstance(result["content"], list) else []
    text = obj(block)["text"]
    assert isinstance(text, str)
    return text


def echoed_response(payload: dict[str, JsonValue]) -> JsonValue:
    response = response_of(payload)
    out: dict[str, JsonValue] = {"statusCode": response["statusCode"]}
    if "headers" in response:
        out["headers"] = response["headers"]
    out["body"] = response["body"]
    return {"interceptorOutputVersion": VERSION, "mcp": {"transformedGatewayResponse": out}}


# REQUEST


async def test_request_sends_one_call_check_and_echoes_the_body(load: Load) -> None:
    runtime = make_runtime()
    payload = load("gateway_request")
    answer = await handle(runtime, payload, ctx())
    assert answer == allow_request(body_of(payload))
    [event] = checker_of(runtime).invocations
    assert event.request_id == f"{SESSION}:{RID}:request"
    assert event.conversation_id == SESSION
    assert event.parsed_as == "agentcore-gateway-hook"
    assert event.user_agent == "example-agent/1.0"
    identity = identity_of(event)
    assert identity.gateway_arn == GATEWAY_ARN
    assert identity.principal_arn == PRINCIPAL
    assert identity.agent_arn is None
    assert event.used_tools is None
    [use] = event.requested_tool_uses or []
    assert (use.tool_use_id, use.is_error) == (RID, None)
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]
    [server] = event.available_tool_servers or []
    assert (server.name, server.kind) == ("gdrive", "mcp")


async def test_request_without_a_session_id_has_a_bare_request_id(load: Load) -> None:
    payload = load("gateway_request")
    request_of(payload)["headers"] = {"user-agent": "example-agent/1.0"}
    del params_of(payload)["_meta"]
    runtime = make_runtime()
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert event.request_id == f"{RID}:request"
    assert event.conversation_id is None


async def test_baggage_is_read_field_by_field_header_first(load: Load) -> None:
    payload = load("gateway_request")
    request_of(payload)["headers"] = {"Baggage": "session.id=FROM-HEADER"}
    params_of(payload)["_meta"] = {"baggage": "harness.id=harness1-AbCdEfGhIj,session.id=FROM-META"}
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert event.conversation_id == "FROM-HEADER"
    assert identity_of(event).agent_arn == HARNESS_ARN  # harness.id from _meta, verified
    assert ("claimed", HARNESS_ARN) in roles.looked_up


async def test_baggage_falls_back_to_meta(load: Load) -> None:
    payload = load("gateway_request")
    del request_of(payload)["headers"]
    runtime = make_runtime()
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert event.conversation_id == SESSION
    assert event.user_agent is None


async def test_role_lookups_come_before_the_budget_starts(load: Load) -> None:
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    await handle(runtime, load("gateway_request_jwt"), ctx())
    [looked_up_at] = roles.looked_up_at
    [started] = checker_of(runtime).started
    assert started >= looked_up_at


async def test_a_deny_is_a_transformed_response_with_the_request_id(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("tagged file",)))
    answer = await handle(runtime, load("gateway_request"), ctx())
    text = denied_text(answer)
    assert answer == deny_body(2, text)
    assert "tagged file" in text
    assert not text.startswith(WITHHELD)
    assert is_own_deny(text, runtime.key)


async def test_a_deny_keeps_a_string_id(load: Load) -> None:
    payload = load("gateway_request")
    body_of(payload)["id"] = "req-7"
    answer = await handle(make_runtime(Outcome("deny", ("no",))), payload, ctx())
    assert answer == deny_body("req-7", denied_text(answer))


async def test_a_deny_without_an_id_has_a_null_id(load: Load) -> None:
    payload = load("gateway_request")
    del body_of(payload)["id"]
    answer = await handle(make_runtime(Outcome("deny", ("no",))), payload, ctx())
    assert answer == deny_body(None, denied_text(answer))


async def test_shadow_mode_echoes_a_deny_and_logs_it(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request")
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    assert await handle(runtime, payload, ctx()) == allow_request(body_of(payload))
    [line] = events(caplog, "shadow")
    assert (line["surface"], line["check"], line["verdict"]) == ("gateway", "call", "deny")


@pytest.mark.parametrize(
    ("mode", "allowed"), [("allow", True), ("deny", False)], ids=["fail-open", "fail-closed"]
)
async def test_no_answer_follows_the_fail_mode(
    load: Load, caplog: pytest.LogCaptureFixture, mode: str, allowed: bool
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request")
    runtime = make_runtime(Outcome("no_answer", why="timeout"), verdict_fail_mode=mode)
    answer = await handle(runtime, payload, ctx())
    if allowed:
        assert answer == allow_request(body_of(payload))
    else:
        text = denied_text(answer)
        assert text.startswith(POLICY_UNAVAILABLE)
        assert is_own_deny(text, runtime.key)
    [line] = events(caplog, "fail_mode")
    assert (line["surface"], line["check"], line["reason"], line["mode"]) == (
        "gateway",
        "call",
        "timeout",
        mode,
    )


async def test_a_missing_request_id_is_replaced_and_logged(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime()
    await handle(runtime, load("gateway_request"), ctx(REQUEST_ID=None))
    [event] = checker_of(runtime).invocations
    [use] = event.requested_tool_uses or []
    assert use.tool_use_id is not None
    uuid.UUID(use.tool_use_id)
    assert event.request_id == f"{SESSION}:{use.tool_use_id}:request"
    assert len(events(caplog, "missing_request_id")) == 1


async def test_a_present_request_id_logs_nothing(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    await handle(make_runtime(), load("gateway_request"), ctx())
    assert events(caplog, "missing_request_id") == []


async def test_a_truncated_argument_object_is_logged_and_gives_no_refs(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request")
    params_of(payload)["arguments"] = {"truncated": True, "preview": '{"fileId": "ID1'}
    runtime = make_runtime()
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert event.accessed_files is None
    [line] = events(caplog, "truncated_input")
    assert (line["surface"], line["tool"], line["call_id"]) == ("gateway", READ, RID)
    assert "ID1" not in json.dumps(line)


# identity


async def test_a_jwt_call_is_checked_with_the_gateway_role(load: Load) -> None:
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    payload = load("gateway_request_jwt")
    assert await handle(runtime, payload, ctx()) == allow_request(body_of(payload))
    [event] = checker_of(runtime).invocations
    identity = identity_of(event)
    assert (identity.principal_arn, identity.gateway_arn) == (GATEWAY_ROLE, GATEWAY_ARN)
    assert identity.agent_arn is None
    assert roles.looked_up == [("gateway", GATEWAY_ARN)]


async def test_a_jwt_call_without_a_role_is_still_sent_with_the_gateway_arn(load: Load) -> None:
    runtime = make_runtime(roles=FakeRoles(gateway=None))
    await handle(runtime, load("gateway_request_jwt"), ctx())
    [event] = checker_of(runtime).invocations
    identity = identity_of(event)
    assert identity.principal_arn is None
    assert identity.gateway_arn == GATEWAY_ARN


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_neither_gateway_arn_nor_principal_takes_the_no_answer_path(
    load: Load, caplog: pytest.LogCaptureFixture, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request_jwt")
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload, ctx(GATEWAY_ARN=None))
    assert checker_of(runtime).invocations == []
    if fail_mode == "allow":
        assert answer == allow_request(body_of(payload))
    else:
        assert denied_text(answer).startswith(POLICY_UNAVAILABLE)
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"]) == ("call", "no_identity")


# pass-through and unreadable requests


async def test_a_tools_list_request_is_echoed_without_a_request(load: Load) -> None:
    runtime = make_runtime()
    payload = load("gateway_tools_list_request")
    assert await handle(runtime, payload, ctx()) == allow_request(body_of(payload))
    assert checker_of(runtime).invocations == []


async def test_initialize_is_echoed_without_lookups(load: Load) -> None:
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    payload = load("gateway_tools_list_request")
    body_of(payload)["method"] = "initialize"
    assert await handle(runtime, payload, ctx()) == allow_request(body_of(payload))
    assert checker_of(runtime).invocations == []
    assert roles.looked_up == []


async def test_an_http_payload_is_answered_with_an_empty_http(load: Load) -> None:
    payload: dict[str, JsonValue] = {
        "interceptorInputVersion": VERSION,
        "http": {"gatewayRequest": {"path": "/mcp", "headers": {"Authorization": SECRET}}},
    }
    runtime = make_runtime()
    assert await handle(runtime, payload, ctx()) == {
        "interceptorOutputVersion": VERSION,
        "http": {},
    }
    assert checker_of(runtime).invocations == []


async def test_a_batch_without_a_call_is_echoed(load: Load) -> None:
    payload = load("gateway_tools_list_request")
    batch: JsonValue = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    request_of(payload)["body"] = batch
    runtime = make_runtime()
    assert await handle(runtime, payload, ctx()) == allow_request(batch)
    assert checker_of(runtime).invocations == []


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_a_batch_with_a_call_is_denied_whatever_the_fail_mode(
    load: Load, caplog: pytest.LogCaptureFixture, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request")
    request_of(payload)["body"] = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        body_of(payload),
    ]
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload, ctx())
    text = denied_text(answer)
    assert answer == deny_body(None, text)
    assert text.startswith(UNREADABLE)
    assert is_own_deny(text, runtime.key)
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "unreadable")
    assert (line["surface"], line["check"], line["reason"]) == (
        "gateway",
        "call",
        "batch_with_call",
    )


async def test_a_batch_with_a_call_is_echoed_in_shadow_mode(load: Load) -> None:
    payload = load("gateway_request")
    batch: JsonValue = [body_of(payload)]
    request_of(payload)["body"] = batch
    runtime = make_runtime(shadow_mode=True)
    assert await handle(runtime, payload, ctx()) == allow_request(batch)


@pytest.mark.parametrize("name", ["", "   ", None])
async def test_a_call_without_a_tool_name_is_unreadable(
    load: Load, caplog: pytest.LogCaptureFixture, name: str | None
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request")
    params_of(payload)["name"] = name
    runtime = make_runtime(verdict_fail_mode="allow")
    answer = await handle(runtime, payload, ctx())
    text = denied_text(answer)
    assert answer == deny_body(2, text)  # the id is still readable
    assert text.startswith(UNREADABLE)
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "unreadable")
    assert line["reason"] == "bad_tool_call"


async def test_a_body_that_is_not_an_object_is_unreadable(load: Load) -> None:
    payload = load("gateway_request")
    request_of(payload)["body"] = "tools/call"
    answer = await handle(make_runtime(), payload, ctx())
    assert answer == deny_body(None, denied_text(answer))


async def test_a_request_envelope_that_does_not_validate_is_unreadable(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_request")
    mcp_of(payload)["gatewayRequest"] = "oops"
    runtime = make_runtime(verdict_fail_mode="allow")
    answer = await handle(runtime, payload, ctx())
    assert answer == deny_body(None, denied_text(answer))
    [line] = events(caplog, "unreadable")
    assert (line["check"], line["reason"]) == ("call", "bad_envelope")


async def test_an_mcp_part_that_is_not_an_object_takes_the_no_answer_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload: dict[str, JsonValue] = {"interceptorInputVersion": VERSION, "mcp": "oops"}
    runtime = make_runtime(verdict_fail_mode="allow")
    assert await handle(runtime, payload, ctx()) == {
        "interceptorOutputVersion": VERSION,
        "mcp": {},
    }
    [line] = events(caplog, "fail_mode")
    assert line["reason"] == "error"


# the Authorization header


async def test_the_authorization_header_is_never_read(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    outcomes = [Outcome("deny", ("no",)), Outcome("no_answer", why="status"), Outcome("allow")]
    runtime = make_runtime(*outcomes, verdict_fail_mode="deny")
    payloads = [
        load("gateway_request"),
        load("gateway_request"),
        load("gateway_response"),
        load("gateway_request_jwt"),
    ]
    for payload in payloads:
        assert SECRET in json.dumps(payload) or "SYNTHETIC.JWT" in json.dumps(payload)
        answer = await handle(runtime, payload, ctx())
        assert SECRET not in json.dumps(answer)
        assert "SYNTHETIC" not in json.dumps(answer)
    seen = [e.model_dump_json() for e in checker_of(runtime).invocations]
    for text in [*seen, caplog.text]:
        assert SECRET not in text
        assert "SYNTHETIC" not in text


# RESPONSE


async def test_response_sends_a_result_check_and_echoes_the_response(load: Load) -> None:
    runtime = make_runtime()
    payload = with_result(load, drive_result())
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    [event] = checker_of(runtime).invocations
    assert event.request_id == f"{SESSION}:{RID}:result"
    assert event.requested_tool_uses is None
    assert event.conversation_id == SESSION
    assert event.user_agent == "example-agent/1.0"
    identity = identity_of(event)
    assert (identity.principal_arn, identity.gateway_arn) == (PRINCIPAL, GATEWAY_ARN)
    [use] = event.used_tools or []
    assert (use.tool_use_id, use.is_error) == (RID, False)
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]
    assert file.name == "banana-bread.md"
    assert file.content_hashes is None  # read_file_content: no hash


async def test_the_two_checks_of_a_call_share_the_tool_use_id(load: Load) -> None:
    runtime = make_runtime()
    await handle(runtime, load("gateway_request"), ctx())
    await handle(runtime, load("gateway_response"), ctx())
    call_check, result_check = checker_of(runtime).invocations
    [requested] = call_check.requested_tool_uses or []
    [used] = result_check.used_tools or []
    assert requested.tool_use_id == used.tool_use_id == RID
    assert call_check.request_id != result_check.request_id


async def test_response_arguments_come_from_the_request_it_carries(load: Load) -> None:
    runtime = make_runtime()
    payload = with_result(load, {"isError": False, "content": [{"type": "text", "text": "x"}]})
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]  # the result names no file: the arguments do


async def test_allow_echoes_the_response_with_its_unknown_fields(load: Load) -> None:
    payload = load("gateway_response")
    response = response_of(payload)
    response["statusCode"] = 202
    response["headers"] = {"content-type": "application/json", "x-extra": "1"}
    body = obj(response["body"])
    body["extra"] = {"keep": [1, 2]}
    obj(body["result"])["_meta"] = {"k": "v"}
    obj(body["result"])["structuredContent"] = {"a": 1}
    answer = await handle(make_runtime(), payload, ctx())
    assert answer == echoed_response(payload)
    echoed = obj(obj(obj(answer)["mcp"])["transformedGatewayResponse"])
    assert echoed["statusCode"] == 202
    assert echoed["body"] == body


async def test_allow_leaves_the_headers_out_when_the_payload_has_none(load: Load) -> None:
    payload = load("gateway_response")
    del response_of(payload)["headers"]
    answer = await handle(make_runtime(), payload, ctx())
    echoed = obj(obj(obj(answer)["mcp"])["transformedGatewayResponse"])
    assert set(echoed) == {"statusCode", "body"}


async def test_an_error_flag_in_the_result_is_an_error_without_files(load: Load) -> None:
    runtime = make_runtime()
    result = obj(drive_result())
    result["isError"] = True
    await handle(runtime, with_result(load, result), ctx())
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is True
    assert event.accessed_files is None


async def test_a_policy_denial_is_checked_like_any_result(load: Load) -> None:
    runtime = make_runtime()
    payload = load("gateway_response_policy_denied")
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert (use.tool_use_id, use.is_error) == (RID, True)
    assert event.accessed_files is None
    assert event.request_id == f"{SESSION}:{RID}:result"


async def test_an_error_is_read_before_the_result(load: Load) -> None:
    payload = load("gateway_response_policy_denied")
    obj(response_of(payload)["body"])["result"] = drive_result()
    runtime = make_runtime()
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is True
    assert event.accessed_files is None


@pytest.mark.parametrize("result", [None, "just a string", ["a"], 7])
async def test_a_reply_without_a_readable_result_gives_no_references(
    load: Load, result: JsonValue
) -> None:
    payload = load("gateway_response")
    body = obj(response_of(payload)["body"])
    del body["result"]
    if result is not None:
        body["result"] = result
    runtime = make_runtime()
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is False
    assert event.accessed_files is None


async def test_a_body_that_is_not_an_object_is_still_checked(load: Load) -> None:
    payload = load("gateway_response")
    response_of(payload)["body"] = "plain text"
    runtime = make_runtime()
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    [event] = checker_of(runtime).invocations
    assert event.accessed_files is None


async def test_a_result_with_structured_content_is_read(load: Load) -> None:
    payload = load("gateway_response")
    params_of(payload)["name"] = "gdrive___search_files"
    params_of(payload)["arguments"] = {"query": "q"}
    hits: JsonValue = {"files": [{"id": "A", "title": "a.md", "mimeType": "text/markdown"}]}
    obj(obj(response_of(payload)["body"])["result"]).update(content=[], structuredContent=hits)
    runtime = make_runtime()
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    [file] = event.accessed_files or []
    assert (file.file_refs, file.name) == (["google/A"], "a.md")


async def test_a_deny_withholds_the_result(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("tagged file",)))
    answer = await handle(runtime, load("gateway_response"), ctx())
    text = denied_text(answer)
    assert answer == deny_body(2, text)
    assert text.startswith(WITHHELD + "tagged file")
    assert is_own_deny(text, runtime.key)
    assert "Synthetic file body" not in json.dumps(answer)


async def test_the_deny_text_is_capped_with_its_prefix(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("x" * 900,)))
    text = denied_text(await handle(runtime, load("gateway_response"), ctx()))
    assert text.startswith(WITHHELD + "x")
    body, _, _ref = text.rpartition(" (ref ")
    assert len(body) == 500


async def test_response_shadow_mode_echoes_a_deny(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_response")
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    [line] = events(caplog, "shadow")
    assert (line["surface"], line["check"]) == ("gateway", "result")


@pytest.mark.parametrize(
    ("mode", "allowed"), [("allow", True), ("deny", False)], ids=["fail-open", "fail-closed"]
)
async def test_response_no_answer_follows_the_fail_mode(
    load: Load, mode: str, allowed: bool
) -> None:
    payload = load("gateway_response")
    runtime = make_runtime(Outcome("no_answer", why="status"), verdict_fail_mode=mode)
    answer = await handle(runtime, payload, ctx())
    if allowed:
        assert answer == echoed_response(payload)
    else:
        text = denied_text(answer)
        assert text.startswith(WITHHELD + POLICY_UNAVAILABLE)
        assert is_own_deny(text, runtime.key)


async def test_a_response_with_no_context_is_treated_like_a_jwt_call(load: Load) -> None:
    payload = load("gateway_response")
    del request_of(payload)["context"]
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    await handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    identity = identity_of(event)
    assert identity.principal_arn == GATEWAY_ROLE
    assert identity.agent_arn is None
    assert roles.looked_up == [("gateway", GATEWAY_ARN)]


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_a_response_without_an_identity_takes_the_no_answer_path(
    load: Load, caplog: pytest.LogCaptureFixture, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_response")
    del request_of(payload)["context"]
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload, ctx(GATEWAY_ARN=None))
    assert checker_of(runtime).invocations == []
    if fail_mode == "allow":
        assert answer == echoed_response(payload)
    else:
        assert denied_text(answer).startswith(WITHHELD + POLICY_UNAVAILABLE)
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"]) == ("result", "no_identity")


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_a_response_with_no_request_takes_the_no_answer_path(
    load: Load, caplog: pytest.LogCaptureFixture, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_response_no_request")
    roles = FakeRoles()
    runtime = make_runtime(verdict_fail_mode=fail_mode, roles=roles)
    answer = await handle(runtime, payload, ctx())
    assert checker_of(runtime).invocations == []
    assert roles.looked_up == []
    if fail_mode == "allow":
        assert answer == echoed_response(payload)
    else:
        text = denied_text(answer)
        assert text.startswith(WITHHELD + POLICY_UNAVAILABLE)
        assert answer == deny_body(2, text)  # the id of the reply: there is no request
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"]) == ("result", "no_request")


@pytest.mark.parametrize("field", ["gatewayResponse", "isStreamingResponse"])
@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_an_invalid_response_envelope_takes_the_no_answer_path(
    load: Load, caplog: pytest.LogCaptureFixture, fail_mode: str, field: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_response")
    if field == "gatewayResponse":
        mcp_of(payload)["gatewayResponse"] = "oops"
    else:
        response_of(payload)["isStreamingResponse"] = "maybe"
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload, ctx())
    assert checker_of(runtime).invocations == []
    if fail_mode == "allow":
        assert answer == {"interceptorOutputVersion": VERSION, "mcp": {}}
    else:
        assert denied_text(answer).startswith(WITHHELD + POLICY_UNAVAILABLE)
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"]) == ("result", "error")
    assert events(caplog, "unreadable") == []


async def test_a_streaming_response_is_echoed_with_a_pass_through_line(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    roles = FakeRoles()
    runtime = make_runtime(Outcome("deny", ("no",)), roles=roles)
    payload = load("gateway_response_streaming")
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    assert checker_of(runtime).invocations == []
    assert roles.looked_up == []
    [line] = events(caplog, "pass_through")
    assert (line["surface"], line["reason"]) == ("gateway", "streaming_response")


async def test_a_response_to_a_tools_list_is_echoed(load: Load) -> None:
    payload = load("gateway_response")
    request_of(payload)["body"] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    runtime = make_runtime()
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    assert checker_of(runtime).invocations == []


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_a_response_to_a_batch_with_a_call_is_withheld(
    load: Load, caplog: pytest.LogCaptureFixture, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("gateway_response")
    request_of(payload)["body"] = [body_of(payload)]
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload, ctx())
    text = denied_text(answer)
    assert answer == deny_body(None, text)
    assert text.startswith(WITHHELD + UNREADABLE)
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "unreadable")
    assert (line["check"], line["reason"]) == ("result", "batch_with_call")


async def test_a_response_to_a_call_without_a_name_is_withheld(load: Load) -> None:
    payload = load("gateway_response")
    params_of(payload)["name"] = ""
    runtime = make_runtime()
    answer = await handle(runtime, payload, ctx())
    text = denied_text(answer)
    assert answer == deny_body(2, text)
    assert text.startswith(WITHHELD + UNREADABLE)


# our own deny


@pytest.mark.parametrize("is_error", [True, False])
async def test_our_own_deny_is_allowed_without_a_request(load: Load, is_error: bool) -> None:
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    text = with_reference(WITHHELD + "no", runtime.key)
    result: JsonValue = {"isError": is_error, "content": [{"type": "text", "text": text}]}
    payload = with_result(load, result)
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    assert checker_of(runtime).invocations == []
    assert roles.looked_up == []


async def test_our_own_deny_is_allowed_whatever_the_request(load: Load) -> None:
    runtime = make_runtime()
    text = with_reference("Blocked.", runtime.key)
    result: JsonValue = {"isError": True, "content": [{"type": "text", "text": text}]}
    payload = with_result(load, result, "gateway_response_no_request")
    assert await handle(runtime, payload, ctx()) == echoed_response(payload)
    assert checker_of(runtime).invocations == []


@pytest.mark.parametrize("variant", ["second_block", "structured", "other_key", "other_key_signed"])
async def test_a_deny_text_that_is_not_exactly_ours_is_checked(load: Load, variant: str) -> None:
    runtime = make_runtime()
    text = with_reference("Blocked", runtime.key)
    block: JsonValue = {"type": "text", "text": text}
    result: dict[str, JsonValue] = {"isError": True, "content": [block]}
    if variant == "second_block":
        result["content"] = [block, {"type": "text", "text": "more"}]
    elif variant == "structured":
        result["structuredContent"] = {"a": 1}
    elif variant == "other_key":
        result["content"] = [{"type": "text", "text": text.replace("Blocked", "Allowed")}]
    else:
        signed = with_reference("Blocked", derive_key("other-token"))
        result["content"] = [{"type": "text", "text": signed}]
    await handle(runtime, with_result(load, result), ctx())
    assert len(checker_of(runtime).invocations) == 1


async def test_an_error_whose_message_is_our_text_is_still_checked(load: Load) -> None:
    runtime = make_runtime()
    payload = load("gateway_response_policy_denied")
    error = obj(obj(response_of(payload)["body"])["error"])
    error["message"] = with_reference("Blocked", runtime.key)
    await handle(runtime, payload, ctx())
    assert len(checker_of(runtime).invocations) == 1


# logs


async def test_no_log_line_carries_payload_text(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    await handle(runtime, load("gateway_request"), ctx(REQUEST_ID=None))
    payload = with_result(load, drive_result("SECRET-ID"))
    await handle(runtime, payload, ctx())
    streaming = load("gateway_response_streaming")
    await handle(runtime, streaming, ctx())
    assert "SECRET-ID" not in caplog.text
    assert "banana-bread" not in caplog.text
    assert "Synthetic file body" not in caplog.text
