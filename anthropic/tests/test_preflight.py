"""POST /ip/nhi/ai/preflight: the body is the tail event, the answer is a
list of deny reasons, and an empty list is a real allow."""

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

from slashid_anthropic_forwarder.hook.checks import CheckFailed, Verdict
from slashid_anthropic_forwarder.hook.preflight import preflight_check

ENDPOINT = "https://api.slashid.example"
FILES = [
    AIAccessedFile(
        name="a.txt",
        content_hashes={"sha256": "aa", "sha1": "bb", "md5": "cc"},
        media_type="text/plain",
        byte_length=3,
        provenance="tool_result",
    ),
    AIAccessedFile(name="b.txt", content_hashes={"sha256": "dd"}, provenance="attachment"),
]
SENSITIVE = 'A file in this request is marked sensitive in your organization: "a.txt"'


def tail(files: list[AIAccessedFile] | None = None) -> AIInvocationObservedV1:
    """What `pending._unanswered_round` hands over: input-only, no output,
    no stop reason, nothing the model has not produced yet."""
    return AIInvocationObservedV1(
        request_id="msg_1",
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01A"),
        model=AIModel(id="claude-opus-5", provider="anthropic"),
        parsed_as="anthropic-inference-hook",
        conversation_id="sess_1",
        accessed_files=FILES if files is None else files,
    )


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def answer(*reasons: str) -> httpx.Response:
    return httpx.Response(200, json={"deny_reasons": list(reasons)})


async def call(c: httpx.AsyncClient, **overrides: Any) -> Verdict:
    # Annotated: the mixed-type kwargs mapping otherwise infers a union the
    # type checker cannot match against the keyword parameters.
    kwargs: dict[str, Any] = dict(
        endpoint=ENDPOINT, push_token="tok", invocation=tail(), timeout_s=1.0
    )
    kwargs.update(overrides)
    return await preflight_check(c, **kwargs)


async def test_the_body_is_the_invocation_carried_by_the_push_token() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["timeout"] = request.headers.get("slashid-request-timeout")
        seen["json"] = json.loads(request.content)
        return answer()

    invocation = tail()
    async with client(handler) as c:
        await call(c, invocation=invocation)
    # The gateway routes preflight only under ``/ip``; ingest also has a
    # bare ``/nhi`` alias, which is what made the prefix look optional.
    assert seen["url"] == f"{ENDPOINT}/ip/nhi/ai/preflight"
    assert seen["auth"] == "Bearer tok"
    # The server bounds its own work to the budget we pass down, instead of a
    # fixed per-check deadline that knows nothing about ours.
    assert seen["timeout"] == "1.0"
    # Byte-for-byte what the sink would push, so there is no second shape
    # to keep in step with the event's field types.
    assert seen["json"] == invocation.model_dump(mode="json", exclude_none=True)
    # Sent early: what the model has not produced is absent, not null.
    assert "output" not in seen["json"] and "stop_reason" not in seen["json"]
    assert seen["json"]["accessed_files"][0]["content_hashes"]["sha256"] == "aa"


async def test_empty_deny_reasons_is_an_allow_not_an_unverified() -> None:
    """The server's fail-open never reaches the wire: a check it could not
    run contributes no reason and is counted server-side. So 200 with an
    empty list is an allow, never a case for our fail mode."""
    async with client(lambda r: answer()) as c:
        verdict = await call(c)
    assert verdict.action == "allow" and verdict.source == "preflight"
    assert verdict.deny_reason is None


async def test_deny_reasons_join_into_one_reason() -> None:
    async with client(lambda r: answer(SENSITIVE, "Another file is marked sensitive.")) as c:
        verdict = await call(c)
    assert verdict.action == "deny" and verdict.source == "preflight"
    assert verdict.deny_reason == f"{SENSITIVE} Another file is marked sensitive."


async def test_every_accessed_file_is_sent() -> None:
    """No cap. Preflight fails closed, so a batch it cannot finish denies
    rather than slipping through — which is what makes sending everything
    safe. Truncating here would be the bypass: a sensitive file in position
    101 would simply never be checked."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return answer()

    many = [AIAccessedFile(name=f"{i}.txt", content_hashes={"sha256": "x"}) for i in range(150)]
    async with client(handler) as c:
        verdict = await call(c, invocation=tail(many))
    assert verdict.action == "allow"
    assert len(seen["json"]["accessed_files"]) == 150
    assert seen["json"]["accessed_files"][-1]["name"] == "149.txt"


@pytest.mark.parametrize("status", [400, 401, 404, 503])
async def test_non_200_raises(status: int) -> None:
    async with client(lambda r: httpx.Response(status, text="x")) as c:
        with pytest.raises(CheckFailed):
            await call(c)


@pytest.mark.parametrize(
    "body",
    [
        {"kwargs": {"text": "not json"}},
        {"kwargs": {"json": {}}},
        {"kwargs": {"json": {"deny_reasons": "a.txt is sensitive"}}},
    ],
    ids=["not_json", "no_deny_reasons", "not_a_list_of_strings"],
)
async def test_unparseable_body_raises(body: dict) -> None:
    async with client(lambda r: httpx.Response(200, **body["kwargs"])) as c:
        with pytest.raises(CheckFailed):
            await call(c)


async def test_transport_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with client(handler) as c:
        with pytest.raises(CheckFailed):
            await call(c)
