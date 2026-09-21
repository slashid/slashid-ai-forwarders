"""The HTTP surface: signature gate, verdict body, capture, failure isolation."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.main import create_app
from tests.conftest import SECRET, Signer

FRAME: dict[str, Any] = {
    "type": "prompt",
    "request_id": "req_test",
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "actor": {"type": "user", "id": "user_01A", "email_address": "a@example.com"},
    "source": {"application": "claude-code"},
    "session_id": None,
    "model": "claude-sonnet-4-5",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
    "metadata": {},
}


class MemoryCapture:
    def __init__(self, *, fail: bool = False) -> None:
        self.stored: list[tuple[str, dict[str, str], bytes]] = []
        self.fail = fail

    async def store(self, request_id: str, headers: dict[str, str], body: bytes) -> None:
        if self.fail:
            raise RuntimeError("bucket unreachable")
        self.stored.append((request_id, headers, body))


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.com",
        "push_token": "tok",
        "hook_signing_secret": SECRET,
        "gcp_project_id": "proj",
    }
    base.update(overrides)
    return Config(**base)


def _client(config: Config, capture: MemoryCapture | None = None) -> httpx.AsyncClient:
    app = create_app(config, capture=capture)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_unsigned_request_is_rejected() -> None:
    async with _client(_config()) as c:
        r = await c.post("/", content=json.dumps(FRAME).encode())
    assert r.status_code == 401


async def test_signed_prompt_frame_is_allowed(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    async with _client(_config()) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_frame_is_captured_raw_with_its_headers(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    capture = MemoryCapture()
    async with _client(_config(capture_bucket="b"), capture) as c:
        await c.post("/", content=body, headers=sign(body, "req_test"))
    assert len(capture.stored) == 1
    request_id, headers, stored_body = capture.stored[0]
    assert request_id == "req_test"
    assert stored_body == body  # raw bytes, not a re-encoding
    assert headers["webhook-id"] == "req_test"


async def test_capture_failure_never_reaches_the_verdict(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    async with _client(_config(capture_bucket="b"), MemoryCapture(fail=True)) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_unknown_top_level_type_is_allowed(sign: Signer) -> None:
    body = json.dumps({**FRAME, "type": "response"}).encode()
    async with _client(_config()) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_unparseable_body_is_still_allowed(sign: Signer) -> None:
    # A rejected body is a webhook failure; the frame is inspected, not parsed
    # for the verdict, so answer allow rather than 400.
    body = b"not json"
    async with _client(_config()) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_deny_marker_denies_only_outside_shadow_mode(sign: Signer) -> None:
    text = {"type": "text", "text": "SLASHID_DENY_ME"}
    frame = {**FRAME, "messages": [{"role": "user", "content": [text]}]}
    body = json.dumps(frame).encode()
    async with _client(_config(capture_deny_marker="SLASHID_DENY_ME")) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.json() == {"action": "allow"}
    async with _client(_config(capture_deny_marker="SLASHID_DENY_ME", shadow_mode=False)) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    verdict = r.json()
    assert verdict["action"] == "deny"
    assert verdict["deny_reason"]
    assert len(verdict["reference_id"]) == 32


async def test_oversized_body_is_rejected(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    async with _client(_config(max_body_bytes=10)) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 413


@pytest.mark.parametrize("path", ["/", "/hooks/anthropic"])
async def test_any_path_is_the_endpoint(sign: Signer, path: str) -> None:
    # Anthropic posts to whatever URL the admin configured; no fixed suffix.
    body = json.dumps(FRAME).encode()
    async with _client(_config()) as c:
        r = await c.post(path, content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
