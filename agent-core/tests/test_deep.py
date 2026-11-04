"""Deeply nested payloads are checked, never skipped, and both surfaces agree."""

import logging

import pytest
from pydantic import JsonValue

from helpers import (
    LOGGER,
    Load,
    checker_of,
    ctx,
    events,
    make_runtime,
    mcp_of,
    obj,
    params_of,
    request_of,
    response_of,
)
from slashid_agentcore_forwarder import gateway, harness, tool_call
from slashid_agentcore_forwarder.checks import UNREADABLE, Outcome
from slashid_agentcore_forwarder.deny import is_own_deny

DEPTHS = [300, 5_000]
HUGE = 100_000
READ = "gdrive___read_file_content"


def nest(depth: int, leaf: JsonValue = "x") -> JsonValue:
    node = leaf
    for _ in range(depth):
        node = {"a": node}
    return node


def nest_list(depth: int) -> JsonValue:
    node: list[JsonValue] = []
    for _ in range(depth):
        node = [node]
    return node


def gateway_call(load: Load, arguments: JsonValue) -> dict[str, JsonValue]:
    payload = load("gateway_request")
    params_of(payload)["arguments"] = arguments
    return payload


def gateway_result(load: Load, result: JsonValue) -> dict[str, JsonValue]:
    payload = load("gateway_response")
    obj(response_of(payload)["body"])["result"] = result
    return payload


def harness_call(load: Load, tool_input: JsonValue) -> dict[str, JsonValue]:
    payload = load("harness_before_tool_call")
    obj(payload["context"])["toolInput"] = tool_input
    return payload


def harness_result(load: Load, result: JsonValue) -> dict[str, JsonValue]:
    payload = load("harness_after_tool_call")
    obj(payload["context"]).update(toolName=READ, toolType="agentcore_gateway", toolResult=result)
    return payload


def refs(event: object) -> list[str]:
    files = getattr(event, "accessed_files", None) or []
    return [ref for file in files for ref in file.file_refs or []]


def is_deny(answer: JsonValue, key: bytes) -> bool:
    """A Gateway deny: a transformed response whose one text block is our own deny text."""
    mcp = obj(obj(answer)["mcp"])
    if "transformedGatewayResponse" not in mcp:
        return False
    body = obj(obj(mcp["transformedGatewayResponse"])["body"])
    result = body.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or len(content) != 1:
        return False
    block = content[0]
    text = block.get("text") if isinstance(block, dict) else None
    return isinstance(text, str) and is_own_deny(text, key)


# the Gateway


@pytest.mark.parametrize("depth", DEPTHS)
async def test_gateway_deep_arguments_are_checked_with_their_file_ref(
    load: Load, depth: int
) -> None:
    payload = gateway_call(load, {"fileId": "ID1", "deep": nest(depth)})
    body = obj(request_of(payload)["body"])
    runtime = make_runtime()
    answer = await gateway.handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert refs(event) == ["google/ID1"]
    echoed = obj(obj(obj(answer)["mcp"])["transformedGatewayRequest"])
    assert echoed["body"] is body


@pytest.mark.parametrize("depth", DEPTHS)
async def test_gateway_deep_arguments_can_be_denied(load: Load, depth: int) -> None:
    payload = gateway_call(load, {"fileId": "ID1", "deep": nest(depth)})
    runtime = make_runtime(Outcome("deny", ("tagged",)))
    assert is_deny(await gateway.handle(runtime, payload, ctx()), runtime.key)
    assert len(checker_of(runtime).invocations) == 1


@pytest.mark.parametrize("depth", DEPTHS)
async def test_gateway_arguments_that_are_one_deep_value_are_checked(
    load: Load, depth: int
) -> None:
    runtime = make_runtime()
    await gateway.handle(runtime, gateway_call(load, nest(depth)), ctx())
    assert len(checker_of(runtime).invocations) == 1


@pytest.mark.parametrize("depth", DEPTHS)
async def test_gateway_deep_result_is_checked_with_the_request_input(
    load: Load, depth: int
) -> None:
    result: JsonValue = {
        "isError": False,
        "content": [{"type": "text", "text": "{}"}],
        "structuredContent": nest(depth),
    }
    payload = gateway_result(load, result)
    runtime = make_runtime()
    answer = await gateway.handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert [use.is_error for use in event.used_tools or []] == [False]
    assert refs(event) == ["google/ID1"]
    echoed = obj(obj(obj(answer)["mcp"])["transformedGatewayResponse"])
    assert echoed["body"] is obj(response_of(payload))["body"]


@pytest.mark.parametrize("depth", DEPTHS)
async def test_gateway_deep_result_keeps_its_error_flag(load: Load, depth: int) -> None:
    payload = gateway_result(load, {"isError": True, "content": nest(depth)})
    runtime = make_runtime()
    await gateway.handle(runtime, payload, ctx())
    [event] = checker_of(runtime).invocations
    assert [use.is_error for use in event.used_tools or []] == [True]
    assert event.accessed_files is None


async def test_gateway_deep_result_can_be_withheld(load: Load) -> None:
    payload = gateway_result(load, {"isError": False, "structuredContent": nest(300)})
    runtime = make_runtime(Outcome("deny", ("tagged",)))
    assert is_deny(await gateway.handle(runtime, payload, ctx()), runtime.key)


@pytest.mark.parametrize("where", ["headers", "context", "_meta", "extra"])
async def test_gateway_deep_values_elsewhere_are_checked(load: Load, where: str) -> None:
    payload = load("gateway_request")
    request = request_of(payload)
    if where == "headers":
        obj(request["headers"])["x-deep"] = nest(300)
    elif where == "context":
        obj(request["context"])["deep"] = nest(300)
    elif where == "_meta":
        obj(params_of(payload)["_meta"])["deep"] = nest(300)
    else:
        mcp_of(payload)["extra"] = nest(300)
    runtime = make_runtime()
    await gateway.handle(runtime, payload, ctx())
    assert len(checker_of(runtime).invocations) == 1


async def test_gateway_deep_id_is_an_unreadable_call_not_an_error(load: Load) -> None:
    payload = load("gateway_request")
    obj(request_of(payload)["body"])["id"] = nest(300)
    runtime = make_runtime(verdict_fail_mode="allow")
    answer = await gateway.handle(runtime, payload, ctx())
    assert is_deny(answer, runtime.key)
    assert checker_of(runtime).invocations == []


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_gateway_a_request_that_overflows_the_stack_is_denied(
    load: Load,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fail_mode: str,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)

    def overflow(*_: object) -> None:
        raise RecursionError

    monkeypatch.setattr(tool_call, "connector_refs", overflow)
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await gateway.handle(runtime, load("gateway_request"), ctx())
    text = obj(obj(obj(obj(obj(answer)["mcp"])["transformedGatewayResponse"])["body"])["result"])
    assert obj(text)["isError"] is True
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "unreadable")
    assert (line["check"], line["reason"]) == ("call", "too_deep")


async def test_gateway_a_response_that_overflows_the_stack_is_withheld(
    load: Load, monkeypatch: pytest.MonkeyPatch
) -> None:
    def overflow(*_: object, **__: object) -> None:
        raise RecursionError

    monkeypatch.setattr(tool_call, "connector_files", overflow)
    runtime = make_runtime(verdict_fail_mode="allow")
    answer = await gateway.handle(runtime, load("gateway_response"), ctx())
    assert is_deny(answer, runtime.key)
    assert checker_of(runtime).invocations == []
    assert UNREADABLE in str(answer)


# the Harness


@pytest.mark.parametrize("depth", DEPTHS)
async def test_harness_deep_input_is_checked_with_its_file_ref(load: Load, depth: int) -> None:
    runtime = make_runtime()
    answer = await harness.handle(runtime, harness_call(load, {"fileId": "ID1", "d": nest(depth)}))
    assert obj(answer)["decision"] == "allow"
    [event] = checker_of(runtime).invocations
    assert refs(event) == ["google/ID1"]


@pytest.mark.parametrize("depth", DEPTHS)
async def test_harness_deep_input_can_be_denied(load: Load, depth: int) -> None:
    runtime = make_runtime(Outcome("deny", ("tagged",)))
    answer = await harness.handle(runtime, harness_call(load, {"fileId": "ID1", "d": nest(depth)}))
    assert obj(answer)["decision"] == "deny"
    assert len(checker_of(runtime).invocations) == 1


@pytest.mark.parametrize("depth", DEPTHS)
async def test_harness_deep_result_is_checked_and_keeps_its_error_flag(
    load: Load, depth: int
) -> None:
    runtime = make_runtime()
    for status, want in (("success", False), ("error", True)):
        result: JsonValue = {
            "status": status,
            "content": [{"text": "x"}],
            "structuredContent": nest(depth),
        }
        await harness.handle(runtime, harness_result(load, result))
        event = checker_of(runtime).invocations[-1]
        assert [use.is_error for use in event.used_tools or []] == [want]


async def test_harness_deep_result_content_is_checked(load: Load) -> None:
    runtime = make_runtime()
    await harness.handle(runtime, harness_result(load, {"status": "success", "content": nest(300)}))
    assert len(checker_of(runtime).invocations) == 1


@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_harness_a_call_that_overflows_the_stack_is_denied(
    load: Load,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fail_mode: str,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)

    def overflow(*_: object) -> None:
        raise RecursionError

    monkeypatch.setattr(tool_call, "connector_refs", overflow)
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await harness.handle(runtime, load("harness_before_tool_call"))
    assert obj(answer)["decision"] == "deny"
    assert str(obj(answer)["reason"]).startswith(UNREADABLE)
    [line] = events(caplog, "unreadable")
    assert (line["check"], line["reason"]) == ("call", "too_deep")


async def test_harness_a_result_that_overflows_the_stack_is_denied(
    load: Load, monkeypatch: pytest.MonkeyPatch
) -> None:
    def overflow(*_: object, **__: object) -> None:
        raise RecursionError

    monkeypatch.setattr(tool_call, "connector_files", overflow)
    runtime = make_runtime(verdict_fail_mode="allow")
    answer = await harness.handle(runtime, load("harness_after_tool_call"))
    assert obj(answer)["decision"] == "deny"
    assert checker_of(runtime).invocations == []


# both surfaces


@pytest.mark.parametrize("depth", DEPTHS)
@pytest.mark.parametrize("verdict", ["allow", "deny"])
async def test_the_surfaces_agree_on_a_deep_call(load: Load, depth: int, verdict: str) -> None:
    arguments = {"fileId": "ID1", "deep": nest(depth)}
    outcome = Outcome("deny", ("tagged",)) if verdict == "deny" else Outcome("allow")
    gw, hn = make_runtime(outcome), make_runtime(outcome)
    gw_answer = await gateway.handle(gw, gateway_call(load, arguments), ctx())
    hn_answer = await harness.handle(hn, harness_call(load, arguments))
    assert (
        is_deny(gw_answer, gw.key) == (obj(hn_answer)["decision"] == "deny") == (verdict == "deny")
    )
    [gw_event] = checker_of(gw).invocations
    [hn_event] = checker_of(hn).invocations
    assert refs(gw_event) == refs(hn_event) == ["google/ID1"]


# a structure far deeper than the recursion limit, built in memory


async def test_a_huge_nesting_is_checked_on_the_gateway(load: Load) -> None:
    runtime = make_runtime()
    request = gateway_call(load, {"fileId": "ID1", "deep": nest_list(HUGE)})
    await gateway.handle(runtime, request, ctx())
    result = gateway_result(load, {"isError": False, "structuredContent": nest_list(HUGE)})
    await gateway.handle(runtime, result, ctx())
    assert len(checker_of(runtime).invocations) == 2


async def test_a_huge_nesting_is_checked_on_the_harness(load: Load) -> None:
    runtime = make_runtime()
    await harness.handle(runtime, harness_call(load, {"fileId": "ID1", "d": nest_list(HUGE)}))
    result = {"status": "success", "structuredContent": nest_list(HUGE)}
    await harness.handle(runtime, harness_result(load, result))
    assert len(checker_of(runtime).invocations) == 2


async def test_a_huge_nesting_in_every_gateway_field_never_raises(load: Load) -> None:
    payload = gateway_call(load, nest_list(HUGE))
    request = request_of(payload)
    request["headers"] = {"baggage": nest_list(HUGE), "user-agent": nest_list(HUGE)}
    request["context"] = {"identity": nest_list(HUGE)}
    obj(params_of(payload))["_meta"] = nest_list(HUGE)
    runtime = make_runtime()
    await gateway.handle(runtime, payload, ctx())
    assert len(checker_of(runtime).invocations) == 1
