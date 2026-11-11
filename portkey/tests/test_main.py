"""The HTTP surface Portkey calls: one POST per hook, always answered with a verdict."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from slashid_portkey_forwarder import main
from slashid_portkey_forwarder.config import Config
from slashid_portkey_forwarder.main import create_app
from tests.hooks import TEXT_ANSWER, after, before, completion, tool_call
from tests.slashid import ENDPOINT, SlashID

AUTH = {"authorization": "Bearer tok-1"}
BLOCKED = "Prompt matches the blocked pattern 'project falcon'."
TOOL_ANSWER: dict[str, Any] = {
    "role": "assistant",
    "content": None,
    "tool_calls": [tool_call("call_1", "ship it")],
}


async def post(
    slashid: SlashID,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
    **overrides: Any,
) -> httpx.Response:
    settings: dict[str, Any] = {"endpoint": ENDPOINT, "max_retries": 0, **overrides}
    app = create_app(Config(**settings), client=slashid.client)
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://forwarder") as client:
        return await client.post(
            "/hooks/portkey", content=content, headers=AUTH if headers is None else headers
        )


async def test_a_call_without_a_token_is_refused() -> None:
    slashid = SlashID()
    response = await post(slashid, before(), headers={})
    assert response.status_code == 401
    assert slashid.tokens == []


async def test_a_body_over_the_limit_is_refused() -> None:
    response = await post(SlashID(), before(), max_body_bytes=10)
    assert response.status_code == 413


async def test_before_hook_allowed() -> None:
    slashid = SlashID()
    response = await post(slashid, before())
    assert (response.status_code, response.json()) == (200, {"verdict": True})
    assert slashid.tokens == ["Bearer tok-1"]
    [checked] = slashid.checked
    assert checked["identity_details"] == {"kind": "ai_gateway", "user_email": "ada@example.com"}
    assert slashid.pushed == []


async def test_before_hook_denied() -> None:
    slashid = SlashID(BLOCKED)
    response = await post(slashid, before())
    assert response.status_code == 200
    assert response.json()["verdict"] is False
    assert response.json()["data"]["deny_reasons"] == [BLOCKED]
    assert slashid.pushed == []


async def test_a_request_naming_nobody_takes_the_fail_mode_without_asking_slashid() -> None:
    slashid = SlashID(BLOCKED)
    allowed = await post(slashid, before(metadata={"team": "styling"}))
    denied = await post(slashid, before(metadata={}), verdict_fail_mode="deny")
    assert allowed.json() == {"verdict": True}
    assert denied.json()["verdict"] is False
    assert slashid.checked == []


async def test_a_request_that_is_not_a_chat_takes_the_fail_mode() -> None:
    slashid = SlashID()
    allowed = await post(slashid, before(request_type="embed"))
    denied = await post(slashid, before(request_type="embed"), verdict_fail_mode="deny")
    assert allowed.json() == {"verdict": True}
    assert denied.json()["verdict"] is False
    assert slashid.checked == []


async def test_a_body_that_is_not_a_hook_takes_the_fail_mode() -> None:
    assert (await post(SlashID(), b"not json")).json() == {"verdict": True}
    denied = await post(SlashID(), {"eventType": "somethingElse"}, verdict_fail_mode="deny")
    assert (denied.status_code, denied.json()["verdict"]) == (200, False)


async def test_an_event_that_cannot_be_built_takes_the_fail_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "before_event", broken)
    response = await post(SlashID(), before(), verdict_fail_mode="deny")
    assert (response.status_code, response.json()["verdict"]) == (200, False)


async def test_after_hook_judges_the_answer_and_records_the_invocation() -> None:
    slashid = SlashID()
    response = await post(slashid, after(completion(TEXT_ANSWER)))
    assert response.json() == {"verdict": True}
    [checked] = slashid.checked
    assert checked["output"]["redacted_text"] == "Falcon is on track."
    assert "input" not in checked
    [pushed] = slashid.pushed
    assert pushed["tokens"]["input"] == 12
    assert "redacted_text" not in pushed["output"]
    assert slashid.tokens == ["Bearer tok-1", "Bearer tok-1"]


async def test_after_hook_withholds_an_answer_slashid_denies_and_records_it_anyway() -> None:
    slashid = SlashID("The answer contains content your organization's policy does not allow.")
    response = await post(slashid, after(completion(TEXT_ANSWER)))
    assert response.json()["verdict"] is False
    assert len(slashid.pushed) == 1


async def test_a_streamed_answer_is_recorded_without_a_check() -> None:
    slashid = SlashID()
    response = await post(slashid, after(None))
    assert response.json() == {"verdict": True}
    assert slashid.checked == []
    assert len(slashid.pushed) == 1


async def test_after_hook_judges_the_tool_calls_and_records_either_way() -> None:
    slashid = SlashID("Tool create_issue is not allowed.")
    response = await post(slashid, after(completion(TOOL_ANSWER)))
    assert response.json()["verdict"] is False
    [checked] = slashid.checked
    assert checked["requested_tool_uses"][0]["arguments"] == {"title": "ship it"}
    assert "input" not in checked
    [pushed] = slashid.pushed
    assert pushed["request_id"] == checked["request_id"]
    assert "arguments" not in pushed["requested_tool_uses"][0]


async def test_a_push_that_fails_does_not_change_the_answer() -> None:
    slashid = SlashID(push_status=500)
    response = await post(slashid, after(completion(TEXT_ANSWER)))
    assert (response.status_code, response.json()) == (200, {"verdict": True})


async def test_nothing_is_recorded_when_push_is_off() -> None:
    slashid = SlashID()
    response = await post(slashid, after(completion(TEXT_ANSWER)), push_events=False)
    assert response.json() == {"verdict": True}
    assert slashid.pushed == []


async def test_shadow_mode_answers_allow_to_a_denied_request() -> None:
    slashid = SlashID(BLOCKED)
    response = await post(slashid, before(), shadow_mode=True)
    assert response.json() == {"verdict": True}
    assert len(slashid.checked) == 1
