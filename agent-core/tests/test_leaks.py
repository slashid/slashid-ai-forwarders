"""Nothing from a payload reaches a log record, and the Authorization value reaches no event or
answer, on every path through the entry point."""

import base64
import json
import logging
from dataclasses import replace

import pytest
from pydantic import BaseModel, JsonValue, ValidationError
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from helpers import FakeChecker, Load, ctx, events, make_runtime, obj
from slashid_agentcore_forwarder import handler
from slashid_agentcore_forwarder.checks import Outcome
from slashid_agentcore_forwarder.deny import is_own_deny
from slashid_agentcore_forwarder.handler import dispatch
from slashid_agentcore_forwarder.runtime import CheckRunner, Runtime

AUTHORIZATION = "Bearer secret-token"
# What a payload carries that no log line and no refusal may repeat. The server's reason text
# is not one: a deny answers with it and logs only the verdict's action.
PAYLOAD_SECRETS = [
    "BAGGAGE-SECRET",
    "SECRET-FILE-NAME",
    "SECRET-FILE-ID",
    "SECRET-ARGUMENT",
    "SECRET-RESULT-TEXT",
]
EXCEPTION_SECRET = "SECRET-IN-MESSAGE"
REASON = "SECRET-REASON"

FIXTURES = [
    "gateway_request",
    "gateway_request_jwt",
    "gateway_request_notification",
    "gateway_response",
    "gateway_response_drive",
    "gateway_response_no_request",
    "gateway_response_notification",
    "gateway_response_policy_denied",
    "gateway_response_streaming",
    "gateway_tools_list_request",
    "harness_after_drive",
    "harness_after_inline",
    "harness_after_tool_call",
    "harness_after_truncated",
    "harness_before_tool_call",
    "harness_remote_mcp",
]
PATHS = ["allow", "deny", "no_answer", "unreadable", "unplaced", "exception", "recursion"]
# Never sent to the checker, so no outcome of it can deny them.
NO_REQUEST = "gateway_response_no_request"  # no tool name to check: a no-answer by design
UNCHECKED = {
    "gateway_request_notification",
    "gateway_response_notification",
    "gateway_response_streaming",
    "gateway_tools_list_request",
}


class Boom(Exception):
    pass


class Exploding:
    async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome:
        raise Boom(f"{EXCEPTION_SECRET} {AUTHORIZATION}")


async def recursing(*args: object, **kwargs: object) -> JsonValue:
    raise RecursionError(EXCEPTION_SECRET)


# Building the payload


def drive_text() -> str:
    body = {
        "content": base64.b64encode(b"# Bread\n").decode(),
        "id": "ID1",
        "title": "SECRET-FILE-NAME.md",
    }
    return json.dumps(body)


def build(name: str, load: Load) -> dict[str, JsonValue]:
    """A fixture, or one of the two result variants that carry a file name."""
    if name == "gateway_response_drive":
        payload = load("gateway_response")
        body = obj(obj(obj(payload["mcp"])["gatewayResponse"])["body"])
        body["result"] = {"isError": False, "content": [{"type": "text", "text": drive_text()}]}
        return payload
    if name == "harness_after_drive":
        payload = load("harness_after_tool_call")
        result: JsonValue = {"status": "success", "content": [{"text": drive_text()}]}
        obj(payload["context"])["toolResult"] = result
        return payload
    return load(name)


def seed(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Put the secrets in: a bearer token, a baggage value, a file id, a tool argument and a
    result text."""
    text = json.dumps(payload)
    for old, new in [
        ("AWS4-HMAC-SHA256 SYNTHETIC-SIGNATURE", AUTHORIZATION),
        ("Self=example", "Self=BAGGAGE-SECRET"),
        ('"ID1"', '"SECRET-FILE-ID"'),
        ("Synthetic file body.", "SECRET-RESULT-TEXT"),
    ]:
        text = text.replace(old, new)
    seeded = obj(json.loads(text))
    if "event" in seeded:
        obj(seeded["headers"])["Authorization"] = AUTHORIZATION
        tool_context = obj(seeded["context"])
        if "toolInput" in tool_context:
            obj(tool_context["toolInput"])["note"] = "SECRET-ARGUMENT"
    else:
        for params in _params(seeded):
            if "arguments" in params:
                obj(params["arguments"])["note"] = "SECRET-ARGUMENT"
    return seeded


def _params(payload: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """The ``params`` of the Gateway request a payload carries, if any."""
    if "mcp" not in payload:
        return []
    request = obj(payload["mcp"])["gatewayRequest"]
    if not isinstance(request, dict) or not isinstance(request["body"], dict):
        return []
    body = request["body"]
    return [body["params"]] if "params" in body and isinstance(body["params"], dict) else []


def blank_the_name(payload: dict[str, JsonValue]) -> None:
    """The envelope of the call can no longer be read."""
    if "event" in payload:
        obj(payload["context"])["toolName"] = ""
    for params in _params(payload):
        params["name"] = ""


def change_the_format(payload: dict[str, JsonValue]) -> None:
    if "event" in payload:
        payload["event"] = "an_event_from_the_future"
    else:
        payload["interceptorInputVersion"] = "2.0"


# Reading the answer


class _Block(BaseModel):
    text: str


class _Result(BaseModel):
    content: list[_Block]


class _Body(BaseModel):
    result: _Result


class _Reply(BaseModel):
    body: _Body


class _Mcp(BaseModel):
    transformedGatewayResponse: _Reply


class _Gateway(BaseModel):
    mcp: _Mcp


class _Harness(BaseModel):
    decision: str


def is_deny(answer: JsonValue, key: bytes) -> bool:
    """A Harness deny, or a Gateway reply whose text is one of ours."""
    try:
        return _Harness.model_validate(answer).decision == "deny"
    except ValidationError:
        pass
    try:
        blocks = _Gateway.model_validate(answer).mcp.transformedGatewayResponse.body.result.content
    except ValidationError:
        return False
    return any(is_own_deny(block.text, key) for block in blocks)


# The test


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.parametrize("path", PATHS)
async def test_no_path_leaks_a_payload(
    load: Load,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    path: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    handler.reset_announced()
    payload = seed(build(fixture, load))
    outcome = {
        "deny": Outcome("deny", (REASON,)),
        "no_answer": Outcome("no_answer", why="timeout"),
    }.get(path, Outcome("allow"))
    runtime = make_runtime(outcome, verdict_fail_mode="deny")
    if path == "unreadable":
        blank_the_name(payload)
    elif path == "unplaced":
        change_the_format(payload)
    elif path == "exception":
        exploding: CheckRunner = Exploding()
        runtime = replace(runtime, checker=exploding)
    elif path == "recursion":
        monkeypatch.setattr(handler.harness, "handle", recursing)
        monkeypatch.setattr(handler.gateway, "handle", recursing)

    answer = await dispatch(runtime, payload, ctx())

    logged = caplog.text + "".join(f"{r.getMessage()}{r.args}{r.exc_text}" for r in caplog.records)
    for secret in ["secret-token", *PAYLOAD_SECRETS, EXCEPTION_SECRET, REASON]:
        assert secret not in logged, secret
    assert all(r.exc_info is None for r in caplog.records)
    assert all(
        sorted(line) == ["check", "event", "latency_ms", "outcome", "surface"]
        and line["check"] in ("call", "result")
        and line["outcome"] in ("allow", "deny", "no_answer")
        for line in events(caplog, "check")
    )

    sent = [event.model_dump_json() for event in sent_events(runtime)]
    assert all("secret-token" not in event for event in sent)
    assert all("BAGGAGE-SECRET" not in event for event in sent)
    seen = json.dumps(answer)
    assert "secret-token" not in seen
    denied = is_deny(answer, runtime.key)
    if denied:  # a refusal repeats nothing of the call
        for secret in [*PAYLOAD_SECRETS, EXCEPTION_SECRET]:
            assert secret not in seen, secret
    if path == "allow" and fixture != NO_REQUEST:
        assert not denied
    if path == "recursion" or (
        path in ("deny", "no_answer", "exception") and fixture not in UNCHECKED
    ):
        assert denied
    if path == "exception" and fixture not in UNCHECKED and fixture != NO_REQUEST:
        assert [line["error"] for line in events(caplog, "unexpected_error")] == ["Boom"]


def sent_events(runtime: Runtime) -> list[AIInvocationObservedV1]:
    return runtime.checker.invocations if isinstance(runtime.checker, FakeChecker) else []
