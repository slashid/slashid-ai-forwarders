import asyncio
import copy
import inspect
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from helpers import (
    GATEWAY_ARN,
    LOGGER,
    RID,
    FakeChecker,
    Load,
    allow_request,
    body_of,
    checker_of,
    ctx,
    denied_text,
    deny_body,
    events,
    looked_up,
    make_config,
    make_runtime,
    mcp_of,
    obj,
    request_of,
    response_of,
)
from slashid_agentcore_forwarder import handler, runtime
from slashid_agentcore_forwarder.checks import POLICY_UNAVAILABLE, UNREADABLE, Outcome
from slashid_agentcore_forwarder.config import load_config
from slashid_agentcore_forwarder.deny import derive_key, is_own_deny
from slashid_agentcore_forwarder.handler import dispatch, lambda_handler

ALLOW: JsonValue = {"decision": "allow", "reason": "slashid"}
HTTP_ANSWER: JsonValue = {"interceptorOutputVersion": "1.0", "http": {}}
EMPTY_MCP: JsonValue = {"interceptorOutputVersion": "1.0", "mcp": {}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in [n for n in os.environ if n.startswith("SLASHID_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "env-token")
    monkeypatch.setenv("SLASHID_ENDPOINT", "http://env")
    load_config.cache_clear()
    runtime.reset_runtime()
    handler.reset_announced()
    yield
    load_config.cache_clear()
    runtime.reset_runtime()


def echo_request(payload: dict[str, JsonValue]) -> JsonValue:
    return allow_request(body_of(payload))


def echo_response(payload: dict[str, JsonValue]) -> JsonValue:
    response = response_of(payload)
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": response["statusCode"],
                "headers": response["headers"],
                "body": response["body"],
            }
        },
    }


def junk(value: object) -> dict[str, JsonValue]:
    """A payload that is not an object, for the types."""
    return cast("dict[str, JsonValue]", value)


def deny_reason(answer: JsonValue) -> str:
    reason = obj(answer)["reason"]
    assert isinstance(reason, str)
    return reason


# Routes


async def test_a_harness_hook_goes_to_the_harness_adapter(load: Load) -> None:
    rt = make_runtime(Outcome("allow"))
    assert await dispatch(rt, load("harness_before_tool_call"), ctx()) == ALLOW
    [invocation] = checker_of(rt).invocations
    assert invocation.identity_details is not None
    assert len(looked_up(rt)) == 1


async def test_a_harness_deny_is_applied(load: Load) -> None:
    rt = make_runtime(Outcome("deny", ("no",)))
    answer = await dispatch(rt, load("harness_before_tool_call"), ctx())
    assert obj(answer)["decision"] == "deny"
    assert is_own_deny(deny_reason(answer), rt.key)


async def test_a_gateway_request_goes_to_the_gateway_adapter(load: Load) -> None:
    payload = load("gateway_request")
    rt = make_runtime(Outcome("allow"))
    assert await dispatch(rt, payload, ctx()) == echo_request(payload)
    assert len(checker_of(rt).invocations) == 1


async def test_a_gateway_response_goes_to_the_gateway_adapter(load: Load) -> None:
    payload = load("gateway_response")
    rt = make_runtime(Outcome("allow"))
    assert await dispatch(rt, payload, ctx()) == echo_response(payload)
    assert len(checker_of(rt).invocations) == 1


async def test_a_gateway_deny_is_applied(load: Load) -> None:
    rt = make_runtime(Outcome("deny", ("no",)))
    answer = await dispatch(rt, load("gateway_request"), ctx())
    assert is_own_deny(denied_text(answer), rt.key)


async def test_an_http_payload_is_answered_without_a_check() -> None:
    rt = make_runtime()
    payload: dict[str, JsonValue] = {"interceptorInputVersion": "1.0", "http": {"x": 1}}
    assert await dispatch(rt, payload, ctx()) == HTTP_ANSWER
    assert checker_of(rt).invocations == []


@pytest.mark.parametrize("event", ["before_invocation", "after_invocation"])
async def test_invocation_hooks_are_allowed_without_a_check(load: Load, event: str) -> None:
    payload = load("harness_before_tool_call")
    payload["event"] = event
    rt = make_runtime()
    assert await dispatch(rt, payload, ctx()) == ALLOW
    assert checker_of(rt).invocations == []
    assert looked_up(rt) == []


async def test_an_after_tool_call_is_the_result_check(load: Load) -> None:
    rt = make_runtime(Outcome("allow"))
    assert await dispatch(rt, load("harness_after_tool_call"), ctx()) == ALLOW
    assert len(checker_of(rt).invocations) == 1


# Neither discriminator, none of the AWS keys


async def test_a_payload_that_is_not_ours_is_allowed_and_logged_by_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    payload: dict[str, JsonValue] = {"other": "SECRET-VALUE", "more": {"x": 1}}
    for mode in ("allow", "deny"):
        rt = make_runtime(verdict_fail_mode=mode)
        assert await dispatch(rt, payload, ctx()) == ALLOW
        assert checker_of(rt).invocations == []
    first, _ = events(caplog, "pass_through")
    assert first == {
        "event": "pass_through",
        "surface": "unknown",
        "reason": "unknown_payload",
        "keys": ["other", "more"],
    }
    assert "SECRET-VALUE" not in caplog.text
    assert events(caplog, "fail_mode") == []


# Switches


async def test_a_disabled_harness_surface_allows_without_a_request(load: Load) -> None:
    rt = make_runtime(harness_hooks_enabled=False)
    assert await dispatch(rt, load("harness_before_tool_call"), ctx()) == ALLOW
    assert checker_of(rt).invocations == []
    assert looked_up(rt) == []


async def test_a_disabled_gateway_surface_echoes_a_request(load: Load) -> None:
    payload = load("gateway_request")
    rt = make_runtime(gateway_hooks_enabled=False)
    assert await dispatch(rt, payload, ctx()) == echo_request(payload)
    assert checker_of(rt).invocations == []
    assert looked_up(rt) == []


async def test_a_disabled_gateway_surface_echoes_a_response(load: Load) -> None:
    payload = load("gateway_response")
    rt = make_runtime(gateway_hooks_enabled=False)
    assert await dispatch(rt, payload, ctx()) == echo_response(payload)
    assert checker_of(rt).invocations == []


async def test_a_disabled_gateway_surface_answers_an_http_payload() -> None:
    payload: dict[str, JsonValue] = {"interceptorInputVersion": "1.0", "http": {}}
    assert await dispatch(make_runtime(gateway_hooks_enabled=False), payload, ctx()) == HTTP_ANSWER


async def test_a_switch_leaves_the_other_surface_on(load: Load) -> None:
    rt = make_runtime(Outcome("deny", ("no",)), harness_hooks_enabled=False)
    answer = await dispatch(rt, load("gateway_request"), ctx())
    assert is_own_deny(denied_text(answer), rt.key)
    rt = make_runtime(Outcome("deny", ("no",)), gateway_hooks_enabled=False)
    answer = await dispatch(rt, load("harness_before_tool_call"), ctx())
    assert obj(answer)["decision"] == "deny"


async def test_an_off_switch_silences_a_format_change_deny(load: Load) -> None:
    gateway = load("gateway_request")
    gateway["interceptorInputVersion"] = "2.0"
    rt = make_runtime(verdict_fail_mode="deny", gateway_hooks_enabled=False)
    assert await dispatch(rt, gateway, ctx()) == echo_request(gateway)
    harness = load("harness_before_tool_call")
    harness["event"] = "before_model_call"
    rt = make_runtime(verdict_fail_mode="deny", harness_hooks_enabled=False)
    assert await dispatch(rt, harness, ctx()) == ALLOW


async def test_a_disabled_surface_writes_one_line_per_container(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    rt = make_runtime(harness_hooks_enabled=False)
    for _ in range(3):
        await dispatch(rt, load("harness_before_tool_call"), ctx())
    await dispatch(rt, load("gateway_request"), ctx())
    [line] = events(caplog, "pass_through")
    assert line == {"event": "pass_through", "surface": "harness", "reason": "surface_disabled"}


async def test_both_surfaces_disabled_write_a_line_each(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    rt = make_runtime(harness_hooks_enabled=False, gateway_hooks_enabled=False)
    await dispatch(rt, load("gateway_request"), ctx())
    await dispatch(rt, load("gateway_request"), ctx())
    surfaces = sorted(str(line["surface"]) for line in events(caplog, "pass_through"))
    assert surfaces == ["gateway", "harness"]


async def test_enabled_surfaces_write_no_line(load: Load, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    await dispatch(make_runtime(), load("gateway_request"), ctx())
    assert events(caplog, "pass_through") == []


async def test_the_once_flag_can_be_reset(load: Load, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    rt = make_runtime(harness_hooks_enabled=False)
    await dispatch(rt, load("harness_before_tool_call"), ctx())
    handler.reset_announced()
    await dispatch(rt, load("harness_before_tool_call"), ctx())
    assert len(events(caplog, "pass_through")) == 2


# Rule 1: payloads we cannot place


def _both(load: Load) -> dict[str, JsonValue]:
    payload = load("gateway_request")
    payload["event"] = "before_tool_call"
    return payload


def _version(load: Load) -> dict[str, JsonValue]:
    payload = load("gateway_request")
    payload["interceptorInputVersion"] = "2.0"
    return payload


def _response_version(load: Load) -> dict[str, JsonValue]:
    payload = load("gateway_response")
    payload["interceptorInputVersion"] = "2.0"
    return payload


def _neither(load: Load) -> dict[str, JsonValue]:
    return {"interceptorInputVersion": "1.0"}


def _null_mcp(load: Load) -> dict[str, JsonValue]:
    return {"interceptorInputVersion": "1.0", "mcp": None, "http": None}


def _no_response_key(load: Load) -> dict[str, JsonValue]:
    payload = load("gateway_request")
    del mcp_of(payload)["gatewayResponse"]
    return payload


def _event(load: Load) -> dict[str, JsonValue]:
    payload = load("harness_before_tool_call")
    payload["event"] = "before_model_call"
    return payload


def _mcp_only(load: Load) -> dict[str, JsonValue]:
    return {"mcp": load("gateway_request")["mcp"]}


def _request_only(load: Load) -> dict[str, JsonValue]:
    return {"gatewayRequest": request_of(load("gateway_request"))}


def _http_only(load: Load) -> dict[str, JsonValue]:
    return {"http": {}}


def _context_only(load: Load) -> dict[str, JsonValue]:
    return {"context": {"toolName": "x"}}


# (payload, shape of the answer, the best echo of what it carries, the fail_mode reason)
Unplaced = tuple[Callable[[Load], dict[str, JsonValue]], str, Callable[[Load], JsonValue], str]


def _echo_of_request(load: Load) -> JsonValue:
    return echo_request(load("gateway_request"))


def _echo_of_response(load: Load) -> JsonValue:
    return echo_response(load("gateway_response"))


def _nothing(load: Load) -> JsonValue:
    return EMPTY_MCP


def _harness(load: Load) -> JsonValue:
    return ALLOW


UNPLACED: dict[str, Unplaced] = {
    "both-discriminators": (_both, "gateway", _echo_of_request, "unknown_payload"),
    "unknown-version": (_version, "gateway", _echo_of_request, "unknown_version"),
    "unknown-version-response": (
        _response_version,
        "gateway",
        _echo_of_response,
        "unknown_version",
    ),
    "neither-mcp-nor-http": (_neither, "gateway", _nothing, "unknown_payload"),
    "null-mcp-and-http": (_null_mcp, "gateway", _nothing, "unknown_payload"),
    "absent-gateway-response": (_no_response_key, "gateway", _echo_of_request, "unknown_payload"),
    "unknown-harness-event": (_event, "harness", _harness, "unknown_event"),
    "mcp-without-discriminator": (_mcp_only, "gateway", _echo_of_request, "unknown_payload"),
    "request-without-discriminator": (_request_only, "gateway", _nothing, "unknown_payload"),
    "http-without-discriminator": (_http_only, "gateway", _nothing, "unknown_payload"),
    "context-without-discriminator": (_context_only, "harness", _harness, "unknown_payload"),
}


@pytest.fixture(params=list(UNPLACED))
def unplaced(request: pytest.FixtureRequest) -> Unplaced:
    return UNPLACED[request.param]


async def test_an_unplaced_payload_under_fail_open_gets_the_best_allow(
    load: Load, caplog: pytest.LogCaptureFixture, unplaced: Unplaced
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    build, surface, expected, reason = unplaced
    rt = make_runtime(verdict_fail_mode="allow")
    assert await dispatch(rt, build(load), ctx()) == expected(load)
    assert checker_of(rt).invocations == []
    [line] = events(caplog, "fail_mode")
    assert (line["reason"], line["mode"]) == (reason, "allow")
    assert line["surface"] == surface


async def test_an_unplaced_payload_under_fail_closed_is_denied(
    load: Load, caplog: pytest.LogCaptureFixture, unplaced: Unplaced
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    build, surface, _, reason = unplaced
    rt = make_runtime(verdict_fail_mode="deny")
    answer = await dispatch(rt, build(load), ctx())
    if surface == "harness":
        assert obj(answer)["decision"] == "deny"
        text = deny_reason(answer)
    else:
        assert answer == deny_body(None, denied_text(answer))
        text = denied_text(answer)
    assert POLICY_UNAVAILABLE in text
    assert is_own_deny(text, rt.key)
    [line] = events(caplog, "fail_mode")
    assert (line["reason"], line["mode"]) == (reason, "deny")


async def test_an_unplaced_payload_in_shadow_mode_is_allowed(
    load: Load, caplog: pytest.LogCaptureFixture, unplaced: Unplaced
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    build, _, expected, _ = unplaced
    rt = make_runtime(verdict_fail_mode="deny", shadow_mode=True)
    assert await dispatch(rt, build(load), ctx()) == expected(load)
    [line] = events(caplog, "shadow")
    assert (line["verdict"], line["answered"]) == ("deny", "allow")


async def test_a_response_we_cannot_place_is_withheld_when_denied(load: Load) -> None:
    rt = make_runtime(verdict_fail_mode="deny")
    answer = await dispatch(rt, _response_version(load), ctx())
    assert denied_text(answer).startswith("Content withheld: " + POLICY_UNAVAILABLE)


# Rule 4: exceptions


class Boom(Exception):
    pass


class Exploding:
    """A checker that raises with payload text in its message."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome:
        raise self.error


def exploding(error: BaseException, **config: object) -> runtime.Runtime:
    cfg = make_config(**config)
    return replace(
        make_runtime(), config=cfg, key=derive_key(cfg.push_token), checker=Exploding(error)
    )


SURFACES = ["harness_before_tool_call", "gateway_request", "gateway_response"]


@pytest.mark.parametrize("name", SURFACES)
async def test_a_checker_exception_is_a_no_answer_that_logs_no_payload_text(
    load: Load, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    rt = exploding(Boom("SECRET-IN-MESSAGE Bearer secret-token"))
    await dispatch(rt, load(name), ctx())
    [fail] = events(caplog, "fail_mode")
    assert fail["reason"] == "error"
    assert "SECRET-IN-MESSAGE" not in caplog.text
    assert "secret-token" not in caplog.text


async def test_an_exception_is_logged_as_its_type_name_only(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    rt = exploding(Boom("SECRET-IN-MESSAGE"))
    await dispatch(rt, load("gateway_request"), ctx())
    [line] = events(caplog, "unexpected_error")
    assert line == {"event": "unexpected_error", "surface": "gateway", "error": "Boom"}
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.parametrize(
    ("mode", "allowed"), [("allow", True), ("deny", False)], ids=["fail-open", "fail-closed"]
)
async def test_an_exception_follows_the_fail_mode(load: Load, mode: str, allowed: bool) -> None:
    rt = exploding(Boom("x"), verdict_fail_mode=mode)
    harness = await dispatch(rt, load("harness_before_tool_call"), ctx())
    gateway = await dispatch(rt, load("gateway_request"), ctx())
    if allowed:
        assert harness == ALLOW
        assert gateway == echo_request(load("gateway_request"))
    else:
        assert obj(harness)["decision"] == "deny"
        assert is_own_deny(deny_reason(harness), rt.key)
        assert is_own_deny(denied_text(gateway), rt.key)


async def test_an_exception_in_shadow_mode_is_allowed(load: Load) -> None:
    rt = exploding(Boom("x"), verdict_fail_mode="deny", shadow_mode=True)
    assert await dispatch(rt, load("harness_before_tool_call"), ctx()) == ALLOW
    assert await dispatch(rt, load("gateway_request"), ctx()) == echo_request(
        load("gateway_request")
    )


async def test_a_response_exception_is_withheld_under_fail_closed(load: Load) -> None:
    rt = exploding(Boom("x"), verdict_fail_mode="deny")
    answer = await dispatch(rt, load("gateway_response"), ctx())
    assert denied_text(answer).startswith("Content withheld: ")


@pytest.fixture
def adapter_raises(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException], None]:
    def install(error: BaseException) -> None:
        async def raising(*args: object, **kwargs: object) -> JsonValue:
            raise error

        monkeypatch.setattr(handler.harness, "handle", raising)
        monkeypatch.setattr(handler.gateway, "handle", raising)

    return install


@pytest.mark.parametrize("name", SURFACES)
@pytest.mark.parametrize("mode", ["allow", "deny"])
async def test_an_exception_from_an_adapter_follows_the_fail_mode(
    load: Load,
    caplog: pytest.LogCaptureFixture,
    adapter_raises: Callable[[BaseException], None],
    name: str,
    mode: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    adapter_raises(Boom("SECRET-IN-MESSAGE"))
    payload = load(name)
    rt = make_runtime(verdict_fail_mode=mode)
    answer = await dispatch(rt, payload, ctx())
    harness = name.startswith("harness")
    if mode == "allow":
        assert answer == (ALLOW if harness else _echo_for(name, payload))
    elif harness:
        assert is_own_deny(deny_reason(answer), rt.key)
    else:
        assert is_own_deny(denied_text(answer), rt.key)
    [line] = events(caplog, "unexpected_error")
    assert line["error"] == "Boom"
    assert "SECRET-IN-MESSAGE" not in caplog.text


def _echo_for(name: str, payload: dict[str, JsonValue]) -> JsonValue:
    return echo_response(payload) if name == "gateway_response" else echo_request(payload)


@pytest.mark.parametrize("name", SURFACES)
@pytest.mark.parametrize("mode", ["allow", "deny"])
async def test_a_recursion_error_is_unreadable_whatever_the_mode(
    load: Load,
    caplog: pytest.LogCaptureFixture,
    adapter_raises: Callable[[BaseException], None],
    name: str,
    mode: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    adapter_raises(RecursionError("SECRET-IN-MESSAGE"))
    rt = make_runtime(verdict_fail_mode=mode)
    answer = await dispatch(rt, load(name), ctx())
    text = deny_reason(answer) if name.startswith("harness") else denied_text(answer)
    assert UNREADABLE in text
    assert is_own_deny(text, rt.key)
    [line] = events(caplog, "unreadable")
    assert line["reason"] == "too_deep"
    assert "SECRET-IN-MESSAGE" not in caplog.text


async def test_a_recursion_error_in_shadow_mode_is_allowed(
    load: Load, adapter_raises: Callable[[BaseException], None]
) -> None:
    adapter_raises(RecursionError())
    rt = make_runtime(shadow_mode=True)
    assert await dispatch(rt, load("harness_before_tool_call"), ctx()) == ALLOW


async def test_a_payload_that_is_not_an_object_is_a_no_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    rt = make_runtime(verdict_fail_mode="allow")
    answer = await dispatch(rt, junk(["SECRET-ITEM"]), ctx())
    assert answer == EMPTY_MCP
    [line] = events(caplog, "unexpected_error")
    assert line["surface"] == "unknown"
    assert "SECRET-ITEM" not in caplog.text
    denied = await dispatch(
        make_runtime(verdict_fail_mode="deny"),
        junk("SECRET-ITEM"),
        ctx(),
    )
    assert is_own_deny(denied_text(denied), make_runtime().key)


async def test_the_payload_is_not_changed(load: Load) -> None:
    for name in ("gateway_request", "gateway_response", "harness_before_tool_call"):
        payload = load(name)
        before = copy.deepcopy(payload)
        await dispatch(make_runtime(Outcome("deny", ("no",))), payload, ctx())
        assert payload == before


# lambda_handler


def test_lambda_handler_is_synchronous() -> None:
    assert not inspect.iscoroutinefunction(lambda_handler)
    assert isinstance(handler._LOOP, asyncio.AbstractEventLoop)


def test_lambda_handler_serves_a_harness_hook(load: Load, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = make_runtime(Outcome("allow"), Outcome("deny", ("no",)))
    monkeypatch.setattr(runtime, "_runtime", rt)
    assert lambda_handler(load("harness_before_tool_call"), None) == ALLOW
    denied = lambda_handler(load("harness_before_tool_call"), None)
    assert obj(denied)["decision"] == "deny"
    assert is_own_deny(deny_reason(denied), rt.key)


class _Custom:
    def __init__(self, custom: dict[str, str]) -> None:
        self.custom = custom


class _Context:
    """The Lambda context of a Gateway call: ``client_context.custom`` holds the AWS values."""

    def __init__(self) -> None:
        self.client_context = _Custom(
            {
                "GATEWAY_ARN": GATEWAY_ARN,
                "REQUEST_ID": RID,
            }
        )


def test_lambda_handler_serves_a_gateway_hook(load: Load, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = make_runtime(Outcome("allow"), Outcome("deny", ("no",)))
    monkeypatch.setattr(runtime, "_runtime", rt)
    payload = load("gateway_request")
    assert lambda_handler(payload, _Context()) == echo_request(payload)
    assert is_own_deny(denied_text(lambda_handler(payload, _Context())), rt.key)
    [first, _] = checker_of(rt).invocations
    assert RID in first.request_id  # the client context reached the adapter


def test_the_loop_is_reused_across_invocations(load: Load, monkeypatch: pytest.MonkeyPatch) -> None:
    loops: list[asyncio.AbstractEventLoop] = []

    class Spy(FakeChecker):
        async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome:
            loops.append(asyncio.get_running_loop())
            return await super().check(invocation, started=started)

    rt = make_runtime()
    spied = replace(rt, checker=Spy())
    monkeypatch.setattr(runtime, "_runtime", spied)
    lambda_handler(load("harness_before_tool_call"), None)
    lambda_handler(load("harness_before_tool_call"), None)
    assert loops == [handler._LOOP, handler._LOOP]
    assert not handler._LOOP.is_closed()


def test_a_bad_configuration_escapes_as_a_lambda_error(
    load: Load, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLASHID_PUSH_TOKEN")
    with pytest.raises(ValidationError):
        lambda_handler(load("harness_before_tool_call"), None)


def test_debug_logging_never_reaches_the_aws_and_http_libraries() -> None:
    """At DEBUG aiobotocore logs the signed request, Authorization and session token included."""
    probe = (
        "import json, logging\n"
        "root = logging.getLogger().level\n"
        "import slashid_agentcore_forwarder.handler\n"
        "debug = logging.DEBUG\n"
        "print(json.dumps({\n"
        "    'root_unchanged': logging.getLogger().level == root,\n"
        "    'package': logging.getLogger('slashid_agentcore_forwarder').level == debug,\n"
        "    'libraries': {n: logging.getLogger(n).isEnabledFor(debug)\n"
        "                  for n in ('botocore', 'aiobotocore', 'httpx', 'httpcore')},\n"
        "}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        env={**os.environ, "LOG_LEVEL": "debug"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(done.stdout) == {
        "root_unchanged": True,
        "package": True,
        "libraries": {"botocore": False, "aiobotocore": False, "httpx": False, "httpcore": False},
    }
