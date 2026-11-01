from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from slashid_openai_forwarder.compliance import WorkspaceUser
from slashid_openai_forwarder.config import Config
from slashid_openai_forwarder.directory import UserDirectory
from slashid_openai_forwarder.hook import TOOL_NAME
from slashid_openai_forwarder.main import create_app
from slashid_openai_forwarder.oidc import TokenVerifier

from .conftest import ISSUER, Signer

ENDPOINT = "https://api.slashid.test"


def _config(**kw: Any) -> Config:
    return Config(
        push_token="t" * 40,
        endpoint=ENDPOINT,
        public_url="https://hooks.example.com",
        oidc_issuer=ISSUER,
        **kw,
    )


class _SlashID:
    """The preflight endpoint: records what it was asked, answers ``deny``."""

    def __init__(self, deny: list[str] | None = None, status: int = 200) -> None:
        self.deny = deny or []
        self.status = status
        self.asked: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ip/nhi/events/ai-invocations/preflight"
        assert request.headers["authorization"] == "Bearer " + "t" * 40
        self.asked.append(json.loads(request.content))
        return httpx.Response(self.status, json={"deny_reasons": self.deny})


async def _users() -> list[WorkspaceUser]:
    return [WorkspaceUser(id="user-a", email="alice@example.com")]


def _app(tokens: TokenVerifier, slashid: _SlashID, **kw: Any):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(slashid))
    directory = UserDirectory(_users, ttl_seconds=600, miss_refresh_seconds=60)
    return create_app(_config(**kw), tokens=tokens, directory=directory, client=upstream)


def _call(arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": TOOL_NAME, "arguments": arguments},
    }


PROMPT = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t", "prompt": "p"}
TOOL = {
    "hook_event_name": "PreToolUse",
    "session_id": "s",
    "turn_id": "t",
    "tool_name": "mcp__github__create_issue",
    "tool_input": {"title": "x"},
    "tool_use_id": "call_1",
}

Post = Callable[..., Any]


@pytest.fixture
def post() -> Post:
    async def _post(app: Any, body: dict, token: str | None) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://hooks.example.com"
        ) as c:
            return await c.post("/mcp", json=body, headers=headers)

    return _post


def _text(response: httpx.Response) -> dict:
    return json.loads(response.json()["result"]["content"][0]["text"])


async def test_no_token_is_challenged_toward_the_metadata(
    tokens: TokenVerifier, post: Post
) -> None:
    response = await post(_app(tokens, _SlashID()), _call(PROMPT), None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://hooks.example.com/.well-known/oauth-protected-resource"'
    )


async def test_the_metadata_names_the_customers_idp(tokens: TokenVerifier) -> None:
    app = _app(tokens, _SlashID())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://hooks.example.com"
    ) as c:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            body = (await c.get(path)).json()
            assert body["resource"] == "https://hooks.example.com/mcp"
            assert body["authorization_servers"] == [ISSUER]


async def test_a_denied_prompt_is_blocked_as_the_token_user(
    tokens: TokenVerifier, sign: Signer, post: Post
) -> None:
    slashid = _SlashID(deny=["Prompts to gpt-5.5 are not allowed."])
    response = await post(_app(tokens, slashid), _call(PROMPT), sign())
    assert response.status_code == 200
    assert _text(response) == {"decision": "block", "reason": "Prompts to gpt-5.5 are not allowed."}
    assert slashid.asked[0]["identity_details"] == {"kind": "openai", "user_id": "user-a"}
    assert slashid.asked[0]["parsed_as"] == "chatgpt-work-hook"


async def test_a_denied_tool_call_is_denied_with_the_pretooluse_shape(
    tokens: TokenVerifier, sign: Signer, post: Post
) -> None:
    response = await post(_app(tokens, _SlashID(deny=["no"])), _call(TOOL), sign())
    assert _text(response)["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_an_allowed_call_answers_an_empty_object(
    tokens: TokenVerifier, sign: Signer, post: Post
) -> None:
    response = await post(_app(tokens, _SlashID()), _call(TOOL), sign())
    assert _text(response) == {}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("allow", {}), ("deny", {"decision": "block"})],
)
async def test_no_verdict_follows_the_fail_mode(
    tokens: TokenVerifier, sign: Signer, post: Post, mode: str, expected: dict
) -> None:
    slashid = _SlashID()
    unknown = await post(
        _app(tokens, slashid, verdict_fail_mode=mode), _call(PROMPT), sign(email="eve@example.com")
    )
    failing = await post(
        _app(tokens, _SlashID(status=503), verdict_fail_mode=mode), _call(PROMPT), sign()
    )
    for response in (unknown, failing):
        body = _text(response)
        assert {k: v for k, v in body.items() if k != "reason"} == expected
    assert slashid.asked == [], "an unknown user is never sent as someone else"


async def test_a_notification_is_accepted_without_a_body(
    tokens: TokenVerifier, sign: Signer, post: Post
) -> None:
    response = await post(
        _app(tokens, _SlashID()), {"jsonrpc": "2.0", "method": "notifications/initialized"}, sign()
    )
    assert response.status_code == 202


async def test_tick_refuses_without_a_scheduler_token(tokens: TokenVerifier) -> None:
    app = _app(tokens, _SlashID())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://hooks.example.com"
    ) as c:
        assert (await c.post("/tick")).status_code == 401
        assert (
            await c.post("/tick", headers={"authorization": "Bearer anything"})
        ).status_code == 401


async def test_a_template_that_does_not_match_still_denies_in_its_events_shape(
    tokens: TokenVerifier, sign: Signer, post: Post
) -> None:
    broken = {"hook_event_name": "PreToolUse", "session_id": "s"}
    response = await post(_app(tokens, _SlashID(), verdict_fail_mode="deny"), _call(broken), sign())
    assert _text(response)["hookSpecificOutput"]["permissionDecision"] == "deny"
