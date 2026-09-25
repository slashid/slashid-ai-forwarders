"""Preflight as a verdict: an empty list is a real allow, reasons join into
one deny, and a call that got no answer is a ``CheckFailed``. The wire
itself is tested with ``sink.preflight_invocation`` in ``shared``."""

from __future__ import annotations

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


async def test_no_answer_is_a_check_failed() -> None:
    async with client(lambda r: httpx.Response(503, text="x")) as c:
        with pytest.raises(CheckFailed):
            await call(c)
