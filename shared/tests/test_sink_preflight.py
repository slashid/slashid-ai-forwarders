"""POST /ip/nhi/events/ai-invocations/preflight: the body is the invocation
the push would carry, and the answer is a list of deny reasons."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)
from slashid_ai_forwarder_core.sink import (
    PreflightError,
    preflight_invocation,
    preflight_verdict,
)

ENDPOINT = "https://api.slashid.example"


def invocation(files: list[AIAccessedFile] | None = None) -> AIInvocationObservedV1:
    """Input-only: no output, no stop reason, nothing the model has not
    produced yet."""
    return AIInvocationObservedV1(
        request_id="msg_1",
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01A"),
        model=AIModel(id="claude-opus-5", provider="anthropic"),
        parsed_as="anthropic-inference-hook",
        conversation_id="sess_1",
        accessed_files=files
        if files is not None
        else [AIAccessedFile(name="a.txt", content_hashes={"sha256": "aa"})],
    )


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def call(c: httpx.AsyncClient, **overrides: Any) -> list[str]:
    kwargs: dict[str, Any] = dict(endpoint=ENDPOINT, push_token="tok", timeout_s=1.0)
    inv = overrides.pop("invocation", None) or invocation()
    kwargs.update(overrides)
    return await preflight_invocation(c, inv, **kwargs)


async def test_the_body_is_the_invocation_carried_by_the_push_token() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["timeout"] = request.headers.get("slashid-request-timeout")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"deny_reasons": []})

    inv = invocation()
    async with client(handler) as c:
        assert await call(c, invocation=inv) == []
    assert seen["url"] == f"{ENDPOINT}/ip/nhi/events/ai-invocations/preflight"
    assert seen["auth"] == "Bearer tok"
    # Our budget less the return margin: the server spends all of it and
    # denies at the end, and that deny has to reach us before we give up.
    assert seen["timeout"] == "0.500"
    # Byte-for-byte what the push would send, so there is no second shape.
    assert seen["json"] == inv.model_dump(mode="json", exclude_none=True)
    assert "output" not in seen["json"] and "stop_reason" not in seen["json"]


async def test_a_budget_smaller_than_the_margin_sends_the_server_floor() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.headers.get("slashid-request-timeout")
        return httpx.Response(200, json={"deny_reasons": []})

    async with client(handler) as c:
        await call(c, timeout_s=0.3)
    assert seen["timeout"] == "0.050"


async def test_deny_reasons_come_back_as_sent() -> None:
    body = {"deny_reasons": ["a.txt is sensitive.", "b.txt is sensitive."]}
    async with client(lambda r: httpx.Response(200, json=body)) as c:
        assert await call(c) == body["deny_reasons"]


async def test_a_field_the_server_adds_is_ignored() -> None:
    body = {"deny_reasons": ["a.txt is sensitive."], "checked": ["hashes"]}
    async with client(lambda r: httpx.Response(200, json=body)) as c:
        assert await call(c) == ["a.txt is sensitive."]


async def test_every_accessed_file_is_sent() -> None:
    """No cap. Preflight fails closed, so a batch it cannot finish denies;
    truncating here would be the bypass."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"deny_reasons": []})

    many = [AIAccessedFile(name=f"{i}.txt", content_hashes={"sha256": "x"}) for i in range(150)]
    async with client(handler) as c:
        await call(c, invocation=invocation(many))
    assert len(seen["json"]["accessed_files"]) == 150


@pytest.mark.parametrize("status", [400, 401, 404, 503])
async def test_non_200_raises(status: int) -> None:
    async with client(lambda r: httpx.Response(status, text="x")) as c:
        with pytest.raises(PreflightError):
            await call(c)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "not json"},
        {"json": {}},
        {"json": {"deny_reasons": "a.txt is sensitive"}},
        {"json": {"deny_reasons": [1]}},
    ],
    ids=["not_json", "no_deny_reasons", "not_a_list", "not_strings"],
)
async def test_unparseable_body_raises(kwargs: dict) -> None:
    async with client(lambda r: httpx.Response(200, **kwargs)) as c:
        with pytest.raises(PreflightError):
            await call(c)


async def test_transport_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with client(handler) as c:
        with pytest.raises(PreflightError):
            await call(c)


def test_deny_reasons_are_joined_with_a_semicolon_and_blanks_dropped() -> None:
    from slashid_ai_forwarder_core.sink import join_deny_reasons

    assert join_deny_reasons(["a. ", "", "  ", "b."]) == "a; b."
    assert join_deny_reasons(["a", "b", "c."]) == "a; b; c."
    assert join_deny_reasons(["only."]) == "only."
    assert join_deny_reasons([]) == ""


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "cause"),
    [
        (_raise(httpx.ConnectError("x")), "transport"),
        (_raise(httpx.ReadTimeout("x")), "timeout"),
        (lambda request: httpx.Response(500), "status"),
        (lambda request: httpx.Response(200, json={"nope": 1}), "unparseable"),
    ],
)
async def test_a_failure_says_what_kind_it_was(handler, cause: str) -> None:
    with pytest.raises(PreflightError) as caught:
        await call(client(handler))
    assert caught.value.cause == cause


DENIED = {
    "deny_reasons": ["This model is not allowed."],
    "decision": {
        "effect": "deny",
        "reason": "This model is not allowed.",
        "rule_id": "no-opus",
        "policy_id": "conn:c1",
    },
}


async def test_the_verdict_asks_for_the_decision_and_names_the_rule_behind_a_deny() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["capabilities"] = request.headers.get("slashid-preflight-capabilities")
        return httpx.Response(200, json=DENIED)

    async with client(handler) as c:
        verdict = await preflight_verdict(
            c, invocation(), endpoint=ENDPOINT, push_token="tok", timeout_s=1.0
        )
    assert seen["capabilities"] == "decision"
    assert verdict.deny_reasons == ["This model is not allowed."]
    assert verdict.decision is not None
    assert (verdict.decision.effect, verdict.decision.rule_id, verdict.decision.policy_id) == (
        "deny",
        "no-opus",
        "conn:c1",
    )


async def test_a_server_that_names_no_decision_still_gives_a_verdict() -> None:
    body = {"deny_reasons": ["a.txt is sensitive."]}
    async with client(lambda r: httpx.Response(200, json=body)) as c:
        verdict = await preflight_verdict(
            c, invocation(), endpoint=ENDPOINT, push_token="tok", timeout_s=1.0
        )
    assert verdict.deny_reasons == ["a.txt is sensitive."] and verdict.decision is None


async def test_a_decision_in_a_shape_we_do_not_know_does_not_cost_the_verdict() -> None:
    """The reasons are the verdict. A decision we cannot read is one we
    cannot name the rule from, not a check that failed."""
    body = {"deny_reasons": [], "decision": {"effect": "mask", "mask": ["ssn"]}}
    async with client(lambda r: httpx.Response(200, json=body)) as c:
        verdict = await preflight_verdict(
            c, invocation(), endpoint=ENDPOINT, push_token="tok", timeout_s=1.0
        )
    assert verdict.deny_reasons == [] and verdict.decision is None
