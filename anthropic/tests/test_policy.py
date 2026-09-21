"""The policy receiver re-verifies the signature, so the forward must be
byte-for-byte the frame Anthropic sent, with its three webhook headers."""

from __future__ import annotations

import httpx
import pytest

from slashid_anthropic_forwarder.hook.checks import CheckFailed
from slashid_anthropic_forwarder.hook.policy import policy_check

URL = "https://api.slashid.example/ai-access/acme"
HEADERS = {
    "Webhook-Id": "msg_1",
    "webhook-timestamp": "1789945700",
    "webhook-signature": "v1,abc",
    "content-type": "application/json",
    "user-agent": "anthropic-dlp/1",
    "x-forwarded-for": "1.2.3.4",
}
BODY = b'{"type":"prompt","request_id":"msg_1"}'


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_forwards_raw_bytes_and_only_the_webhook_headers() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"action": "allow"})

    async with client(handler) as c:
        verdict = await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
    assert verdict.action == "allow" and verdict.source == "policy"
    assert seen["body"] == BODY
    assert seen["headers"]["webhook-id"] == "msg_1"
    assert seen["headers"]["webhook-timestamp"] == "1789945700"
    assert seen["headers"]["webhook-signature"] == "v1,abc"
    assert seen["headers"]["content-type"] == "application/json"
    assert "x-forwarded-for" not in seen["headers"]
    assert "content-encoding" not in seen["headers"]


async def test_deny_with_reason_and_reference_is_returned() -> None:
    body = {"action": "deny", "deny_reason": "no", "reference_id": "ref"}
    async with client(lambda r: httpx.Response(200, json=body)) as c:
        verdict = await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
    assert verdict.action == "deny" and verdict.deny_reason == "no"
    assert verdict.reference_id == "ref"


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_non_200_raises(status: int) -> None:
    async with client(lambda r: httpx.Response(status, text="x")) as c:
        with pytest.raises(CheckFailed):
            await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)


async def test_unknown_action_raises() -> None:
    async with client(lambda r: httpx.Response(200, json={"action": "maybe"})) as c:
        with pytest.raises(CheckFailed):
            await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)


async def test_transport_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with client(handler) as c:
        with pytest.raises(CheckFailed):
            await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
